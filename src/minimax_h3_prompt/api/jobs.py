"""执行任务：同进程 asyncio、单写入者、协作式取消、启动对账（D2 §6 / issue #217）。

为什么阻塞调用走 ``asyncio.to_thread``：``run_stage1`` 是同步的 LangGraph 流，一次模型
调用可能几十秒到几分钟；直接在事件循环里跑，健康检查、详情与取消请求全部被拖住。
线程池里跑图，事件循环只负责等它、并保持可响应（D2 §6 第一条）。

取消**不是立即的**：请求只置一个 ``threading.Event``，真正的停在节点边界
（``on_step_done`` 回调）——当前节点跑完、状态已落盘，才抛 ``JobCancelled``。
界面与文案都必须按「当前步骤结束后停止」表述，不许承诺即时停止。

快照的写入全部经 ``WorkbenchSessionStore.update``（原子写 + revision），执行器不直接
碰会话文件；唯一例外是 ``active-job.json``——它记录「谁在写这个会话」，崩溃后由
``reconcile`` 据此对账。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只用于段级注入卡写入口的签名，运行时按鸭子类型调用
    from ..ref_segments import RefSegmentJob

from .. import step_machine
from ..brief_parser import is_director_mode, is_ref_mode, route_uses_asset_ledger
from ..errors import error_code
from ..kinds import KIND_PICTURE
from ..segment_validation import segment_submission_problems
from ..session_store import FRAME_SUBMISSION_KEY, atomic_write_text, load_session
from ..summary import PromptSummary, render_summary_zh
from ..workbench_store import (
    ARTIFACT_CONCAT_ORDER_CARD,
    ARTIFACT_DIRECTOR_FINAL_FILM,
    ARTIFACT_DIRECTOR_FINAL_PACK,
    ARTIFACT_DIRECTOR_PACK,
    ARTIFACT_FINAL_FILM,
    ARTIFACT_FIRST_FRAME_PROMPT,
    ARTIFACT_LAST_FRAME_PROMPT,
    ARTIFACT_REF_SHOT_TABLE,
    ARTIFACT_SCRIPT,
    ARTIFACT_SHOT_TABLE,
    ARTIFACT_SUMMARY_JSON,
    ARTIFACT_SUMMARY_MARKDOWN,
    ARTIFACT_VIDEO_PROMPT,
    ASSETS_DIRNAME,
    ASSET_LEDGER_STATUS_PASSED,
    ASSET_LEDGER_STATUS_REGISTERED,
    ASSET_PROOFREAD_KEY,
    SEGMENTS_DIRNAME,
    ActionItemNotFound,
    ActionItemOut,
    ActionItemPending,
    ActionItemResolved,
    AspectMismatchUnconfirmed,
    AssetImageInvalid,
    ArchiveInvalid,
    CacheOutOfRoot,
    EventDraft,
    FinalFilmMismatch,
    FrameInvalid,
    FrameMismatch,
    JobNotFound,
    JobNotRunning,
    JobSummary,
    RevisionOut,
    RouteMismatch,
    SEGMENT_STATUS_PASSED,
    SEGMENT_STATUS_PENDING,
    SEGMENT_STATUS_VALIDATING,
    SEGMENT_STATUS_WAITING_VIDEO,
    SegmentNotFound,
    SegmentNotPassed,
    SegmentResubmitUnsupported,
    SegmentVideoInvalid,
    SessionBusy,
    StartImageRequired,
    VideoUnreadable,
    WorkbenchError,
    WorkbenchSessionStore,
    director_briefing_name,
    director_first_frame_prompt_name,
    director_segment_prompt_name,
    director_tailframe_prompt_name,
)

logger = logging.getLogger(__name__)

ACTIVE_JOB_FILENAME = "active-job.json"
# 会话文件里保留的 job 摘要条数（spec §5.2：最近 N 条，完整历史在事件流里）。
JOB_HISTORY_LIMIT = 20
# 导演台阶段 2 的两件事（spec #253 §5.2 / §8.1-§8.2）：规划与逐段写词。它们仍是
# ``advance`` 的 job，不单独占 step；节点名只进 ``job.progress``。
DIRECTOR_PLAN_NODE = "director_plan"
DIRECTOR_WRITE_NODE = "director_segment_write"


def build_chat_model() -> Any:
    """导演台规划/写段的模型构造口（唯一一处；测试在这一层打桩）。

    为什么不像别处那样在函数里直接 ``from ..model_factory import build_chat_model``：
    这条路线要连建两次客户端（规划一次、写段一次），接缝放在模块上，用例才能用一个
    桩同时替掉两处而不碰真实网关。延迟导入的理由与大模型栈入口一致。
    """
    from ..model_factory import build_chat_model as factory

    return factory()


# 长片分段循环的 job kind 与进度节点名（D6 spec §5.3）。进度事件的 stage 取
# ``step_machine.STEP_SEGMENTING``，与阶段标签同源。
JOB_KIND_SEGMENT = "segment"
SEGMENT_PLAN_NODE = "segment_plan"
SEGMENT_PLAN_FILENAME = "plan.json"

# ``segmenting`` 阶段里的三种子步骤（D6 #246）：一次 job 只做一件，落盘后再接下一件。
# 用「读持久化事实」决定跑哪一种（``_segment_step``），不把子步骤塞进 job 记录——
# job 记录会因 15 段 × 2 条挤爆 ``JOB_HISTORY_LIMIT``，段状态才是唯一事实源。
SEGMENT_STEP_PLANNING = "planning"
SEGMENT_STEP_WRITE = "write"
SEGMENT_STEP_VALIDATE = "validate"
# 全部段通过后的下一步（D6 §9）：拼成 final.mp4 并收成 completed。它同样由
# ``_segment_step`` 从**已落盘的段状态**判定，所以拼接失败后的重试不需要额外记账。
SEGMENT_STEP_ASSEMBLE = "assemble"
SEGMENT_STEP_DONE = "done"

# 导演台段循环的子步骤（D8 §5.5-§5.6 / §8.3 / #264）。它复用同一组段状态四态，
# 只是「pending 的下一步」比 base/ref 多：按需词、段首图、尾帧词都发生在等视频之前。
# 这些名字只进分派，不进 job 记录——判据依旧是**已落盘的段状态与词文件**。
DIRECTOR_STEP_FIRSTFRAME = "director_firstframe"
DIRECTOR_STEP_SEGMENT_IMAGE = "director_segment_image"
DIRECTOR_STEP_TAILFRAME = "director_tailframe"
DIRECTOR_STEP_WAIT_VIDEO = "director_wait_video"
DIRECTOR_STEP_FINALIZE = "director_finalize"
DIRECTOR_STEP_DONE = "director_done"
# 全部段通过之后的最后一步（D8 §12 / #266）：交付归档。它同样是「读盘判出来的」
# 子步骤——判据是快照里那条已决议、带两个素材 id 的 ``director_delivery`` 待办。
DIRECTOR_STEP_DELIVERY = "director_delivery"

# 段任务的进度节点名（带段号，前端据进度事件显示「正在写/校验第几段」）。
SEGMENT_WRITE_NODE = "segment_write"
SEGMENT_VALIDATE_NODE = "segment_validate"
SEGMENT_ASSEMBLE_NODE = "segment_assemble"
# 尾帧剥出的落点目录（不是产物：登记表只认 ``segments/shot-NN.md``）。
SEGMENT_BRIDGE_DIRNAME = "bridge_frames"

# 素材循环（D7 spec §5.3 / §6.2）：出词与校对是**两个独立 job**，都记这个 kind；
# 中间隔着持久化的 ``asset_image`` 待办（不是阻塞线程）。
JOB_KIND_ASSET_LOOP = "asset_loop"
# 导演台打包（D8 spec §5.4 / #263）：唯一会写二进制产物的 job，也是唯一可能长时间
# 占磁盘的一步。单独一个 kind 才回答得了「任务列表里跑了三分钟的那条是什么」。
JOB_KIND_PACK = "pack"
DIRECTOR_PACK_NODE = "director_pack"
# 交付归档（D8 §12 / #266）：唯一会复制成片并现算包摘要的 job。单独一个 kind 才回答
# 得了「会话为什么在收尾阶段又跑了几十秒」——它是长 I/O，不是一次段校验。
JOB_KIND_DELIVERY = "delivery"
DIRECTOR_DELIVERY_NODE = "director_delivery"
# 一次 job 只做一件：子步骤按**已落盘的台账事实**决定（``_asset_step``），不另记账。
ASSET_STEP_WRITE = "write"
ASSET_STEP_PROOFREAD = "proofread"
ASSET_STEP_DONE = "done"
ASSET_WRITE_NODE = "asset_write"
ASSET_PROOFREAD_NODE = "asset_proofread"
# 资产图词的落点模板（与 ``workbench_store`` 的模式产物同号：两位件号）。
ASSET_PROMPT_FILENAME = ASSETS_DIRNAME + "/picture-{number:02d}.md"

# advance 没有请求体；幂等键绑定的「请求摘要」是一个常量，同 key 重发永远算同一份命令。
_JOB_FINGERPRINT = hashlib.sha256(b"advance").hexdigest()


def _reopen_fingerprint(index: int) -> str:
    """换段请求的摘要：段号进摘要，同 key 换一段重发不能重放上一段的响应。"""
    return hashlib.sha256(f"reopen:{int(index)}".encode("utf-8")).hexdigest()


def _require_cache_inside_root(root: Path, directory: Path) -> None:
    """段缓存目录解析后必须仍在配置根内（#265 路径安全的第一道）。

    第二道是 ``cache.assert_files_inside``（逐文件）——但它的基准就是**这个目录**：
    目录本身被符号链接指到根外时，逐文件校验也跟着跑出去，等于没校验。两道判据各管
    一层，缺一条就能把别人的文件删掉。
    """
    try:
        Path(directory).resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise CacheOutOfRoot(
            f"段缓存目录不在配置根内：{directory}（解析后不在 {root} 之下；"
            "目录可能被符号链接引到别处）") from None


def _resolve_fingerprint(item_id: str, decision: str | None,
                         revisions: list[dict[str, str]],
                         frames: dict[str, str] | None,
                         video_asset_id: str | None = None,
                         asset_id: str | None = None,
                         start_asset_id: str | None = None,
                         end_asset_id: str | None = None,
                         confirm_aspect_mismatch: bool = False,
                         pack_asset_id: str | None = None) -> str:
    """resolve 的请求摘要：待办、决定、修订内容、帧槽与素材标识都算进去。

    段视频素材（``video_asset_id``）必须进摘要：不进的话，同一个 Idempotency-Key 换一段
    视频重发会**重放第一次的响应**，用户以为重交了、系统其实没执行。素材循环的交图
    （``asset_id``）同理：换一张图重发不能重放上一张的结论。导演台的两张图与画幅确认
    同一条纪律：换图或改确认状态都不算同一份请求。
    """
    canonical = json.dumps(
        {"item_id": item_id, "decision": decision, "revisions": revisions,
         "frames": frames or {}, "video_asset_id": video_asset_id or "",
         "asset_id": asset_id or "", "start_asset_id": start_asset_id or "",
         "end_asset_id": end_asset_id or "",
         "confirm_aspect_mismatch": bool(confirm_aspect_mismatch),
         "pack_asset_id": pack_asset_id or ""},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# resolve 的四个去向。待办 kind 与 brief 建好后不再改写，所以先在锁外读一次决定
# 「要不要起任务」是安全的；「待办是否仍 pending」仍由 _decide 在存储层锁内判定。
# 帧选择在决议时落进 ``session_store.FRAME_SUBMISSION_KEY``（为什么要先落盘见那里）。
_BRANCH_REVIEW = "review"
_BRANCH_FRAME_GATE = "frame_gate"
_BRANCH_FRAMES = "frames"
# 第三个待办（D6 §8）：交本段视频。与帧提交同形——不是 approve/revise，靠 video_asset_id
# 分型，resolve 之后起一个段校验 job。分支名就是待办 kind，不另抄一份字面量。
_BRANCH_SEGMENT_VIDEO = step_machine.ACTION_SEGMENT_VIDEO
# 第四个去向（D7 §5.3）：素材循环。它同时覆盖两个入口——ref 会话通过阶段 1 审查
# （先出第 1 件的词）与交回一张素材图（跑校对）。
_BRANCH_ASSET_LOOP = step_machine.ACTION_ASSET_IMAGE
# 第五个去向（D8 §5.6）：导演台开场首帧。**两个入口共用同一个待办 kind、不同分支**：
# 审查通过后只挂待办（``_GATE``，不起任务，与 base 的帧闸门同形），交图之后才起阶段 2
# （``_BRANCH_DIRECTOR_START_IMAGE``）。合成一个分支会让「刚挂出的待办」也被当成
# 「要起任务的交图」，直接撞上「非闸门分支没有任务」的内部不一致。
_BRANCH_DIRECTOR_START_GATE = "director_start_gate"
_BRANCH_DIRECTOR_START_IMAGE = step_machine.ACTION_DIRECTOR_START_IMAGE
# 第六个去向（D8 §5.6 / #264）：切镜段的段首图。与开场首帧同形——交图之后起 job
# 继续本段（写尾帧词或挂 segment_video），没有闸门入口（词先自动产出，再挂待办）。
_BRANCH_DIRECTOR_SEGMENT_IMAGE = step_machine.ACTION_DIRECTOR_SEGMENT_IMAGE
# 第七个去向（D8 §12 / #266）：交付归档。它有自己的同步校验（包段数/成片宽高），
# 过了才起 job；与其他交图分支不同，这一支的 job kind 是 ``delivery``。
_BRANCH_DIRECTOR_DELIVERY = step_machine.ACTION_DIRECTOR_DELIVERY


def _asset_step_of(
    ledger: Sequence[Mapping[str, Any]],
) -> tuple[str, int]:
    """素材循环走到哪一步：**只看台账**（最大连续通过编号 + 1），不看 job 历史。

    严格串行意味着「第 1..N-1 件已定稿、当前是第 N 件」由编号本身就说得清；事件与
    job 记录只做通知，进程重启后不靠它们重建进度（D7 §6.4 / §14）。当前件上带着
    ``ASSET_PROOFREAD_KEY`` ＝ 用户已交图、还差校对；否则该件还欠出词。
    """
    pictures = sorted(
        (entry for entry in ledger if str(entry.get("kind")) == KIND_PICTURE),
        key=lambda entry: int(entry["number"]))
    index = 0
    while (index < len(pictures)
           and pictures[index].get("status") == ASSET_LEDGER_STATUS_PASSED):
        index += 1
    if index >= len(pictures):
        return (ASSET_STEP_DONE, 0)
    entry = pictures[index]
    number = int(entry["number"])
    if str(entry.get(ASSET_PROOFREAD_KEY) or "").strip():
        return (ASSET_STEP_PROOFREAD, number)
    return (ASSET_STEP_WRITE, number)


def _settle_asset(
    ledger: Sequence[Mapping[str, Any]], number: int, passed: bool,
) -> list[dict[str, Any]]:
    """把第 ``number`` 件的校对结论写进台账：轮次 +1，通过即定稿，收回交图标识。

    这是「轮次 + 状态」的唯一转移规则：收尾写盘与「下一件是谁」的判定都调它，
    两处各写一遍就会在改状态名时静默分家。
    """
    updated: list[dict[str, Any]] = []
    for entry in ledger:
        row = dict(entry)
        if (str(row.get("kind")) == KIND_PICTURE
                and int(row.get("number", 0)) == int(number)):
            row["round"] = int(row.get("round", 0)) + 1
            row["status"] = (ASSET_LEDGER_STATUS_PASSED if passed
                             else ASSET_LEDGER_STATUS_REGISTERED)
            # 交上去的那张已经判过：清掉它，下一轮重新交图才算数。
            row.pop(ASSET_PROOFREAD_KEY, None)
        updated.append(row)
    return updated


def _render_asset_prompt(number: int, name: str, prompt: str) -> str:
    """资产图词产物正文：头部写语义名/类型/编号，空行以下是可直接复制的词（D7 §7）。"""
    return "\n".join([
        f"# 资产图提示词 · 第 {number} 件",
        f"- 语义名：{name}",
        "- 类型：Picture",
        f"- 编号：{number}",
        "",
        str(prompt).strip(),
        "",
    ])


def _parse_asset_prompt(text: str) -> str:
    """从产物正文取回词本体（头部是给人看的标签，不参与校对与改写）。

    头部固定四行 + 一个空行。找不到分隔行时整篇当词——那时产物是旧形态，宁可多带
    一段标签也不要丢掉词。
    """
    _head, separator, body = str(text).partition("\n\n")
    return body if separator else str(text)


class JobCancelled(Exception):
    """协作式取消在节点边界生效；``node`` 是停下时刚跑完的节点。"""

    def __init__(self, node: str) -> None:
        super().__init__(f"任务在当前节点结束后取消（{node or '未知'}）")
        self.node = node


class SegmentAssemblyRefused(RuntimeError):
    """拼接没有产出成片（缺段／段表对不上／ffmpeg 失败）。

    ``reason`` 逐字来自 ``assemble_film`` 的返回（**它已点名第几段**）——改写成一句
    「拼接失败」会让用户不知道该补哪一段，所以这里只做搬运，不做归纳。
    """

    code = "segment_assembly_refused"


class DirectorPackFailed(WorkbenchError):
    """导演包装配失败（D8 spec §9.1 / §15）：消息逐字来自打包器，已点名约束与段。

    打包器在**落盘之前**跑完八条约束；这里只把 ``ValueError`` 翻成稳定的
    ``pack_build_failed``，不改写它点名的那句话——用户要照着它去改画布/素材/段表。
    """

    status_code = 500
    code = "pack_build_failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dict(job: JobSummary | dict[str, Any]) -> dict[str, Any]:
    return job.model_dump(mode="json") if isinstance(job, JobSummary) else dict(job)


def _job_id_of(job: JobSummary | dict[str, Any]) -> str:
    """摘要或已落盘的字典都认：长片收尾要把**两个**任务写进同一份 jobs 列表。"""
    if isinstance(job, JobSummary):
        return job.job_id
    return str(job.get("job_id", ""))


def _segment_rejection_reason(end_hook: str, description: str) -> str:
    """语义闸门拒绝的可读原因（D6 §5.4）：尾帧实际画面 + 本段要求，缺一则省略。"""
    frame = description.strip() or "（读图没有给出描述）"
    hook = end_hook.strip()
    reason = f"尾帧未达到本段末态：{frame}"
    return f"{reason}｜要求：{hook}" if hook else reason


def _upsert_job(jobs: Any, job: dict[str, Any]) -> list[dict[str, Any]]:
    """按 job_id 覆盖或追加，并只保留最近 ``JOB_HISTORY_LIMIT`` 条。"""
    items = [_job_dict(item) for item in jobs if _job_id_of(item) != job["job_id"]]
    items.append(dict(job))
    return items[-JOB_HISTORY_LIMIT:]


def _find_job(jobs: Any, job_id: str) -> JobSummary | None:
    for item in jobs:
        if item.job_id == job_id:
            return item
    return None


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。Windows 上不能用 ``os.kill(pid, 0)``——那会真的终止进程。"""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 平台分支
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class _DirectorWriteContext:
    """一次导演台阶段 2 里「规划 → 写段」之间要传的全部入参。

    为什么不用 ``JobRunner`` 的实例属性：runner 是**进程级单实例**、不同会话的 job
    并行跑，实例属性会把 A 会话的全片尾帧描述/定义块串给 B 会话的写段请求，续跑分支
    还会继承上一个会话的残留值。上下文只活在一次执行里，显式传参。
    """

    shot_table: str = ""
    # r2v 的全片英文定义块；其它模式为 None。
    sections: Any = None
    registry: tuple[tuple[int, str], ...] = ()
    # 全片尾帧（fl2v）与它的读图描述：与段表清单同一份判据，写段请求要带。
    film_end: bool = False
    film_end_description: str = ""
    # 只有**本轮真的做了规划**才有预算；续跑读到 plan.json 时预算文件是规划期的
    # 产物，不补写、也不从任何残留值补。
    budget: Any = None


def _director_segment_rows(
    session_id: str, plan: Any, directory: Path,
    existing: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """导演台段表 → 快照行（D8 §6.1）：帧字段直接来自 ``DirectorSegment``。

    ``start_s`` / ``end_s`` 仍是**向下截断**的整秒（既有类型契约不变），真正的执行
    契约是帧；两个数都给，读的人不必自己除 24。``guided`` / ``give_images`` **只认
    规划产物**（``DirectorSegment`` 由类型派生），模型回复里另带的同类字段在
    ``parse_director_plan`` 那层就已被忽略。

    既有行里的运行态（状态、段词产物、已交视频、拒绝原因）原样保留：重建快照不能
    把「这一段已经通过了」抹掉——判据是磁盘上的段表 + 快照里的进度两份事实。
    """
    from ..director.frames import FPS
    from ..director.prompts import load_segment_assets, segment_path

    prior = {
        int(row.get("index") or 0): row
        for row in existing or ()
        if isinstance(row, Mapping)
    }
    try:
        assets_by_segment = load_segment_assets(directory)
    except (OSError, ValueError):
        # 段内素材表坏了不该让整张段表看不见：段词与帧字段仍可展示，打包期会自己
        # 报出坏表（那里才是它的消费者）。这里只降级为空子集。
        assets_by_segment = {}
    rows: list[dict[str, Any]] = []
    for segment in plan.segments:
        previous = prior.get(segment.index, {})
        prompt_file = segment_path(directory, segment.index)
        rows.append({
            "index": segment.index,
            "start_s": int(segment.start_frame / FPS),
            "end_s": int(segment.end_frame / FPS),
            "shots_in_segment": [],
            "summary": segment.summary,
            "end_hook": segment.end_hook,
            "status": str(previous.get("status") or SEGMENT_STATUS_PENDING),
            "prompt_artifact_id": str(
                previous.get("prompt_artifact_id")
                or (f"{session_id}/{director_segment_prompt_name(segment.index)}"
                    if prompt_file.is_file() else "")),
            "video_asset_id": str(previous.get("video_asset_id") or ""),
            "rejection_reason": str(previous.get("rejection_reason") or ""),
            "assets": [
                {
                    "number": int(entry.get("new", 0)),
                    "name": str(entry.get("name") or ""),
                    "original_number": int(entry.get("original", 0)),
                }
                for entry in assets_by_segment.get(segment.index, ())
            ],
            "start_frame": segment.start_frame,
            "end_frame": segment.end_frame,
            "declared_frames": segment.declared_frames,
            "export_frames": segment.export_frames,
            "segment_type": segment.type,
            "guided": bool(segment.guided),
            "give_images": list(segment.give_images),
        })
    return rows



class JobRunner:
    """一个进程一份；按 ``session_id`` 串行化命令，按 ``job_id`` 管取消标记。"""

    def __init__(self, store: WorkbenchSessionStore, config: Any = None, *,
                 fake_pipeline: bool = False) -> None:
        self.store = store
        self._config = config
        # 桩管线开关（D4 spec §11）：只由 ``create_app`` 从环境变量读出后传进来，
        # 没有任何请求能改它。为真时阶段 1/2 与修订都走 ``fake_pipeline`` 的固定产出，
        # 真实路径一行不碰。
        self.fake_pipeline = fake_pipeline
        self._locks: dict[str, asyncio.Lock] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started_at: dict[str, str] = {}

    @property
    def config(self) -> Any:
        if self._config is None:
            # 延迟导入：API 启动不该为整份项目配置与管线付导入成本（同 app.py 的手法）。
            from ..config import config as project_config

            self._config = project_config
        return self._config

    # ------------------------------------------------------------------
    # 命令入口
    # ------------------------------------------------------------------

    async def advance(
        self, session_id: str, *, idempotency_key: str | None = None
    ) -> tuple[JobSummary, bool]:
        """起一次推进；返回 ``(job, 是否幂等重放)``。

        命令串行化由每会话一把 ``asyncio.Lock`` 保证（两个并发 advance 只放一个进
        临界区），「闸门判定 + 写成 running」再放进存储层的线程锁内，与执行器的
        取消/收尾写互斥——两道锁管的是两类并发，缺一都会丢更新或双跑。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            registry = None
            if idempotency_key:
                registry = self.store.session_registry(session_id)
                cached = await asyncio.to_thread(
                    registry.replay, idempotency_key, fingerprint=_JOB_FINGERPRINT)
                if cached is not None:
                    return JobSummary.model_validate(cached), True

            # 闸门与「写成 running」必须在**存储层的会话锁内**一起完成：执行器的
            # 取消/收尾写在别的线程，先读后写会把刚落的终态覆盖回 running。
            job = JobSummary(
                job_id=uuid.uuid4().hex, kind="advance", status="running",
                started_at=_now())

            def _start(current: Any) -> dict[str, Any]:
                self.store.ensure_can_advance(current)
                # 导演台 i2v/fl2v 的开场首帧是规划前置（D8 §7.3）：起跑闸门放在写
                # running 之前，缺图时会话仍停在原状态、可续。
                self._ensure_director_start_image(session_id)
                return {
                    "status": "running",
                    "jobs": _upsert_job(current.jobs, _job_dict(job)),
                }

            await asyncio.to_thread(
                self.store.mutate, session_id, _start,
                events=[EventDraft("job.started", {"kind": job.kind},
                                   job_id=job.job_id)])
            await self._launch(session_id, job)
            if idempotency_key and registry is not None:
                await asyncio.to_thread(
                    registry.record, idempotency_key,
                    fingerprint=_JOB_FINGERPRINT, response=_job_dict(job))
            return job, False

    async def reopen_segment(
        self, session_id: str, index: int, *, idempotency_key: str | None = None
    ) -> tuple[ActionItemOut | None, bool]:
        """把一条**已通过**的段打回，重建下一步（#258 ref / #265 导演台）。

        spec §5.5 要求 ref 的已通过段能「换一段」，§12 又写「不新增端点」——用户
        2026-11-13 裁定：**新增段级端点**。两条新路线的语义**不同构**：

        - ``ref``：只换本段视频——段词与段内参考素材子集原样不动，重交后重过双闸门；
        - ``director``：**级联**——第 N..末段的通过行作废、插件段缓存与旧末帧派生件
          一并删掉（``director.cascade.cascade_rerun``，与 CLI 同一份实现），段状态
          回到 ``pending``。按需词（``tailframe-NN.md`` 等）由随后起的段 job 按盘
          重生成——这里同步写模型词会让一次点击阻塞几分钟。

        base 两条边都不开：中间段换掉会断掉第 N+1 段的帧锚（ADR 0023）。

        返回值：ref 立刻挂出的 ``segment_video`` 待办；导演台没有立刻可返回的待办
        （下一步是先产按需词），返回 ``None``，刷新由快照/SSE 接力。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            payload = _reopen_fingerprint(index)
            registry = None
            if idempotency_key:
                registry = self.store.session_registry(session_id)
                cached = await asyncio.to_thread(
                    registry.replay, idempotency_key, fingerprint=payload)
                if cached is not None:
                    # 空对象是导演台的「无待办」重放结果；ref 一定是一份完整待办。
                    return (
                        ActionItemOut.model_validate(cached) if cached else None), True

            # 路线是会话属性、不会中途改：读一次用来决定「这次换段起不起任务」。
            # 权威判定仍在下面的 ``_apply`` 里（会话锁内），这里只选事件与返回形态。
            before = await asyncio.to_thread(self.store.get_session, session_id)
            director = is_director_mode(before.brief.mode)

            item = ActionItemOut(
                item_id=uuid.uuid4().hex,
                kind=step_machine.ACTION_SEGMENT_VIDEO,
                status="pending", created_at=_now(), segment_index=index)
            job = (
                JobSummary(job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT,
                           status="running", started_at=_now())
                if director else None)

            def _apply(detail: Any) -> dict[str, Any]:
                mode = detail.brief.mode
                # 路线先判：base 会话压根不该有这个动作，回「段不存在」会把人带偏。
                if not (mode and (is_ref_mode(mode) or is_director_mode(mode))):
                    raise SegmentResubmitUnsupported(
                        "base 会话不能换段：中间段换掉会断掉下一段的帧锚（ADR 0023）。")
                if detail.status == step_machine.STATUS_RUNNING:
                    raise SessionBusy("会话有任务在跑，等它停下再换段。")
                rows = [row.model_dump() for row in detail.segments]
                target = next(
                    (row for row in rows if int(row["index"]) == int(index)), None)
                if target is None:
                    raise SegmentNotFound(f"会话里没有第 {index} 段。")
                if target["status"] != SEGMENT_STATUS_PASSED:
                    raise SegmentNotPassed(
                        f"第 {index} 段当前是 {target['status']}，只有已通过的段能换一段。")

                if is_ref_mode(mode):
                    # 换段只换视频：段词与段内子集不动，所以只清素材引用与拒绝原因。
                    target["status"] = SEGMENT_STATUS_WAITING_VIDEO
                    target["video_asset_id"] = ""
                    target["rejection_reason"] = ""
                    # 每段恰好一个待办：同类未决旧项先收掉再挂新的（与段收尾同一条纪律）。
                    items = [
                        existing.model_dump(mode="json")
                        for existing in detail.action_items
                        if not (existing.kind == step_machine.ACTION_SEGMENT_VIDEO
                                and existing.status == "pending")]
                    items.append(item.model_dump(mode="json"))
                    return {
                        "status": step_machine.STATUS_AWAITING_HUMAN,
                        "segments": rows,
                        "action_items": items,
                    }

                assert job is not None
                # 破坏性部分：作废账行 + 删缓存 + 删旧末帧派生件，与 CLI 同源。
                self._cascade_director_segments(
                    self.store.session_directory(session_id), detail.brief,
                    index, len(rows))
                for row in rows:
                    if int(row["index"]) < int(index):
                        continue
                    row["status"] = SEGMENT_STATUS_PENDING
                    row["video_asset_id"] = ""
                    row["rejection_reason"] = ""
                # 第 N 段之后的待办是**旧锚点**算出来的：清掉，让段 job 从第 N 段
                # 重新判「缺哪份按需词 / 该挂哪种待办」。留着会让界面同时挂着两段的表单。
                # 交付待办（#266）一并收掉：它建立在「全部段通过」之上，段被打回后那个
                # 前提不成立了——留着的后果是重跑完最后一段又挂一个，界面出现两个交付入口。
                def _drop(existing: Any) -> bool:
                    if existing.status != "pending":
                        return False
                    if existing.kind == step_machine.ACTION_DIRECTOR_DELIVERY:
                        return True
                    return (existing.segment_index is not None
                            and int(existing.segment_index) >= int(index))

                items = [
                    existing.model_dump(mode="json")
                    for existing in detail.action_items
                    if not _drop(existing)]
                return {
                    "status": step_machine.STATUS_RUNNING,
                    "segments": rows,
                    "action_items": items,
                    "jobs": _upsert_job(detail.jobs, _job_dict(job)),
                }

            events = (
                [EventDraft("job.started", {"kind": job.kind}, job_id=job.job_id)]
                if job is not None else
                [EventDraft(
                    "action_item.created",
                    {"item_id": item.item_id, "kind": item.kind,
                     "segment_index": index}, job_id=None)])
            await asyncio.to_thread(
                self.store.mutate, session_id, _apply, events=events)
            if job is not None:
                # 段 job 走 #264 现成的按盘判据：首帧词→段首图待办→尾帧词→视频待办。
                await self._launch(session_id, job)
            if idempotency_key and registry is not None:
                await asyncio.to_thread(
                    registry.record, idempotency_key, fingerprint=payload,
                    # 导演台没有待办可重放：记一个空对象，重发仍返回 null。
                    response=(item.model_dump(mode="json") if job is None else {}))
            return (None if job is not None else item), False

    def _director_cache_directory(self, brief: Any) -> Path | None:
        """段缓存目录；定位不到返回 ``None``（跳过删缓存，不阻断作废）。

        缓存只用于定位（#112 起证据是用户贴回的成片视频），配置缺失/路径非法不该让
        「这段通没通过」的判定失败。但**越界**是另一回事：目录解析后跑出配置根说明它
        可能被符号链接引到别处，删下去就是删别人的文件——那一条必须 400 而不是跳过。
        """
        from ..director import cache

        reader = getattr(self.config, "director_comfy_output_root", None)
        if reader is None:
            return None
        try:
            root = Path(reader())
            directory = cache.cache_dir(
                root, brief.director_cache_name, brief.director_node_id)
        except (OSError, ValueError):
            return None
        _require_cache_inside_root(root, directory)
        return directory

    def _cascade_director_segments(
        self, directory: Path, brief: Any, index: int, total: int,
    ) -> None:
        """导演台换段的破坏性部分：与 CLI 同一份 ``cascade_rerun``（#265）。

        ``cascade_rerun`` 自己会先全量校验再删；这里只把路径越界翻成稳定错误码。
        段号已在快照里校验过（1..总数），所以剩下的 ``ValueError`` 只可能来自路径。
        """
        from ..director import cascade

        cache_directory = self._director_cache_directory(brief)
        try:
            cascade.cascade_rerun(
                directory, from_segment=index, total_segments=total,
                cache_directory=cache_directory,
                reason=f"用户换第 {index} 段（工作台）")
        except ValueError as exc:
            raise CacheOutOfRoot(f"级联重跑被拒，没有删除任何文件：{exc}") from exc

    async def resolve(
        self, session_id: str, item_id: str, *, decision: str | None,
        revisions: list[dict[str, str]] | None = None,
        frames: dict[str, str] | None = None,
        video_asset_id: str | None = None,
        asset_id: str | None = None,
        start_asset_id: str | None = None,
        end_asset_id: str | None = None,
        pack_asset_id: str | None = None,
        confirm_aspect_mismatch: bool = False,
        idempotency_key: str | None = None,
    ) -> tuple[JobSummary | None, bool]:
        """在人工待办上提交决定；返回 ``(job, 是否幂等重放)``（#219 / D4 §9）。

        决定本身（待办转 resolved、修订进累积清单、帧选择与状态）在**存储层会话锁内**
        一次落盘，事件与快照不分家；真正的重跑在后台任务里。``revise`` 的修订也在这次
        落盘里写进 ``user_revisions``——先记下用户说过的话，再花几分钟重跑；中途崩了
        也只是「没落地」，不会变成「系统没听见」。

        帧链通过审查（``frame_gate``）是唯一**不起任务**的去向：它只把人推进到第二道
        待办，返回 ``job=None``。这不是「没有发生任何事」——待办与事件都已落盘，只是
        没有可等待的执行。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            payload = _resolve_fingerprint(
                item_id, decision, list(revisions or []), frames, video_asset_id,
                asset_id, start_asset_id, end_asset_id, confirm_aspect_mismatch,
                pack_asset_id)
            registry = None
            if idempotency_key:
                registry = self.store.session_registry(session_id)
                cached = await asyncio.to_thread(
                    registry.replay, idempotency_key, fingerprint=payload)
                if cached is not None:
                    return (
                        JobSummary.model_validate(cached) if cached else None, True)

            branch = await asyncio.to_thread(
                self._resolve_branch, session_id, item_id, decision, frames)
            # 两道「只挂待办、不起任务」的闸门：base 的帧闸门与导演台的开场首帧。
            # 两者都不产生可等待的执行，所以 job 为 None，会话停在 awaiting_human。
            gate_branch = branch in (
                _BRANCH_FRAME_GATE, _BRANCH_DIRECTOR_START_GATE)
            # 闸门分支通过审查不产生任务；其余去向照旧。``job`` 为 None 时下面所有
            # 与任务相关的落盘都跳过——契约里那一段的 202 是「已受理、无执行」。
            job = None if gate_branch else JobSummary(
                job_id=uuid.uuid4().hex,
                # 素材循环的两个 job 都记 ``asset_loop``（D7 §6.2）：前端/排障按 kind
                # 就能把「出词」「校对」与外层的推进任务分开。
                kind=(JOB_KIND_ASSET_LOOP if branch == _BRANCH_ASSET_LOOP
                      else JOB_KIND_DELIVERY if branch == _BRANCH_DIRECTOR_DELIVERY
                      else "advance"),
                status="running", started_at=_now())
            # 新待办的 id 先生成：``action_item.created`` 事件要和决议在**同一次**
            # 落盘里写出，不能等 _decide 返回后再补一条事件。
            created_item_id = uuid.uuid4().hex
            # 记录下来的修订条目（带轮次）要立刻带给后台任务：稍后按它标「已落地」，
            # 重新按 (round, layer, text) 反查会把轮次对齐规则又抄一遍。
            entries: list[dict[str, Any]] = []
            # 决议时收到什么就原样留在待办上（D4 spec §7）：读详情的人要看的是
            # 「用户当时说了什么」，而不是事后由累积账反推的一份。事件也带同一份，
            # 快照落后时重放才能补回修订历史。
            recorded_revisions = [
                RevisionOut(layer=str(entry["layer"]), text=str(entry["text"]))
                for entry in revisions or []
            ]
            # 事件 payload 必须是纯 JSON（EventLog 直接 json.dumps）：模型对象留在这里，
            # 进事件的是一份扁平副本。
            event_revisions = [
                revision.model_dump(mode="json") for revision in recorded_revisions]

            holder: dict[str, Any] = {}

            def _decide(current: Any) -> dict[str, Any]:
                # 会话状态先判：不在闸门上时，任何待办 id 都不该被「决议」——先答
                # action_item_pending 才能让调用方知道该去等/去推进，而不是去翻待办表。
                if current.status != step_machine.STATUS_AWAITING_HUMAN:
                    raise ActionItemPending(
                        f"会话不在人工待办上（当前 {current.status}），不能提交决定。")
                target = next(
                    (item for item in current.action_items
                     if item.item_id == item_id), None)
                if target is None:
                    # 不属于该会话与不存在回同一码：不回不同答案以免泄漏他会话的 id。
                    raise ActionItemNotFound(f"待办不存在：{item_id}")
                if target.status != "pending":
                    raise ActionItemResolved(
                        f"待办已决议（{target.decision or target.status}），不能重复提交。")
                # 路线错配先于 kind 判据（D7 §13 / #258）：ref 不走帧链（ADR 0011），
                # frames 载荷与 frame_submission 都是 base 专属动作。只在**这一种
                # 待办**上答 409——素材待办收到 frames 仍按「载荷与 kind 错配」报
                # 422，那是用户发错了字段，不是路线问题，回路线错配会把人带偏。
                if is_ref_mode(current.brief.mode) and (
                        target.kind == step_machine.ACTION_FRAME_SUBMISSION
                        or (frames is not None
                            and target.kind == step_machine.ACTION_SEGMENT_VIDEO)):
                    raise RouteMismatch(
                        "ref 会话不走帧链：提交本段视频请用 video_asset_id。")
                now = _now()
                stage_state: dict[str, Any] | None = None
                new_item: ActionItemOut | None = None
                resolved_frames: dict[str, str] = {}
                if target.kind == step_machine.ACTION_STAGE1_REVIEW:
                    # kind 与载荷强校验（D4 §9）：审查待办只认 approve/revise，
                    # 带了 frames 就是发错了端点之外的语义。
                    if decision not in (
                            step_machine.DECISION_APPROVE,
                            step_machine.DECISION_REVISE):
                        raise FrameInvalid(
                            "stage1_review 待办需要 approve 或 revise。")
                    if frames is not None:
                        raise FrameInvalid(
                            "stage1_review 待办不接受 frames：帧槽只用于 frame_submission。")
                    if video_asset_id is not None:
                        raise FrameInvalid(
                            "stage1_review 待办不接受 video_asset_id：段视频只用于 segment_video。")
                    recorded_decision = decision
                    if (decision == step_machine.DECISION_APPROVE
                            and step_machine.needs_frame_submission_for_brief(
                                current.brief)):
                        # 同一判据、两种 kind：base 收关键帧（frame_submission），
                        # 导演台 i2v/fl2v 收开场首帧（director_start_image，D8 §5.6）。
                        new_item = ActionItemOut(
                            item_id=created_item_id,
                            kind=(step_machine.ACTION_DIRECTOR_START_IMAGE
                                  if is_director_mode(current.brief.mode)
                                  else step_machine.ACTION_FRAME_SUBMISSION),
                            status="pending", created_at=now)
                elif target.kind == step_machine.ACTION_FRAME_SUBMISSION:
                    if decision is not None:
                        raise FrameInvalid(
                            "frame_submission 待办不接受 approve/revise；请提交 frames。")
                    if frames is None:
                        raise FrameInvalid(
                            "frame_submission 待办必须带 frames（首尾槽都空表示显式跳过）。")
                    if revisions:
                        raise FrameInvalid("帧提交不接受 revisions。")
                    if video_asset_id is not None:
                        raise FrameInvalid(
                            "frame_submission 待办不接受 video_asset_id；段视频请交到 segment_video。")
                    for role in ("first", "last"):
                        # 变量名与素材循环的 ``asset_id`` 分开：两处同名会让闭包里的
                        # 后者变成未绑定的局部变量（Python 的赋值即声明）。
                        frame_asset_id = str(frames.get(role) or "").strip()
                        if frame_asset_id:
                            # 归属、存在性、类型都在这里判；错误一律 422 frame_invalid。
                            resolved_frames[role] = str(
                                self.store.resolve_frame_asset(session_id, frame_asset_id))
                    holder["frames"] = resolved_frames
                    recorded_decision = step_machine.DECISION_SUBMIT_FRAMES
                    # 帧选择随决议一起落进 stage_state：慢速读图在任务里，崩溃在
                    # 两者之间时续跑仍知道用户交了哪一对素材（而不是静默降级）。
                    session = load_session(self.store.session_directory(session_id))
                    if session is None:
                        raise RuntimeError(f"会话文件读不到：{session_id}")
                    stage_state = dict(session.stage_state)
                    stage_state[FRAME_SUBMISSION_KEY] = dict(resolved_frames)
                elif target.kind == step_machine.ACTION_SEGMENT_VIDEO:
                    # kind 与载荷强校验（D6 §8）：段视频只收 video_asset_id，别的载荷一律
                    # 错配。判据放在锁内：并发决议下只有待办仍 pending 才走得到这里。
                    if decision is not None:
                        raise SegmentVideoInvalid(
                            "segment_video 待办不接受 approve/revise；请提交 video_asset_id。")
                    if revisions:
                        raise SegmentVideoInvalid(
                            "segment_video 待办不接受 revisions。")
                    if frames is not None:
                        raise SegmentVideoInvalid(
                            "segment_video 待办不接受 frames。")
                    submitted_video = str(video_asset_id or "").strip()
                    if not submitted_video:
                        raise SegmentVideoInvalid(
                            "segment_video 待办必须带 video_asset_id。")
                    number = target.segment_index
                    if number is None:
                        raise SegmentVideoInvalid(
                            "这条 segment_video 待办缺段号，无法核对。")
                    ordered = sorted(current.segments, key=lambda item: item.index)
                    current_seg = next(
                        (item for item in ordered
                         if item.status != SEGMENT_STATUS_PASSED), None)
                    if current_seg is None:
                        raise SegmentVideoInvalid(
                            "所有段都已通过，没有待交视频的段。")
                    # 归属与 mp4 容器：不属于本会话/非 mp4 一律 segment_video_invalid（422）。
                    resource = self.store.resolve_video_asset(
                        session_id, submitted_video)
                    if int(current_seg.index) != int(number):
                        raise SegmentVideoInvalid(
                            f"第 {number} 段不是当前段：段视频只能交给轮到的这一段。")
                    if is_director_mode(current.brief.mode):
                        # 导演台的判据是**帧**（17k+5 网格），而且合并导出常常比本段长：
                        # 技术档的「秒数」闸门（base/ref 用）会把它误判成不合格。
                        # 同步判帧在这里做——AC 要 422，不能等异步任务。
                        self._judge_director_segment_video(
                            session_id, int(number), resource.path)
                    else:
                        # 段号与当前段一致 + 技术档合格（复用 check_segment_video）。
                        problems = segment_submission_problems(
                            current_index=current_seg.index, number=int(number),
                            video_path=resource.path,
                            expected_seconds=float(
                                current_seg.end_s - current_seg.start_s))
                        if problems:
                            raise SegmentVideoInvalid("；".join(problems))
                    rows = [segment.model_dump() for segment in ordered]
                    for row in rows:
                        if int(row["index"]) == int(number):
                            # 交上视频 → 异步校验排队；落盘后由 job 判三档结局。
                            row["status"] = SEGMENT_STATUS_VALIDATING
                            row["video_asset_id"] = submitted_video
                            # 重新交回＝上一轮拒绝的原因不再适用（D6 §5.4）。
                            row["rejection_reason"] = ""
                    holder["segments"] = rows
                    recorded_decision = step_machine.DECISION_SUBMIT_VIDEO
                elif target.kind == step_machine.ACTION_ASSET_IMAGE:
                    # kind 与载荷强校验（D7 §6.3）：素材图待办只收 ``asset_id``。它没有
                    # 「就这样吧」那一档——放行入口一旦存在，校对结论就只是建议。
                    if decision is not None:
                        raise AssetImageInvalid(
                            "asset_image 待办不接受 approve/revise；请提交 asset_id。")
                    if revisions:
                        raise AssetImageInvalid("asset_image 待办不接受 revisions。")
                    if frames is not None:
                        raise AssetImageInvalid("asset_image 待办不接受 frames。")
                    if video_asset_id is not None:
                        raise AssetImageInvalid(
                            "asset_image 待办不接受 video_asset_id；段视频请交到 "
                            "segment_video。")
                    submitted = str(asset_id or "").strip()
                    if not submitted:
                        raise AssetImageInvalid("asset_image 待办必须带 asset_id。")
                    number = target.asset_index
                    if number is None:
                        raise AssetImageInvalid("这条 asset_image 待办缺件号，无法核对。")
                    ledger = current.asset_ledger_snapshot()
                    current_entry = next(
                        (entry for entry in ledger
                         if entry["kind"] == KIND_PICTURE
                         and int(entry["number"]) == int(number)), None)
                    if current_entry is None:
                        raise AssetImageInvalid(
                            f"素材台账里没有第 {number} 件参考素材。")
                    # 归属与类型（不属于本会话 / 非图片）都在这里判，一律 422。
                    self.store.resolve_image_asset(session_id, submitted)
                    # 先落盘再接校对：崩在两者之间时，恢复能重跑校对而不是要求重交
                    # （用户已经交过一次的图不该再交一遍）。
                    current_entry[ASSET_PROOFREAD_KEY] = submitted
                    holder["asset_ledger"] = ledger
                    recorded_decision = step_machine.DECISION_SUBMIT_IMAGE
                elif target.kind == step_machine.ACTION_DIRECTOR_START_IMAGE:
                    # 开场首帧与全片尾帧（D8 §7.1）：两张都必须是**本会话已上传的图片
                    # 素材**，不接受本地路径（spec §4.4 的不变式 6/10）。
                    if decision != step_machine.DECISION_SUBMIT_START_IMAGE:
                        raise FrameInvalid(
                            "director_start_image 待办需要 decision=submit_start_image。")
                    if revisions:
                        raise FrameInvalid(
                            "director_start_image 待办不接受 revisions。")
                    if frames is not None:
                        raise FrameInvalid(
                            "director_start_image 待办不接受 frames；请用 start_asset_id。")
                    if video_asset_id is not None:
                        raise FrameInvalid(
                            "director_start_image 待办不接受 video_asset_id。")
                    if asset_id is not None:
                        raise FrameInvalid(
                            "director_start_image 待办不接受 asset_id；请用 start_asset_id。")
                    submitted_start = str(start_asset_id or "").strip()
                    if not submitted_start:
                        raise FrameInvalid(
                            "director_start_image 待办必须带 start_asset_id。")
                    # 归属与图片类型都在 resolve_image_asset 里判（与素材循环同一个口）。
                    start_path = self.store.resolve_image_asset(
                        session_id, submitted_start)
                    submitted_end = str(end_asset_id or "").strip()
                    end_path = (
                        self.store.resolve_image_asset(session_id, submitted_end)
                        if submitted_end else None)
                    # 画幅预检、落盘与警告都在这一处：判据只有 ``precheck_message``。
                    holder["reason"] = self._accept_director_start_image(
                        session_id, current, start_path, end_path,
                        confirm=bool(confirm_aspect_mismatch))
                    # 两个图槽都记到待办上（D8 §7.1）：一个待办收两张图，少一项就
                    # 只剩「交过图」，说不清交的是哪一对。
                    holder["start_asset_id"] = submitted_start
                    if submitted_end:
                        holder["end_asset_id"] = submitted_end
                    recorded_decision = step_machine.DECISION_SUBMIT_START_IMAGE
                elif target.kind == step_machine.ACTION_DIRECTOR_SEGMENT_IMAGE:
                    # 切镜段的段首图（D8 §5.6 / #264）：一个逐段图槽，只收 ``asset_id``。
                    if decision != step_machine.DECISION_SUBMIT_SEGMENT_IMAGE:
                        raise FrameInvalid(
                            "director_segment_image 待办需要 "
                            "decision=submit_segment_image。")
                    if revisions:
                        raise FrameInvalid(
                            "director_segment_image 待办不接受 revisions。")
                    if frames is not None:
                        raise FrameInvalid(
                            "director_segment_image 待办不接受 frames。")
                    if video_asset_id is not None:
                        raise FrameInvalid(
                            "director_segment_image 待办不接受 video_asset_id。")
                    if start_asset_id is not None or end_asset_id is not None:
                        raise FrameInvalid(
                            "director_segment_image 待办只收 asset_id，"
                            "开场首帧/全片尾帧用 director_start_image。")
                    submitted_image = str(asset_id or "").strip()
                    if not submitted_image:
                        raise FrameInvalid(
                            "director_segment_image 待办必须带 asset_id。")
                    number = target.segment_index
                    if number is None:
                        raise FrameInvalid(
                            "这条 director_segment_image 待办缺段号，无法核对。")
                    if not is_director_mode(current.brief.mode):
                        raise RouteMismatch(
                            "只有导演台会话有逐段首帧图槽。")
                    ordered = sorted(current.segments, key=lambda item: item.index)
                    current_seg = next(
                        (item for item in ordered
                         if item.status != SEGMENT_STATUS_PASSED), None)
                    if current_seg is None or int(current_seg.index) != int(number):
                        raise FrameInvalid(
                            f"第 {number} 段不是当前段：段首图只能交给轮到的这一段。")
                    from ..director.frames import (
                        FIRST_SEGMENT_TYPE, needs_first_frame_image)

                    segment_type = str(current_seg.segment_type or "")
                    if (not segment_type
                            or segment_type == FIRST_SEGMENT_TYPE
                            or not needs_first_frame_image(
                                segment_type, current.brief.director_mode)):
                        raise FrameInvalid(
                            f"第 {number} 段（{segment_type or '未知类型'}）没有段首图槽："
                            "开场首帧走 director_start_image，其余段按给图契约判。")
                    image_path = self.store.resolve_image_asset(
                        session_id, submitted_image)
                    # 路径记进本段首帧词的 sidecar（#158）：尾帧词要拿它当参考，续跑
                    # 那一轮也要靠它知道这一段该是什么画幅。
                    from ..director.firstframe import record_first_frame_image

                    record_first_frame_image(
                        self.store.session_directory(session_id), int(number),
                        image_path)
                    recorded_decision = (
                        step_machine.DECISION_SUBMIT_SEGMENT_IMAGE)
                elif target.kind == step_machine.ACTION_DIRECTOR_DELIVERY:
                    # 交付归档（D8 §12 / #266）：成片 mp4 + 最终包 zip，两个都必填。
                    # 两条校验在**这里**同步跑（AC 要 422，不能等异步 job）：包段数
                    # 与计划一致、成片宽高走 ``final_film_verdict``。``deliver`` 自己
                    # 只查成片存在与包段数，不查宽高——漏调那道判据就是静默放行。
                    if decision != step_machine.DECISION_SUBMIT_DELIVERY:
                        raise FrameInvalid(
                            "director_delivery 待办需要 decision=submit_delivery。")
                    if revisions:
                        raise FrameInvalid(
                            "director_delivery 待办不接受 revisions。")
                    if frames is not None or asset_id is not None:
                        raise FrameInvalid(
                            "director_delivery 待办不接受 frames/asset_id；"
                            "请用 video_asset_id 与 pack_asset_id。")
                    if start_asset_id is not None or end_asset_id is not None:
                        raise FrameInvalid(
                            "director_delivery 待办不接受 start_asset_id/"
                            "end_asset_id。")
                    submitted_video = str(video_asset_id or "").strip()
                    submitted_pack = str(pack_asset_id or "").strip()
                    if not submitted_video or not submitted_pack:
                        raise FrameInvalid(
                            "director_delivery 待办必须同时带 video_asset_id"
                            "（成片 mp4）与 pack_asset_id（最终包 zip）。")
                    video_resource = self.store.resolve_video_asset(
                        session_id, submitted_video)
                    pack_resource = self.store.resolve_pack_asset(
                        session_id, submitted_pack)
                    # 同步校验一：最终包能安全解、段数与计划一致（不归档）。
                    self._validate_delivery_pack(
                        session_id, pack_resource.path)
                    # 同步校验二：成片宽高。t2v 没有画布判据，直接放行。
                    self._validate_final_film(current, video_resource.path)
                    holder["video_asset_id"] = submitted_video
                    holder["pack_asset_id"] = submitted_pack
                    recorded_decision = step_machine.DECISION_SUBMIT_DELIVERY
                else:
                    raise FrameInvalid(f"不认识的人工待办类型：{target.kind}")
                holder["decision"] = recorded_decision

                resolved_update: dict[str, Any] = {
                    "status": "resolved", "decision": recorded_decision,
                    "resolved_at": now,
                    "revisions": recorded_revisions,
                }
                if holder.get("reason"):
                    # 画幅警告落在待办上（D8 §7.2 的「有地方可读」）：决议响应是
                    # ``JobSummary``，不给它长一个只服务导演台的字段。没有警告时
                    # **不动** reason——段被拒/校对差异那几类的历史文本要留着。
                    resolved_update["reason"] = holder["reason"]
                for key in ("start_asset_id", "end_asset_id",
                            "video_asset_id", "pack_asset_id"):
                    # 图槽与交付槽标识与事件同一份来源：快照与重放读到的必须是这些 id。
                    if holder.get(key):
                        resolved_update[key] = holder[key]
                items = [
                    (item.model_copy(update=resolved_update).model_dump(mode="json")
                     if item.item_id == item_id
                     else item.model_dump(mode="json"))
                    for item in current.action_items
                ]
                if new_item is not None:
                    items.append(new_item.model_dump(mode="json"))
                changes: dict[str, Any] = {"action_items": items}
                if new_item is not None:
                    # 帧链通过审查：会话停在原地等第二道待办，不写成 running。
                    changes["status"] = step_machine.STATUS_AWAITING_HUMAN
                else:
                    changes["status"] = step_machine.STATUS_RUNNING
                    if job is None:
                        # 只有帧闸门分支不建任务，而它走的是上面那条 if。
                        raise RuntimeError("内部不一致：非帧闸门分支没有任务")
                    changes["jobs"] = _upsert_job(
                        current.jobs, _job_dict(job))
                if stage_state is not None:
                    changes["stage_state"] = stage_state
                if "segments" in holder:
                    # 段视频回交：段状态在这一笔里落到 validating（视频素材 id 一并记）。
                    changes["segments"] = holder["segments"]
                if "asset_ledger" in holder:
                    # 素材图回交：台账条目记下「刚交的那张待校对图」，与决议同一笔落盘。
                    changes["asset_ledger"] = holder["asset_ledger"]
                if decision == step_machine.DECISION_REVISE:
                    # 累积语义只住 user_revisions.py（begin_round + record_user_revision）；
                    # 这里不另造轮次号，也不改清单形状。
                    from ..user_revisions import record_user_revision

                    if stage_state is None:
                        session = load_session(self.store.session_directory(session_id))
                        if session is None:
                            raise RuntimeError(f"会话文件读不到：{session_id}")
                        stage_state = dict(session.stage_state)
                    for revision in revisions or []:
                        entries.append(record_user_revision(
                            stage_state, layer=revision["layer"],
                            text=revision["text"]))
                    changes["stage_state"] = stage_state
                return changes

            resolved_payload: dict[str, Any] = {
                "item_id": item_id,
                # 事件在 mutate 之前就要构造好（先写事件、后写快照），所以这里只能按
                # 预判出的分支给 decision。导演台首帧不用单列：它是**显式**的 decision
                # 取值（``submit_start_image``），与审查待办的 approve/revise 一样
                # 直接取请求里的值；只有靠载荷分型的三种交图才要按分支改名。
                "decision": (
                    step_machine.DECISION_SUBMIT_FRAMES
                    if branch == _BRANCH_FRAMES
                    else step_machine.DECISION_SUBMIT_VIDEO
                    if branch == _BRANCH_SEGMENT_VIDEO
                    else step_machine.DECISION_SUBMIT_IMAGE
                    if branch == _BRANCH_ASSET_LOOP
                    else decision),
                "revisions": event_revisions,
            }
            if frames is not None:
                # 帧槽载荷是用户输入的一部分（D4 §9）：重放与排障要看出用户当时
                # 选了哪两个素材。审查待办没有这一项，就不写空对象进事件。
                resolved_payload["frames"] = dict(frames)
            if video_asset_id is not None:
                # 段视频素材也是用户输入：重放要看出当时交的是哪一条。
                resolved_payload["video_asset_id"] = video_asset_id
            if asset_id is not None:
                # 素材图同理：交回的是哪一张，重放要看得出来。
                resolved_payload["asset_id"] = asset_id
            if start_asset_id is not None:
                # 导演台开场首帧（D8 §7.1）：两张图都是用户输入，重放要看得出。
                resolved_payload["start_asset_id"] = start_asset_id
            if end_asset_id is not None:
                resolved_payload["end_asset_id"] = end_asset_id
            if pack_asset_id is not None:
                # 最终包素材（D8 §12 / #266）：与成片 id 一起进事件，重放才重建得出
                # 「当时交的是哪一份包」。
                resolved_payload["pack_asset_id"] = pack_asset_id
            if confirm_aspect_mismatch:
                resolved_payload["confirm_aspect_mismatch"] = True
            events = [
                EventDraft("action_item.resolved", resolved_payload,
                           job_id=job.job_id if job else None),
            ]
            if job is not None:
                events.insert(0, EventDraft(
                    "job.started", {"kind": job.kind}, job_id=job.job_id))
            if gate_branch:
                events.append(EventDraft(
                    "action_item.created",
                    {"item_id": created_item_id,
                     "kind": (step_machine.ACTION_FRAME_SUBMISSION
                              if branch == _BRANCH_FRAME_GATE
                              else step_machine.ACTION_DIRECTOR_START_IMAGE)},
                    job_id=None))

            await asyncio.to_thread(
                self.store.mutate, session_id, _decide, events=events)
            if job is None:
                if idempotency_key and registry is not None:
                    await asyncio.to_thread(
                        registry.record, idempotency_key,
                        fingerprint=payload, response={})
                return None, False
            self._started_at[job.job_id] = job.started_at
            await asyncio.to_thread(self._write_active_job, session_id, job.job_id, "")
            event = threading.Event()
            self._cancel_events[job.job_id] = event
            if branch in (_BRANCH_SEGMENT_VIDEO, _BRANCH_ASSET_LOOP,
                          _BRANCH_DIRECTOR_START_IMAGE,
                          _BRANCH_DIRECTOR_SEGMENT_IMAGE,
                          _BRANCH_DIRECTOR_DELIVERY):
                # 段校验、素材循环与导演台开场首帧都走通用分派（``_run`` 按持久化
                # 事实决定跑哪一步）：交完首帧后的下一步是导演台阶段 2（规划 + 写段），
                # 不是修订重跑阶段 1。三处各写一份子步骤判定会长出两套口径。
                coroutine = self._run(session_id, job.job_id, event)
            else:
                coroutine = self._run_decision(
                    session_id, job.job_id, event, holder["decision"], entries)
            self._tasks[job.job_id] = asyncio.create_task(coroutine)
            if idempotency_key and registry is not None:
                await asyncio.to_thread(
                    registry.record, idempotency_key,
                    fingerprint=payload, response=_job_dict(job))
            return job, False

    def _resolve_branch(self, session_id: str, item_id: str,
                        decision: str | None,
                        frames: dict[str, str] | None) -> str:
        """这次 resolve 走向哪条分支（只读预判；真正的判定在 ``_decide`` 里）。

        预判只依赖两个**建好后不再改写**的事实：待办 kind 与会话变体。所以先在锁外
        读一次不会与并发决议打架；待办是否仍 pending 由锁内的 ``_decide`` 兜底。
        """
        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            return _BRANCH_REVIEW
        target = next(
            (item for item in session.action_items
             if str(item.get("item_id")) == item_id), None)
        if target is None:
            return _BRANCH_REVIEW
        kind = str(target.get("kind"))
        if kind == step_machine.ACTION_SEGMENT_VIDEO:
            return _BRANCH_SEGMENT_VIDEO
        if kind == step_machine.ACTION_FRAME_SUBMISSION:
            return _BRANCH_FRAMES
        if kind == step_machine.ACTION_ASSET_IMAGE:
            return _BRANCH_ASSET_LOOP
        if kind == step_machine.ACTION_DIRECTOR_START_IMAGE:
            return _BRANCH_DIRECTOR_START_IMAGE
        if kind == step_machine.ACTION_DIRECTOR_SEGMENT_IMAGE:
            return _BRANCH_DIRECTOR_SEGMENT_IMAGE
        if kind == step_machine.ACTION_DIRECTOR_DELIVERY:
            return _BRANCH_DIRECTOR_DELIVERY
        if (kind == step_machine.ACTION_STAGE1_REVIEW
                and decision == step_machine.DECISION_APPROVE):
            # 走素材台账的路线通过审查先走素材循环（D7 §5.3 / #268）：ref 与导演台
            # r2v 的参考素材要逐件出词、交图、校对，跑完才轮到阶段 2（r2v 是规划）。
            if route_uses_asset_ledger(session.brief):
                return _BRANCH_ASSET_LOOP
            # 判据只有一份（D8 §6.4）：base 的帧链与导演台 i2v/fl2v 都从这里分流，
            # 只是落到不同的待办 kind。
            if step_machine.needs_frame_submission_for_brief(session.brief):
                return (_BRANCH_DIRECTOR_START_GATE
                        if session.brief.is_director else _BRANCH_FRAME_GATE)
        return _BRANCH_REVIEW

    def _director_frame_tolerance(self) -> int | None:
        """配置里的帧容差；拿不到就交 ``None`` 给 companion 用代码默认值。

        容差是**用户旋钮**（``config/agent.yaml``），代码默认值住在 companion 一处；
        测试的 runtime 桩可能没有这一项，缺了不该让判帧整条挂掉。
        """
        getter = getattr(self.config, "director_frame_tolerance", None)
        if getter is None:
            return None
        try:
            return int(getter())
        except (TypeError, ValueError):
            return None

    def _judge_director_segment_video(
        self, session_id: str, number: int, video_path: Path,
    ) -> tuple[int, Any]:
        """同步判一段导演台成片视频（D8 §10 / #264）：**唯一判据**是 companion。

        返回 ``(实际帧数, verdict)``。读不出 → ``video_unreadable``（不判 0 帧）；
        帧数不合任何合法导出形态 → ``frame_mismatch``（AC 点名「请确认贴的是本段成片」）。
        合并导出（前 N 段/整片）与单段导出都由 ``judge_video_evidence`` 认。
        """
        from ..director import cache, companion
        from ..director.planning import plan_path, read_plan_json

        directory = self.store.session_directory(session_id)
        try:
            plan = read_plan_json(plan_path(directory))
        except (OSError, ValueError) as exc:
            raise SegmentVideoInvalid(
                f"导演台段表读不回来，无法判帧：{exc}") from exc
        segment = next(
            (item for item in plan.segments if item.index == int(number)), None)
        if segment is None:
            raise SegmentVideoInvalid(f"段表里找不到第 {number} 段。")
        try:
            actual = cache.actual_frames(video_path)
        except ValueError as exc:
            # 读不出与「不是这一段」是两种结论（``actual_frames`` 的同一取舍）。
            raise VideoUnreadable(str(exc)) from exc
        verdict = companion.judge_video_evidence(
            segment, actual_frames=actual, plan=plan,
            tolerance=self._director_frame_tolerance())
        if not verdict.passed:
            message = verdict.message
            # AC 要求文案点名这一句；judge 对「不是本段」的几支已经写了，帧数不等
            # 那一支没有——在这里补齐，不另写一份判据。
            if "请确认贴的是本段成片" not in message:
                message = f"{message} 请确认贴的是本段成片。"
            raise FrameMismatch(message)
        return actual, verdict

    def _accept_director_start_image(
        self, session_id: str, current: Any, start_path: Path,
        end_path: Path | None, *, confirm: bool,
    ) -> str:
        """收下开场首帧与全片尾帧并落到导演包认的位置（D8 spec §7）。

        画幅判据只有 ``precheck_message`` 一份：比例超差时**收下并警告**，但决议必须
        带 ``confirm_aspect_mismatch=True``——spec §7.2 不选「上传即 422」，因为用户
        可能就是要有意用这个比例。返回给人看的警告文本（比例相符时为空串）。

        落点与 CLI 同一个词干、同一个函数（``store_director_still``）：首帧进
        ``director/assets/01/start.*``，全片尾帧进 ``director/assets/film-end.*``
        ——后者**不进第 1 段目录**，否则会被当成第 1 段的终点画面。
        """
        from ..director.aspect import image_size
        from ..director.aspect_flow import precheck_message
        from ..director.pack import FILM_END_STEM, START_STEM
        from ..ui.wizard_director import store_director_still

        warning = ""
        size = image_size(start_path)
        declared = self._director_film_size(current.brief)
        # 读不出宽高（损坏/非图片）按「元信息缺失」放行：上传时的嗅探已经验过类型，
        # 这里再拦一次只会把一张坏图说成「比例不符」（同 ``aspect.image_size`` 的口径）。
        if size is not None and declared is not None:
            message = precheck_message(
                str(current.brief.director_mode), frame_canvas=size,
                declared=declared)
            if message:
                if not confirm:
                    raise AspectMismatchUnconfirmed(message)
                warning = message
        directory = self.store.session_directory(session_id)
        store_director_still(start_path, directory, START_STEM)
        if end_path is not None:
            store_director_still(end_path, directory, FILM_END_STEM, segment=None)
        return warning

    def _director_film_size(self, brief: Any) -> tuple[int, int] | None:
        """成片画幅（宽, 高）：优先会话里选定的那一档，旧会话回落 ``director`` 节。

        与 CLI 的 ``film_settings`` 同一条读法（#161 / ADR 0028）：``Brief.canvases``
        里挂着成片画幅时以它为准；没有（旧会话或没选）才读配置。配置对象拿不到
        ``director_settings`` 时返回 ``None``（预检不可判就跳过，不猜一个画幅）。
        """
        from ..director.film_canvas import film_size_for

        film = film_size_for(brief)
        if film is not None:
            return film
        settings_fn = getattr(self.config, "director_settings", None)
        if settings_fn is None:
            return None
        settings = settings_fn()
        return int(settings.width), int(settings.height)

    def _ensure_director_start_image(self, session_id: str) -> None:
        """导演台 i2v/fl2v 起跑前的开场首帧闸门（D8 spec §7.3 / §15）。

        只在「下一次会进阶段 2」的会话上判：刚建的会话先跑阶段 1，那时还没有首帧
        可言。判据复用打包器的那一份（``opening_start_still_missing``），扩展名偏好
        与落点目录不会与打包分家。
        """
        session = load_session(self.store.session_directory(session_id))
        if session is None or not session.brief.is_director:
            return
        from ..director.aspect_flow import PRECHECK_MODES
        from ..director.pack import opening_start_still_missing

        mode = session.brief.director_mode
        if mode not in PRECHECK_MODES:
            return
        if self._resume_step(session_id) != step_machine.STEP_STAGE2:
            return
        missing = opening_start_still_missing(
            self.store.session_directory(session_id), mode)
        if missing is not None:
            raise StartImageRequired(
                f"{mode} 会话没有开场首帧：应放在 {missing}"
                "（接受 png/jpg/jpeg/webp）。导入导演台的包缺这张图会被插件直接拒绝。")


    def _validate_delivery_pack(self, session_id: str, pack_path: Path) -> None:
        """同步核对最终包：能安全解、含 ``timeline.json``、段数与计划一致（#266）。

        用 #263 的 ``extract_archive_zip`` 在临时目录解包，而不是直接读 zip 里的成员：
        解包器的路径判据（绝对路径 / ``..`` / 符号链接）只差这一处复用，另写一份
        读取就会让「包安全」这件事有两条口径。段数比的是**计划**的段数——计划是这次
        打包的依据，成交包里多一段少一段都说明它不属于本次交付。
        """
        import tempfile

        from ..director.pack import TIMELINE_FILENAME
        from ..director.planning import plan_path, read_plan_json
        from ..workbench_store import extract_archive_zip

        directory = self.store.session_directory(session_id)
        try:
            plan = read_plan_json(plan_path(directory))
        except (OSError, ValueError) as exc:
            raise ArchiveInvalid(
                f"导演台段表读不回来，无法核对最终包：{exc}") from exc
        with tempfile.TemporaryDirectory() as temp_dir:
            extract_archive_zip(pack_path, temp_dir)
            timeline_file = Path(temp_dir) / TIMELINE_FILENAME
            if not timeline_file.is_file():
                raise ArchiveInvalid(
                    f"最终包缺 {TIMELINE_FILENAME}：{pack_path}"
                    "（不是导演台导出的包？）")
            try:
                data = json.loads(timeline_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArchiveInvalid(
                    f"最终包的 {TIMELINE_FILENAME} 读不出：{exc}") from exc
        rows = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ArchiveInvalid(
                f"最终包的 {TIMELINE_FILENAME} 缺 segments 数组（或为空）")
        if len(rows) != len(plan.segments):
            raise ArchiveInvalid(
                f"最终包段数 {len(rows)} 与计划 {len(plan.segments)} 段不一致："
                "段数变了说明这不是本次交付的那份包，比对与归档都不做")

    def _validate_final_film(self, current: Any, video_path: Path) -> None:
        """同步核对成片宽高：判据只走 ``final_film_verdict``（#266 / spec §12.3）。

        ``deliver`` **不查宽高**——它只查成片存在、初始包段数、比对与归档。漏调这道
        判据不会报错，只会把一条画幅不符的片子归档成「已交付」。``ask=None``：工作台
        没有交互入口，尺寸差一档时也按停止处理（与 CLI 的「没有入口就停」同一条）。
        """
        from ..director.aspect_flow import final_film_verdict

        ok, note = final_film_verdict(
            mode=str(current.brief.director_mode), video=video_path,
            brief=current.brief, session_created_at=str(current.created_at or ""),
            stage_state=current.stage_state_snapshot(), ask=None)
        if not ok:
            raise FinalFilmMismatch(
                note or "成片宽高与画布不符：没有归档最终包（未通过画幅核对）。")

    def get_job(self, session_id: str, job_id: str) -> JobSummary:
        detail = self.store.get_session(session_id)
        job = _find_job(detail.jobs, job_id)
        if job is None:
            raise JobNotFound(f"任务不存在：{job_id}")
        return job

    async def cancel(self, session_id: str, job_id: str) -> JobSummary:
        return await asyncio.to_thread(self._request_cancel, session_id, job_id)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def _launch(self, session_id: str, job: JobSummary) -> None:
        """登记在跑标记并起后台任务。

        显式 ``advance`` 与长片阶段 2 后自动接上的分段规划共用这一处：两条路都要
        「先写 active-job、再登记取消事件、最后起任务」，抄第二份会漏掉其中一步。
        """
        self._started_at[job.job_id] = job.started_at
        await asyncio.to_thread(self._write_active_job, session_id, job.job_id, "")
        event = threading.Event()
        self._cancel_events[job.job_id] = event
        self._tasks[job.job_id] = asyncio.create_task(
            self._run(session_id, job.job_id, event))

    async def _run(self, session_id: str, job_id: str,
                   cancel_event: threading.Event) -> None:
        # 续跑落点由**已完成到哪**决定，不能一律从阶段 1 重进：#221 真实跑在阶段 2
        # 被网关卡住后强杀，重启再 advance 若回到阶段 1，只会重新开一个人工闸门
        # （阶段 1 已跑完、链已耗尽），阶段 2 永远走不到。
        step = await asyncio.to_thread(self._resume_step, session_id)
        # 三档返回的形状不同（阶段 1/2 是 state 字典，分段步骤是各自的结果字典）：
        # 用 Any 承接，分派处按 step 决定怎么消费。
        result: Any
        # ``segmenting`` 内部有三种子步骤（规划/写词/校验），按**已落盘的事实**决定。
        seg_step: tuple[str, int] = (SEGMENT_STEP_DONE, 0)
        try:
            if step == step_machine.STEP_STAGE2:
                result = await asyncio.to_thread(
                    self._execute_stage2, session_id, job_id, cancel_event)
            elif step == step_machine.STEP_ASSET_LOOP:
                asset_step, number = await asyncio.to_thread(
                    self._asset_step, session_id)
                if asset_step == ASSET_STEP_PROOFREAD:
                    result = await asyncio.to_thread(
                        self._execute_asset_proofread,
                        session_id, job_id, cancel_event, number)
                elif asset_step == ASSET_STEP_WRITE:
                    result = await asyncio.to_thread(
                        self._execute_asset_write,
                        session_id, job_id, cancel_event, number)
                else:
                    # ``_resume_step`` 只在循环没走完时给出 ``asset_loop``；真到了
                    # 「已完成」说明两处判据打架，按失败收场而不是静默跳过。
                    raise RuntimeError("素材循环已全部定稿，不该再起 asset_loop 任务")
            elif step == step_machine.STEP_PACKING:
                result = await asyncio.to_thread(
                    self._execute_pack, session_id, job_id, cancel_event)
            elif step == step_machine.STEP_SEGMENTING:
                seg_step = await asyncio.to_thread(self._segment_step, session_id)
                kind, number = seg_step
                if kind == SEGMENT_STEP_PLANNING:
                    result = await asyncio.to_thread(
                        self._execute_segment_planning, session_id, job_id, cancel_event)
                elif kind == SEGMENT_STEP_WRITE:
                    result = await asyncio.to_thread(
                        self._execute_segment_write, session_id, job_id, cancel_event, number)
                elif kind == SEGMENT_STEP_VALIDATE:
                    result = await asyncio.to_thread(
                        self._execute_segment_validation,
                        session_id, job_id, cancel_event, number)
                elif kind == SEGMENT_STEP_ASSEMBLE:
                    result = await asyncio.to_thread(
                        self._execute_segment_assemble, session_id, job_id, cancel_event)
                elif kind == DIRECTOR_STEP_FIRSTFRAME:
                    result = await asyncio.to_thread(
                        self._execute_director_firstframe,
                        session_id, job_id, cancel_event, number)
                elif kind == DIRECTOR_STEP_TAILFRAME:
                    result = await asyncio.to_thread(
                        self._execute_director_tailframe,
                        session_id, job_id, cancel_event, number)
                elif kind == DIRECTOR_STEP_FINALIZE:
                    result = await asyncio.to_thread(
                        self._execute_director_finalize,
                        session_id, job_id, cancel_event, number)
                elif kind == DIRECTOR_STEP_DELIVERY:
                    result = await asyncio.to_thread(
                        self._execute_director_delivery,
                        session_id, job_id, cancel_event)
                elif kind in (DIRECTOR_STEP_SEGMENT_IMAGE,
                              DIRECTOR_STEP_WAIT_VIDEO):
                    result = {"number": number, "todo_kind": kind}
                else:
                    result = None
            else:
                result = await asyncio.to_thread(
                    self._execute_stage1, session_id, job_id, cancel_event)
        except JobCancelled as exc:
            await asyncio.to_thread(
                self._finish_cancelled, session_id, job_id, exc.node)
        except Exception as exc:  # noqa: BLE001 - 任务失败要落成 job 状态，不能让后台任务静默死掉
            logger.exception("执行任务失败：%s / %s", session_id, job_id)
            await asyncio.to_thread(
                self._finish_failed, session_id, job_id, error_code(exc), str(exc))
        else:
            if step == step_machine.STEP_STAGE2:
                next_job = await asyncio.to_thread(
                    self._finish_stage2, session_id, job_id, result)
            elif step == step_machine.STEP_ASSET_LOOP:
                if result["step"] == ASSET_STEP_PROOFREAD:
                    next_job = await asyncio.to_thread(
                        self._finish_asset_proofread, session_id, job_id, result)
                else:
                    await asyncio.to_thread(
                        self._finish_asset_write, session_id, job_id, result)
                    next_job = None
            elif step == step_machine.STEP_PACKING:
                # 打包成功：导演台立刻接段循环的第一件（按需词/挂待办），与阶段 2
                # 接打包同形——新任务的 ``job.started`` 与旧任务的 ``succeeded``
                # 在同一次落盘里写出（#264）。
                next_job = await asyncio.to_thread(
                    self._finish_pack, session_id, job_id, result)
            elif step == step_machine.STEP_SEGMENTING:
                kind = seg_step[0]
                if kind == SEGMENT_STEP_PLANNING:
                    next_job = await asyncio.to_thread(
                        self._finish_segment_planning, session_id, job_id, result)
                elif kind == SEGMENT_STEP_WRITE:
                    next_job = await asyncio.to_thread(
                        self._finish_segment_write, session_id, job_id, result)
                elif kind == SEGMENT_STEP_VALIDATE:
                    next_job = await asyncio.to_thread(
                        self._finish_segment_validation, session_id, job_id, result)
                elif kind == SEGMENT_STEP_ASSEMBLE:
                    next_job = await asyncio.to_thread(
                        self._finish_segment_assembly, session_id, job_id, result)
                elif kind == DIRECTOR_STEP_FIRSTFRAME:
                    next_job = await asyncio.to_thread(
                        self._finish_director_firstframe,
                        session_id, job_id, result)
                elif kind == DIRECTOR_STEP_TAILFRAME:
                    next_job = await asyncio.to_thread(
                        self._finish_director_tailframe,
                        session_id, job_id, result)
                elif kind == DIRECTOR_STEP_FINALIZE:
                    next_job = await asyncio.to_thread(
                        self._finish_director_finalize,
                        session_id, job_id, result)
                elif kind == DIRECTOR_STEP_DELIVERY:
                    next_job = await asyncio.to_thread(
                        self._finish_director_delivery,
                        session_id, job_id, result)
                elif kind in (DIRECTOR_STEP_SEGMENT_IMAGE,
                              DIRECTOR_STEP_WAIT_VIDEO):
                    next_job = await asyncio.to_thread(
                        self._finish_director_todo,
                        session_id, job_id, result)
                else:
                    next_job = await asyncio.to_thread(
                        self._finish_segment_done, session_id, job_id)
            else:
                await asyncio.to_thread(
                    self._finish_succeeded, session_id, job_id, result)
                next_job = None
            # 长片：收尾时已把下一个任务写进快照，这里起它的执行线程。
            if next_job is not None:
                await self._launch(session_id, next_job)
        finally:
            await asyncio.to_thread(self._cleanup, session_id, job_id)

    def _resume_step(self, session_id: str) -> str:
        """下次 advance 该跑阶段 1、阶段 2 还是分段规划：看**已落盘的事实**，不看内存。

        判据两条同时成立才进阶段 2：阶段 1 已过闸门（存在 stage1_review 待办）、
        最新待办已被 approve 决议；有未决的新待办（revise 跑完会挂新待办）仍属阶段 1。
        长片在阶段 2 成功之后、段表落盘之前是第三档（``segmenting``）。
        """
        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            return step_machine.STEP_STAGE1
        pending = any(
            str(item.get("kind")) == step_machine.ACTION_STAGE1_REVIEW
            and str(item.get("status")) == "pending"
            for item in session.action_items)
        # 「阶段 1 已完成」看**待办存在**，不看 `_progress`：阶段 1 收尾时会用最终
        # state 覆写快照，而最终 state 里没有 `_progress`（那是续跑元数据）——拿它判
        # 会得出「没跑过」，把一个阶段 2 中断的会话重新拖回阶段 1 从头跑。
        reviewed = any(
            str(item.get("kind")) == step_machine.ACTION_STAGE1_REVIEW
            for item in session.action_items)
        approved = any(
            str(item.get("kind")) == step_machine.ACTION_STAGE1_REVIEW
            and str(item.get("status")) == "resolved"
            and str(item.get("decision")) == step_machine.DECISION_APPROVE
            for item in session.action_items)
        if reviewed and approved and not pending:
            # 素材循环（D7 §5.3 / §14）：走台账的路线在阶段 1 与阶段 2 之间多一档。
            # 判据只读台账——重启后照样能重建「在等第几件」，不依赖 job 历史。
            # 导演台 r2v 也走这一档（#268）：它的阶段 2 是规划，规划要吃全片的英文
            # 定义块，而定义块的素材来自素材循环（顺序不能倒）。t2v/i2v/fl2v 没有
            # 台账，`_asset_step_of` 对空台账恒 DONE，不会给它们凭空多开一档。
            if (route_uses_asset_ledger(session.brief)
                    and _asset_step_of(session.asset_ledger)[0] != ASSET_STEP_DONE):
                return step_machine.STEP_ASSET_LOOP
            # 导演台的阶段 2 只做规划 + 写段；写完就该打包（#263）。zip 已落盘
            # 之后进入段循环（#264）——判据全在磁盘（段表 + 每段段词 + zip），
            # 重启后照样知道「该打包还是该跑段」。
            if session.brief.is_director:
                if not self._director_segments_ready(directory):
                    return step_machine.STEP_STAGE2
                from ..director.pack import zip_path_for

                if (zip_path_for(directory).is_file()
                        and self._director_briefings_ready(directory)):
                    return step_machine.STEP_SEGMENTING
                return step_machine.STEP_PACKING
            # 长片的两段接缝都属于 ``segmenting``：① 阶段 2 已成功（``final_prompt``
            # 落盘）但还没段表——重试要重跑**规划**；② 段循环已开始（有段表）——重试要
            # 重跑当前那一段的写词或校验。两者都不能把阶段 2 再跑一遍（D6 spec §5.3）。
            if self._needs_segment_loop(session.brief):
                if session.segments:
                    return step_machine.STEP_SEGMENTING
                if str(session.stage_state.get("final_prompt") or "").strip():
                    return step_machine.STEP_SEGMENTING
            return step_machine.STEP_STAGE2
        return step_machine.STEP_STAGE1

    @staticmethod
    def _director_segments_ready(directory: Path) -> bool:
        """导演台阶段 2 是否已把段表与每段段词写齐（读盘，不读 job 记录）。

        ``plan.json`` 在这里只**宽松**数段数：坏表（未知类型/帧数不自洽）由打包器
        严格读回时点名失败，续跑判定不该先替它下结论、也不该在 resume 阶段抛。
        """
        from ..director.planning import plan_path
        from ..director.prompts import segment_path

        plan_file = plan_path(directory)
        if not plan_file.is_file():
            return False
        try:
            data = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        raw = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw:
            return False
        return all(segment_path(directory, position).is_file()
                   for position in range(1, len(raw) + 1))

    @staticmethod
    def _director_briefings_ready(directory: Path) -> bool:
        """打包步骤是否收尾完整：每段一份简报都在，才算打包真的做完（#263 / #264）。

        zip 早于简报落盘：崩在两者之间时，续跑要回到 ``packing`` 只补简报——直接
        进段循环会把「用户该照抄的清单」永久漏掉。
        """
        from ..director.planning import plan_path

        try:
            data = json.loads(plan_path(directory).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        raw = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw:
            return False
        return all(
            (directory / director_briefing_name(position)).is_file()
            for position in range(1, len(raw) + 1))

    @staticmethod
    def _needs_segment_loop(brief: Any) -> bool:
        """阶段 2 之后接不接段循环：门槛是**路线属性**，不是会话属性（D7 §5.4）。

        base 只在 >10 秒时进分段（D6 §3，不动）；ref 一律进——≤ 单次生成上限的会话
        走「1 段」直出，它仍是同一个段循环（同一套待办、双闸门与拼接），不是长片。

        导演台**不在这里**：它的阶段 2 只做规划 + 写段，打包与段循环是后面的独立
        步骤（#263 / #264）。照 base 的时长阈值把它塞进 ``segmenting``，会让还没
        规划过的会话走 base 的分段规划器——那是另一张段表。
        """
        if brief.is_director:
            return False
        return brief.is_ref or step_machine.is_long_form(brief.duration)

    def _cleanup(self, session_id: str, job_id: str) -> None:
        self._cancel_events.pop(job_id, None)
        self._tasks.pop(job_id, None)
        self._started_at.pop(job_id, None)
        try:
            self._remove_active_job(session_id, job_id)
        except Exception:  # noqa: BLE001 - 清理失败不该盖掉任务终态
            logger.warning("清理在跑标记失败：%s / %s", session_id, job_id)

    async def _run_decision(self, session_id: str, job_id: str,
                            cancel_event: threading.Event, decision: str,
                            entries: list[dict[str, Any]]) -> None:
        """决议的后台执行：approve → 阶段 2；revise → 重跑阶段 1；交帧 → 阶段 2。

        帧链通过审查不走到这里（那条分支没有任务，见 ``resolve`` 的 ``frame_gate``）。
        """
        try:
            if decision in (step_machine.DECISION_APPROVE,
                            step_machine.DECISION_SUBMIT_FRAMES):
                result = await asyncio.to_thread(
                    self._execute_stage2, session_id, job_id, cancel_event)
            else:
                result = await asyncio.to_thread(
                    self._execute_revise, session_id, job_id, cancel_event, entries)
        except JobCancelled as exc:
            await asyncio.to_thread(
                self._finish_cancelled, session_id, job_id, exc.node)
        except Exception as exc:  # noqa: BLE001 - 任务失败要落成 job 状态
            logger.exception("执行决议失败：%s / %s", session_id, job_id)
            await asyncio.to_thread(
                self._finish_failed, session_id, job_id, error_code(exc), str(exc))
        else:
            if decision in (step_machine.DECISION_APPROVE,
                            step_machine.DECISION_SUBMIT_FRAMES):
                next_job = await asyncio.to_thread(
                    self._finish_stage2, session_id, job_id, result)
                if next_job is not None:
                    await self._launch(session_id, next_job)
            else:
                # 修订跑完回到**同一个人工闸门**：新待办、状态 awaiting_human。
                await asyncio.to_thread(
                    self._finish_succeeded, session_id, job_id, result["state"])
        finally:
            await asyncio.to_thread(self._cleanup, session_id, job_id)

    def _ref_brief(self, session: Any) -> Any:
        """执行器交给管线的 Brief：走台账的会话的 ``refs`` 从**素材台账**现造（D7 §5.3 / §10）。

        工作台的参考素材不在 brief 文件里——创建口不收 refs，台账才是唯一真源
        （D7 §4.1）。阶段 1 的分镜表 Subject 标签与资产图提示词命名都要按台账语义名
        对齐，所以每个「把 brief 交给管线或写词」的执行点都必须走这里；漏一处，
        真实会话就会在出词那一步对不上号。

        路线判据是 ``route_uses_asset_ledger``：``ref`` 与导演台 ``r2v`` 都有台账；
        后者没有开场首帧待办，开场图同样只能来自台账（#268）。t2v/i2v/fl2v 与 base
        原样返回。

        只收 Picture：``RefItem.picture`` 是 ``<Picture N>`` 槽位，视频/音频没有这个
        编号（它们的段内子集是 #258 的事）。``path`` 留空——本地路径既不进 DTO 也不进
        管线输入。用 ``replace`` 而不是就地改：``_finish_stage2`` 会把 brief 写回会话
        文件，就地加就是给台账造一份会变旧的副本。
        """
        if not route_uses_asset_ledger(session.brief):
            return session.brief
        pictures = sorted(
            (entry for entry in session.asset_ledger
             if str(entry.get("kind")) == KIND_PICTURE),
            key=lambda entry: int(entry["number"]))
        from ..brief_parser import RefItem

        return replace(session.brief, refs=[
            RefItem(picture=int(entry["number"]), name=str(entry["name"]),
                    description="", path="")
            for entry in pictures])

    def _execute_stage1(self, session_id: str, job_id: str,
                        cancel_event: threading.Event) -> dict[str, Any]:
        """线程里跑的阶段 1；节点边界同时是取消检查点与增量落盘点。"""
        if self.fake_pipeline:
            return self._execute_stage1_fake(session_id, job_id, cancel_event)
        from ..graph.pipeline import _STAGE1_CHAIN, run_stage1

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        state = dict(session.stage_state)
        last_completed = str((state.get("_progress") or {}).get("last_completed_node", ""))
        planned = step_machine.stage1_resume(tuple(_STAGE1_CHAIN), last_completed)
        resume_from = (
            planned.node if planned.reason == step_machine.RESUME_FROM_NODE else None)
        # 链上已跑完且没有常识质检要补：没有剩下的事可做，别再整条重跑（D2-AC9）。
        skip = (planned.reason == step_machine.RESUME_EXHAUSTED
                and not getattr(self.config, "common_sense_qa", False))

        # 本次真正会跑的节点：续跑时从断点之后开始，起点与 ``run_stage1`` 的算法同源。
        chain = list(_STAGE1_CHAIN)
        if resume_from is not None:
            chain = chain[chain.index(resume_from):]
        # 进度事件的节拍器：节点开始时发 started、完成时发 done。起点取自链而不是
        # 「上一个回调的节点名」：基础路线的 ``parallel_image_prompts`` 不产出资产图
        # 提示词，``graph.stream`` 里那一格是空的、回调根本不会来——只看回调会让
        # started/done 错位，所以「图前进」本身也算上一个节点跑完的证据。
        cursor = {"next": 0}
        open_node: dict[str, Any] = {"index": None, "started_at": 0.0}

        def _close_open_node() -> None:
            index = open_node["index"]
            if index is None:
                return
            open_node["index"] = None
            self._emit_progress(session_id, job_id, chain[index], "done")

        def _emit_node_started(index: int) -> None:
            if index >= len(chain):
                return
            _close_open_node()
            open_node["index"] = index
            open_node["started_at"] = time.monotonic()
            cursor["next"] = index + 1
            self._emit_progress(session_id, job_id, chain[index], "started")

        def on_step_done(node_name: str, current_state: dict) -> None:
            node_index = chain.index(node_name) if node_name in chain else None
            if node_index is not None and open_node["index"] != node_index:
                # 中间那些节点没有产出、因而没有回调；图只会向前走，所以它们确实跑完了。
                # 先收掉它们，再补报当前节点的开始，事件流里的 started/done 才成对。
                _close_open_node()
                _emit_node_started(node_index)
            elapsed_ms = (
                int((time.monotonic() - open_node["started_at"]) * 1000)
                if open_node["index"] is not None else None)
            open_node["index"] = None
            progress_state = dict(current_state)
            progress_state["_progress"] = {"last_completed_node": node_name}
            self._save_progress(session_id, job_id, progress_state, node_name,
                                elapsed_ms=elapsed_ms)
            # 先看取消再报下一节点开始：取消后那个节点根本不会跑，先发 started
            # 会让界面显示一个从未执行过的节点（文案必须诚实）。
            if cancel_event.is_set():
                raise JobCancelled(node_name)
            _emit_node_started(
                node_index + 1 if node_index is not None else cursor["next"])

        if skip:
            final_state = state
        else:
            _emit_node_started(0)
            final_state = dict(run_stage1(
                self._ref_brief(session), self.config, initial_state=state,
                resume_from_node=resume_from, on_step_done=on_step_done)[0])
            _close_open_node()

        # 最后一个节点跑完到收尾之间到达的取消请求也在这里生效。
        if cancel_event.is_set():
            raise JobCancelled(str(
                (final_state.get("_progress") or {}).get("last_completed_node", "")))
        self._write_stage1_artifacts(session_id, job_id, final_state)
        return final_state

    def _execute_stage1_fake(self, session_id: str, job_id: str,
                             cancel_event: threading.Event) -> dict[str, Any]:
        """桩阶段 1：按真实链的节点名发进度事件，状态由 ``fake_pipeline`` 固定给出。

        进度事件照真实路径成对发（started/done）——浏览器 E2E 要验证「当前节点」
        真的在动；节点名取自 ``_STAGE1_CHAIN``，不另写一份名字清单。
        """
        from ..graph.pipeline import _STAGE1_CHAIN
        from .fake_pipeline import stage1_state

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        for name in _STAGE1_CHAIN:
            self._emit_progress(session_id, job_id, name, "started")
            if cancel_event.is_set():
                raise JobCancelled(name)
            self._emit_progress(session_id, job_id, name, "done")
        final_state = stage1_state(
            # 桩与真实两路只在「怎么跑」上不同，「交给管线的 brief 从哪来」必须同源；
            # #259 扩桩支持 ref 素材循环时从这里接（桩 state 目前不产资产图提示词）。
            self._ref_brief(session), dict(session.stage_state))
        self._write_stage1_artifacts(session_id, job_id, final_state)
        return final_state

    def _write_stage1_artifacts(self, session_id: str, job_id: str,
                                state: dict[str, Any]) -> None:
        """把阶段 1 的中间产物落成可看可复制的产物（D4 spec §7）。

        只写非空正文：空文件既不是「能审的东西」，还会让清单里多一条点开是空白的
        产物。帧词按 bundle 里**真有的行**写（渲染器对空帧返回空串），所以 T2VA 是
        两件、帧链会话才有首/尾帧词；不做 variant 白名单的第二次判定——bundle 自己
        就是那份事实。

        写盘与登记统一走 ``store.write_text_artifact``：先落盘再发 ``artifact.created``，
        订阅者拿到标识时文件已经在。
        """
        for name, key in ((ARTIFACT_SCRIPT, "script"),
                          (ARTIFACT_SHOT_TABLE, "shot_table")):
            text = str(state.get(key) or "")
            if text.strip():
                self.store.write_text_artifact(session_id, name, text, job_id=job_id)
        bundle = state.get("fl2va_prompt_bundle")
        if not isinstance(bundle, dict):
            return
        from ..generation import FL2VAPromptBundle
        from ..ui.wizard import render_fl2va_frame_markdown_from_bundle

        parsed = FL2VAPromptBundle.from_dict(bundle)
        for frame, name in (("first", ARTIFACT_FIRST_FRAME_PROMPT),
                            ("last", ARTIFACT_LAST_FRAME_PROMPT)):
            rendered = render_fl2va_frame_markdown_from_bundle(parsed, frame)
            if rendered.strip():
                self.store.write_text_artifact(
                    session_id, name, rendered, job_id=job_id)

    def _stage2_frame_paths(self, state: dict[str, Any]) -> dict[str, str]:
        """阶段 2 要用哪两张帧图：先看本次提交的交接键，再看已入库的帧记录。

        两条来源读同一个形状（``role → 文件路径``）：交接键是「用户刚交的」，
        ``frame_images`` 是「已经落盘、续跑时沿用的」。文件不在了就当作没交——
        读图会显式报错的地方已经由审计自己处理，这里只负责给路径。
        """
        pending = state.get(FRAME_SUBMISSION_KEY)
        if isinstance(pending, dict):
            return {str(role): str(path) for role, path in pending.items()
                    if str(path or "").strip()}
        paths: dict[str, str] = {}
        for row in state.get("frame_images") or []:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "")
            if role not in ("first", "last"):
                continue
            for key in ("target", "source"):
                raw = str(row.get(key) or "")
                if raw and Path(raw).is_file():
                    paths[role] = raw
                    break
        return paths

    @staticmethod
    def _frame_records(paths: dict[str, str]) -> list[dict[str, Any]]:
        """与向导 ``_store_frame`` 同形的帧记录——只是 ``target`` 就是素材正本。

        D4 §9 明确「帧直接引用素材文件、不复制第二份」，所以 source/target 指向同
        一个 ``assets/`` 下的文件；sha256 仍现算，续跑判「同一张图」时口径不变。
        """
        records: list[dict[str, Any]] = []
        for role, raw in paths.items():
            path = Path(raw)
            records.append({
                "role": role,
                "source": str(path),
                "target": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        return records

    def _save_stage2_state(self, session_id: str, state: dict[str, Any]) -> None:
        """任务进行中的 state 落盘（会话仍在 running）：帧记录先于慢速读图写下去。"""
        self.store.mutate(session_id, lambda _detail: {
            "status": step_machine.STATUS_RUNNING, "stage_state": state})

    def _write_segment_injection_card(self, session_id: str, position: int,
                                      ref_job: "RefSegmentJob", *,
                                      job_id: str | None = None) -> Any:
        """段级注入卡的**唯一写入口**（D7 §9 / #257）：落盘并登记产物。

        文本取自 ``RefSegmentJob.segment_asset_card``——与终端打印的是同一份，不
        另渲染一遍（两处渲染就是「卡上写 A、屏上打 B」）。**接线在 #258**：写段词
        成功时由段循环调用它，本票只把入口做成可调用、有单测的形状。
        段循环还没跑到该段时这个方法根本不会被调用——页面按「产物在不在」显示，
        不做空卡。旧 plan（无段内子集映射）返回空串，那一路接线看整片卡。
        """
        from ..workbench_store import segment_injection_card_name

        text = ref_job.segment_asset_card(position)
        if not text:
            return None
        return self.store.write_text_artifact(
            session_id, segment_injection_card_name(position), text, job_id=job_id)

    def _write_film_injection_card(self, session_id: str, job_id: str) -> None:
        """素材循环全部定稿之后、阶段 2 开始之前写整片注入卡（D7 §9 / #257）。

        只在「ref 会话 + 台账全部定稿」时产出：阶段 1 通过也会走到阶段 2 的入口，
        那时素材循环还没跑，卡上的台账会是空的。写盘很快，同步做即可——它是进
        阶段 2 前的最后一件产物，但**不单独占一个 step**（spec §5.2）。写失败就让
        这个 job 失败并点名，绝不静默漏卡。

        卡面**只读**：工作台没有手改入口，所以重跑直接覆盖；CLI 的
        ``write_injection_card`` 相反（已存在不覆盖），那边的手改才是出口。
        卡的内容绝不进 state / brief / 提示词——它是给人照抄的，不是可执行载荷。
        """
        from ..asset_ledger import AssetLedger
        from ..injection_card import render_injection_card, resolve_video_output
        from ..workbench_store import ARTIFACT_INJECTION_CARD

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        if not session.brief.is_ref:
            return
        if _asset_step_of(session.asset_ledger)[0] != ASSET_STEP_DONE:
            return
        derivation = resolve_video_output(
            session.brief.video_output, session.brief.canvases)
        ledger = AssetLedger(entries=tuple(
            self._card_record(entry, directory) for entry in session.asset_ledger))
        text = render_injection_card(
            ledger, derivation.params, notes=derivation.notes,
            session_dir=directory)
        self.store.write_text_artifact(
            session_id, ARTIFACT_INJECTION_CARD, text, job_id=job_id)

    def _card_record(self, entry: Mapping[str, Any], directory: Path) -> Any:
        """会话台账条目 → 注入卡的 ``AssetRecord``：``path`` 取盘上正本的相对路径。

        会话台账只记 ``asset_id``（``<会话标识>/<16 位哈希>``、无扩展名），卡上却要
        写「照抄就能找到的文件」。扩展名从盘上的实际文件解析（走素材读取的同一个
        解析口），不按嗅探规则另编一个——文件名就是内容哈希，编错就指向不存在的文件。
        """
        from ..asset_ledger import AssetRecord

        asset_id = str(entry.get("asset_id") or "")
        number = int(entry["number"])
        name = str(entry.get("name") or "")
        try:
            resource = self.store.resolve_asset_media(asset_id)
            relative = resource.path.resolve().relative_to(
                directory.resolve()).as_posix()
        except Exception as exc:  # noqa: BLE001 - 台账与盘上事实对不上，点名这一件
            raise RuntimeError(
                f"注入卡找不到第 {number} 件素材（{name}）的正本：{asset_id}") from exc
        return AssetRecord(
            name=name, kind=str(entry["kind"]), number=number,
            path=relative, sha256=str(entry["sha256"]))

    def _materialize_director_asset_ledger(
        self, session: Any, directory: Path,
    ) -> None:
        """r2v 打包前把会话台账落成 ``asset-ledger.json``（与 CLI 同形的产物）。

        ``director/pack.py`` 的 ``_picture_sources`` 只认这份文件：它按编号与语义名
        找每张参考图的正本。工作台的登记住在会话状态里（``session.asset_ledger``），
        不落这一份，r2v 打包会当场报「台账是空的」——而且只在真正打包时才炸。

        条目形状走 ``_card_record``：注入卡与打包共用同一份「台账条目 → 盘上正本」
        解析口，两处各拼一次路径就是「换布局时必有一处漏改」的老形态。
        """
        from ..asset_ledger import AssetLedger, save_asset_ledger

        entries = tuple(
            self._card_record(entry, directory) for entry in session.asset_ledger)
        if not entries:
            return
        save_asset_ledger(directory, AssetLedger(entries=entries))

    def _execute_pack(self, session_id: str, job_id: str,
                      cancel_event: threading.Event) -> dict[str, Any]:
        """导演台打包 job（D8 §9.1 / #263）：段表 + 段词 → ``director/pack/`` + zip。

        **顺序是契约**：``prepare_director_pack`` 自己先读齐输入、内存装配、跑完八条
        约束，最后才落盘。这里不先建目录再校验——半份包会被用户直接拿去导入，插件
        不会替我们拒绝它（它只会「成功了但少几张图」）。

        zip 已在时**不重建**（spec §9.4：初始包是计划，改词不重打包），但仍要补缺失
        的简报：续跑只补缺，不把用户已经看过的清单换一版。
        """
        from ..director.film_canvas import film_settings, node_setup_lines
        from ..director.pack import (
            CONTINUITY_DEFAULT,
            prepare_director_pack,
            validate_continuity_mode,
            zip_path_for,
        )
        from ..director.planning import plan_path, read_plan_json

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        if cancel_event.is_set():
            raise JobCancelled(DIRECTOR_PACK_NODE)
        self._emit_progress(session_id, job_id, DIRECTOR_PACK_NODE, "started",
                            stage=step_machine.STEP_PACKING)
        self._write_active_job(session_id, job_id, DIRECTOR_PACK_NODE)
        zip_file = zip_path_for(directory)
        try:
            plan = read_plan_json(plan_path(directory))
            settings = film_settings(self.config.director_settings(), session.brief)
            continuity_mode = validate_continuity_mode(
                str(session.brief.director_continuity_mode or CONTINUITY_DEFAULT))
            if zip_file.is_file():
                report = "\n".join([
                    f"导演包已存在，跳过重建：{zip_file}",
                    "初始包是计划、改词不重打包（spec §9.4）；本次只补缺失的简报。",
                ])
                skipped = True
            else:
                if plan.mode == "r2v":
                    # 打包器只认 ``asset-ledger.json``（CLI 的素材库台账形态）；
                    # 工作台的登记住在会话状态里，不落这一份就「没有参考图可挂」。
                    self._materialize_director_asset_ledger(session, directory)
                _job, report = prepare_director_pack(
                    directory, plan, settings=settings,
                    continuity_mode=continuity_mode)
                skipped = False
        except ValueError as exc:
            # 八条装配约束与坏段表都是 ValueError；打包器已点名是哪一条、哪一段，
            # 这里只翻成稳定错误码，不改写那句话（用户要照着它去改）。
            raise DirectorPackFailed(str(exc)) from exc
        if not skipped:
            self.store.register_artifact(
                session_id, ARTIFACT_DIRECTOR_PACK, job_id=job_id)
        briefings = self._write_director_briefings(
            session_id, job_id, plan, session, directory)
        self._emit_progress(session_id, job_id, DIRECTOR_PACK_NODE, "done",
                            stage=step_machine.STEP_PACKING)
        return {
            "report": report,
            "node_lines": node_setup_lines(settings),
            "skipped": skipped,
            "briefings": briefings,
        }

    def _write_director_briefings(
        self, session_id: str, job_id: str, plan: Any, session: Any,
        directory: Path,
    ) -> list[str]:
        """补写缺失的逐段简报（D8 §8.5 / §4.6）：已存在的不重写。

        简报正文只走 ``wizard_display.render_director_segment_briefing`` 这一份渲染口
        （CLI 与工作台同源）。缓存目录**只用于定位**：配置缺失或路径非法就算了，
        不让一个给人看的清单因为定位信息不可得而整份失败。
        """
        from ..director.cache import cache_dir
        from ..ui.wizard_display import render_director_segment_briefing

        cache_name = str(session.brief.director_cache_name or "")
        cache_root = getattr(self.config, "director_comfy_output_root", None)
        cache_directory: Path | None = None
        if cache_root is not None:
            try:
                cache_directory = cache_dir(
                    cache_root(), cache_name,
                    getattr(session.brief, "director_node_id", ""))
            except (OSError, ValueError):
                cache_directory = None
        written: list[str] = []
        for segment in plan.segments:
            name = director_briefing_name(segment.index)
            if (directory / name).is_file():
                continue
            text = render_director_segment_briefing(
                plan, segment, directory,
                cache_directory=cache_directory, cache_name=cache_name)
            self.store.write_text_artifact(session_id, name, text, job_id=job_id)
            written.append(name)
        return written

    def _execute_stage2(self, session_id: str, job_id: str,
                        cancel_event: threading.Event) -> dict[str, Any]:
        """线程里跑的阶段 2（approve / 交帧之后）。

        有帧则先读图再组装（D4 §9）：读图是慢速视觉调用，放在任务里，请求在 202
        就返回了。没有帧时的**自动降级**与向导同源（``resolve_effective_variant``），
        并把降级事实记进会话（``variant_downgraded``）——降级不静默。
        ``video-prompt.md`` 与向导同目录同名，写完才算成功。
        """
        # 导演台阶段 2 不是 base/ref 的阶段 2（spec #253 §5.2 / §8）：它只做「按帧
        # 规划 + 逐段写词」，不跑 ``run_stage2``、不产 ``video-prompt.md``。路线在这里
        # 分叉，base/ref 的既有分支一字不动。
        director_session = load_session(self.store.session_directory(session_id))
        if director_session is not None and director_session.brief.is_director:
            return self._execute_director_stage2(
                session_id, job_id, cancel_event, director_session)
        # 整片注入卡是进阶段 2 前的最后一件产物（D7 §9 / #257）：入口只此一处，
        # 写失败即本 job 失败。它不进下面的 state / brief，模型看不到它。
        self._write_film_injection_card(session_id, job_id)
        if self.fake_pipeline:
            return self._execute_stage2_fake(session_id, job_id, cancel_event)
        from ..brief_parser import RefItem
        from ..graph.pipeline import _STAGE2_CHAIN, run_stage2
        from ..stage2_checkpoint import load_checkpoint
        from ..tools.frame_auditor import audit_frame_images, required_frames
        from ..ui.wizard import resolve_effective_variant

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        state = dict(session.stage_state)
        brief = self._ref_brief(session)
        original = brief.variant
        paths = self._stage2_frame_paths(state)
        have_first, have_last = "first" in paths, "last" in paths
        effective = resolve_effective_variant(original, have_first, have_last)
        downgraded = ""
        if effective != original:
            brief.variant = effective
            downgraded = f"{original}→{effective}"
            # 降级照向导记录、不静默：会话字段写回之外，日志也要能看出这件事发生过。
            logger.info("阶段 2 按提交的帧位降级（%s）：%s / %s",
                        downgraded, session_id, job_id)

        descriptions: list[dict[str, Any]] = []
        if have_first or have_last:
            records = self._frame_records(paths)
            # 记录先落盘、读图在后（与向导同一条理由）：慢速调用中断时帧路径已经
            # 在盘上，续跑不用重新交；交接键在同一把锁里收掉，不留两份事实。
            state["frame_images"] = records
            state.pop(FRAME_SUBMISSION_KEY, None)
            self._save_stage2_state(session_id, state)
            slots = {role: picture for picture, role in required_frames(effective)}
            # 有效变体不要的帧（例如请求 I2VA 却多交了尾帧）不送读图：与向导
            # 「按 required_frames(effective) 取」一致。
            refs = [
                RefItem(picture=slots[role],
                        name="首帧" if role == "first" else "尾帧",
                        description="", path=paths[role])
                for role in ("first", "last")
                if role in slots and role in paths
            ]
            roles: list[str] = [role for role in ("first", "last")
                                if role in slots and role in paths]
            if refs:
                # 续跑时上一轮的读图结果仍然成立（帧图提交后不再更换）：不重付一次
                # 视觉调用。新一轮提交则一定重读。
                reused = self._reusable_descriptions(state, roles, paths)
                if reused is None:
                    audits = audit_frame_images(refs, effective)
                    descriptions = [audit.to_dict() for audit in audits]
                    if cancel_event.is_set():
                        raise JobCancelled("frame_audit")
                else:
                    descriptions = reused
        else:
            state.pop(FRAME_SUBMISSION_KEY, None)
        state["fl2va_frame_descriptions"] = descriptions
        # 有阶段 2 断点时组装已被跳过，那时不会有节点回调——先报 started 会留下
        # 一条「开始了却永远没结束」的假事件。
        if load_checkpoint(directory) is None:
            self._emit_progress(session_id, job_id, _STAGE2_CHAIN[0], "started",
                                stage=step_machine.STEP_STAGE2)

        def on_node(node_name: str) -> None:
            self._emit_progress(session_id, job_id, node_name, "done",
                                stage=step_machine.STEP_STAGE2)
            if cancel_event.is_set():
                raise JobCancelled(node_name)

        final_state, prompt = run_stage2(
            state, brief, self.config, on_node=on_node,
            checkpoint_dir=directory)
        # 写盘与登记走同一个入口：清单一处口径（目录现扫）、事件由登记发出（#220）。
        self.store.write_text_artifact(
            session_id, ARTIFACT_VIDEO_PROMPT, prompt, job_id=job_id)
        self._summarize_stage2_prompt(session_id, job_id, prompt, cancel_event)
        # 派生出来的 refs 不回写会话文件：``_finish_stage2`` 会把这份 brief 落盘，
        # 带上副本就等于给台账造一份会变旧的拷贝。交回的是一份新副本，不动刚交给
        # 管线的那一份（降级后的 variant 仍照旧写回）。
        persisted_brief = replace(brief, refs=list(session.brief.refs))
        return {"state": dict(final_state), "brief": persisted_brief,
                "variant_downgraded": downgraded}

    # ------------------------------------------------------------------
    # 导演台阶段 2：规划 + 写段（D8 §8.1 / §8.2）
    # ------------------------------------------------------------------

    def _execute_director_stage2(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event, session: Any,
    ) -> dict[str, Any]:
        """导演台阶段 2 的线程体：按帧落位 → ``director/plan.json`` → 逐段写词。

        三件事的**判据全在磁盘上**，不在 job 记录里（spec #253 §16）：``plan.json``
        已在就不重规划；某一段的 ``segment-NN.md`` 已在就不重写。于是中断/重启后的
        ``advance`` 是幂等的——它只会补缺的那几段，不会把用户已经看过的段词换一版。

        规划失败即 job 失败（没有机械回退，ADR 0026）：调用病与回复不合格都由
        ``plan_director_segments`` 抛出，这里不降级成别的分段口径。
        """
        from ..director.planning import (
            plan_path, read_plan_json, write_asset_budget, write_plan_json)

        directory = self.store.session_directory(session_id)
        mode = str(session.brief.director_mode)
        state = dict(session.stage_state)
        shot_table = str(state.get("shot_table") or "")
        if not shot_table.strip():
            # 没有分镜表就没有可规划的叙事单元（与向导同一道前置闸）：不拿空表让
            # 模型自由发挥，也不假装成功。
            raise RuntimeError(
                "导演台路线：会话里没有分镜表（阶段 1 产物缺失），无法做段规划")

        plan_file = plan_path(directory)
        context: _DirectorWriteContext | None = None
        if plan_file.is_file():
            # 续跑只认**当初定案的那张表**：未知段类型 / 非法帧数 / Σ 导出 ≠ 总帧
            # 都在 ``read_plan_json`` 里抛，绝不拿错表往下写段。写段入参（定义块/
            # 尾帧描述）等真要写缺段时按**本会话**重建（见 ``_director_resume_context``）。
            plan = read_plan_json(plan_file)
            if plan.mode != mode:
                raise ValueError(
                    f"director/plan.json 的模式是 {plan.mode}，与本次会话的 {mode} "
                    "不一致：不能拿错表续跑")
        else:
            plan, context = self._plan_director_project(
                session_id, job_id, cancel_event, session, state, shot_table, mode,
                directory)
            write_plan_json(plan_file, plan)
            if context.budget is not None:
                # 预算紧随段表落盘（写段只能在本段预算里选图，#188）；非 r2v 的
                # ``write_asset_budget`` 本身不写文件。续跑不重写预算——那是规划期
                # 的产物，重写会用「现在的台账」覆盖「当初定案的那份」。
                write_asset_budget(directory, context.budget)
        written = self._write_director_project(
            session_id, job_id, cancel_event, session, state, mode,
            directory, plan, context)
        for item in written:
            self.store.register_artifact(
                session_id, director_segment_prompt_name(item.index), job_id=job_id)
        rows = _director_segment_rows(
            session_id, plan, directory, list(session.segments))
        return {
            "state": state,
            "brief": session.brief,
            "variant_downgraded": "",
            "director": True,
            "segments": rows,
        }

    def _director_registry(self, session: Any) -> tuple[tuple[int, str], ...]:
        """r2v 的素材登记表：``(全局编号, 语义名)``，编号与台账一致。

        走 ``ref_asset_registry``（与 CLI/多参同一条取数口）并把 ``_ref_brief`` 交进去：
        工作台会话的 brief 本身不带 ``refs``，台账才是唯一真源——不经过 ``_ref_brief``
        这里会拿到空登记，规划师就看不清可挑的图。
        """
        from ..generation import ref_asset_registry

        return tuple(
            (item.picture, item.name)
            for item in ref_asset_registry(
                session.stage_state, self._ref_brief(session)))

    def _director_ref_context(
        self, session: Any, state: dict[str, Any], directory: Path, mode: str,
    ) -> tuple[tuple[tuple[int, str], ...], Any]:
        """r2v 的登记表与全片英文定义块：**只生成一次**，盘上那份登记一致就复用。

        定义块是六节外壳的第 1、3 节，逐段重复；它在规划之前就位（规划吃它的闭包算
        预算），改写素材是阶段 1 的中文 ``subject_defs``——直接复制进段就是半份空定义
        （``refdefs`` 模块头记着 2026-11-01 的现场）。缓存判据包含登记表：台账变了，
        旧定义里的编号/语义名就对不上，必须重写。

        非 r2v 没有参考素材，返回空表与 ``None``（写段也不会用到）。
        """
        if mode != "r2v":
            return (), None
        from ..director import refdefs

        registry = self._director_registry(session)
        cached = refdefs.load_ref_sections(directory)
        if (cached is not None and refdefs.has_sections(cached)
                and cached.registry == registry):
            return registry, cached
        if self.fake_pipeline:
            from .fake_pipeline import director_ref_sections

            sections = director_ref_sections(registry)
        else:
            sections = refdefs.build_ref_sections(
                material=str(state.get("subject_defs", "")),
                registry=registry, llm=build_chat_model())
        refdefs.save_ref_sections(directory, sections)
        return registry, sections

    def _plan_director_project(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event, session: Any, state: dict[str, Any],
        shot_table: str, mode: str, directory: Path,
    ) -> tuple[Any, _DirectorWriteContext]:
        """跑一次导演台规划，交回段表与**本次执行**的写段上下文（含预算）。"""
        from ..director.planning import plan_director_segments
        from ..segment_prompts import frame_anchor_context, render_revision_context

        self._emit_progress(
            session_id, job_id, DIRECTOR_PLAN_NODE, "started",
            stage=step_machine.STEP_STAGE2)
        self._write_active_job(session_id, job_id, DIRECTOR_PLAN_NODE)
        # r2v 的登记表与英文定义块在**规划之前**就位（#268）：规划师按登记编号挑图，
        # 预算闭包读的也是这份定义块。桩模式同样生一份（确定性的英文两节）。
        registry, sections = self._director_ref_context(
            session, state, directory, mode)
        subject_definitions = (sections.subject_definitions
                               if sections is not None else "")
        if self.fake_pipeline:
            from .fake_pipeline import director_plan

            plan = director_plan(mode, session.brief.duration)
            budget = None
            if mode == "r2v":
                # 预算仍由**真实**的闭包/锚降级算出（桩只替规划师挑了哪几张）。
                from ..director.planning import build_asset_budget

                numbers = tuple(number for number, _name in registry)
                budget = build_asset_budget(
                    plan,
                    {segment.index: numbers for segment in plan.segments},
                    subject_definitions=subject_definitions, registry=registry)
            context = _DirectorWriteContext(
                shot_table=shot_table, sections=sections, registry=registry,
                budget=budget)
        else:
            from ..director.pack import film_end_still
            from ..ui.wizard_director import prepare_film_end_description

            film_end = (mode == "fl2v"
                        and film_end_still(directory) is not None)
            ok, description = prepare_film_end_description(directory)
            if not ok:
                raise RuntimeError(
                    "全片尾帧读图失败：没有做段规划、没有写段")
            result = plan_director_segments(
                shot_table, session.brief.duration, build_chat_model(),
                mode=mode,
                # 与 base 的规划调用同源：真实关键帧画面压过分镜表、修订按优先序注入。
                frame_context=frame_anchor_context(state),
                revision_context=render_revision_context(state),
                film_end_description=description,
                registry=registry,
                subject_definitions=subject_definitions,
            )
            plan = result.plan
            context = _DirectorWriteContext(
                shot_table=shot_table,
                sections=sections,
                registry=registry,
                film_end=film_end,
                film_end_description=description,
                budget=result.budget,
            )
        if cancel_event.is_set():
            raise JobCancelled(DIRECTOR_PLAN_NODE)
        self._emit_progress(
            session_id, job_id, DIRECTOR_PLAN_NODE, "done",
            stage=step_machine.STEP_STAGE2)
        return plan, context

    def _director_resume_context(
        self, directory: Path, session: Any, mode: str, shot_table: str,
    ) -> _DirectorWriteContext:
        """续写缺段时的上下文：**只从本会话的盘上事实**重建，不继承任何别的执行。

        定义块与全片尾帧描述都可能已经在盘上（上一轮或 CLI 产出）：写段请求要与
        当初规划时看到的一致，所以按本会话目录重新取一份。预算**不取**——续跑没有
        重新规划，预算文件是规划期的产物，不该被覆盖。
        """
        from ..director.pack import film_end_still
        from ..ui.wizard_director import prepare_film_end_description

        # 定义块与登记表只从本会话的盘上事实取：缓存命中就复用；盘上没有（例如
        # 段表已落盘、定义块被删）就重生成一次——续写段词没有定义块就是半份空定义。
        registry, sections = self._director_ref_context(
            session, session.stage_state, directory, mode)
        film_end = mode == "fl2v" and film_end_still(directory) is not None
        ok, description = prepare_film_end_description(directory)
        if not ok:
            raise RuntimeError("全片尾帧读图失败：不能续写段词")
        return _DirectorWriteContext(
            shot_table=shot_table,
            sections=sections,
            registry=registry,
            film_end=film_end,
            film_end_description=description,
        )

    def _write_director_project(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event, session: Any, state: dict[str, Any],
        mode: str, directory: Path, plan: Any,
        context: _DirectorWriteContext | None,
    ) -> list[Any]:
        """写缺的段词（已在盘上的一段都不碰）并交回本轮写出的清单。

        ``context`` 为 ``None`` ＝ 本轮是续跑（没有重新规划）：此时按**本会话**的
        盘上事实现建上下文，绝不读任何实例级残留。
        """
        from ..director.prompts import segment_path, write_director_segments

        missing = [
            segment for segment in plan.segments
            if not segment_path(directory, segment.index).is_file()
        ]
        if not missing:
            return []
        self._emit_progress(
            session_id, job_id, DIRECTOR_WRITE_NODE, "started",
            stage=step_machine.STEP_STAGE2)
        self._write_active_job(session_id, job_id, DIRECTOR_WRITE_NODE)
        if self.fake_pipeline and plan.mode != "r2v":
            from types import SimpleNamespace

            from .fake_pipeline import director_segment_prompt

            written = []
            for segment in missing:
                path = segment_path(directory, segment.index)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    director_segment_prompt(plan, segment) + "\n", encoding="utf-8")
                written.append(SimpleNamespace(index=segment.index, path=path))
        else:
            if context is None:
                context = self._director_resume_context(
                    directory, session, mode, str(state.get("shot_table") or ""))
            sections = context.sections
            if self.fake_pipeline:
                # r2v 的桩只替「谁写这段正文」：归一、六节校验、段内素材子集与
                # ``assets-in-segment.json`` 落盘全部走真实写段路径。桩另写一份
                # 子集就是「产物形状随开关变」，CI 也证明不了 #268 的接线。
                from .fake_pipeline import StubChatModel, director_segment_reply

                llm = StubChatModel(director_segment_reply())
            else:
                llm = build_chat_model()
            written = write_director_segments(
                plan, llm=llm, session_dir=directory,
                shot_table=context.shot_table,
                sections=(sections.as_mapping() if sections is not None else None),
                registry=context.registry,
                film_end=context.film_end,
                film_end_description=context.film_end_description,
                # 续跑只补缺的段：已在盘上的段词是用户已经看过的产物，不重写、不重付。
                only_missing=True,
            )
        if cancel_event.is_set():
            raise JobCancelled(DIRECTOR_WRITE_NODE)
        self._emit_progress(
            session_id, job_id, DIRECTOR_WRITE_NODE, "done",
            stage=step_machine.STEP_STAGE2)
        return written

    def _summarize_stage2_prompt(self, session_id: str, job_id: str, prompt: str,
                                 cancel_event: threading.Event) -> None:
        """阶段 2 的中文摘要两件（D4 spec §10 / AC14）。

        与向导同一条路、同一份格式（``summary.summarize_prompt_zh`` → JSON + 渲染）；
        ``video-prompt.md`` 已经写完，摘要只是展示层的增强：模型/解析失败一律只记
        日志并返回，不让一次已经成功的阶段 2 变成失败（向导同款降级）。
        """
        from ..model_factory import build_chat_model
        from ..summary import summarize_prompt_zh

        try:
            summary = summarize_prompt_zh(prompt, build_chat_model())
        except Exception as exc:  # noqa: BLE001 - 摘要失败不阻塞交付
            logger.warning("阶段 2 中文摘要生成失败：%s / %s（%s: %s）",
                           session_id, job_id, type(exc).__name__, exc)
            return
        if summary is None:
            logger.warning("阶段 2 中文摘要不可用（模型未返回可用摘要）：%s / %s",
                           session_id, job_id)
            return
        # 摘要是一次慢速模型调用：调用后仍按「当前步骤结束才停」的取消语义收尾。
        if cancel_event.is_set():
            raise JobCancelled("summary")
        self._write_summary_artifacts(session_id, job_id, summary)

    def _write_summary_artifacts(self, session_id: str, job_id: str,
                                 summary: PromptSummary) -> None:
        """落盘摘要两件：JSON 与向导 ``_load_or_make_summary`` 同形，Markdown 同一渲染。"""
        payload = {"overall": summary.overall, "shots": summary.shots}
        self.store.write_text_artifact(
            session_id, ARTIFACT_SUMMARY_MARKDOWN,
            render_summary_zh(summary) + "\n", job_id=job_id)
        self.store.write_text_artifact(
            session_id, ARTIFACT_SUMMARY_JSON,
            json.dumps(payload, ensure_ascii=False, indent=2), job_id=job_id)

    @staticmethod
    def _reusable_descriptions(
        state: dict[str, Any], roles: list[str],
        paths: dict[str, str],
    ) -> list[dict[str, Any]] | None:
        """上一次读图结果还能不能用：同一批帧图（路径重合）且描述非空才复用。

        判据不重算 sha 再比对——阶段 2 的帧图在提交后没有更换路径，路径就是那份
        事实；描述为空（上次没读成功）则重读。
        """
        previous = {
            str(row.get("role")): row
            for row in state.get("fl2va_frame_descriptions") or []
            if isinstance(row, dict)
        }
        if not previous:
            return None
        out: list[dict[str, Any]] = []
        for role in roles:
            row = previous.get(role)
            if row is None or not str(row.get("description") or "").strip():
                return None
            if Path(str(row.get("path") or "")) != Path(paths.get(role, "")):
                return None
            out.append(dict(row))
        return out

    def _execute_stage2_fake(self, session_id: str, job_id: str,
                             cancel_event: threading.Event) -> dict[str, Any]:
        """桩阶段 2：假读用户提交的帧，仍走真实的落盘与降级判定。

        与真实路径共用 ``_stage2_frame_paths`` 与 ``resolve_effective_variant``：
        桩只把「读图」这一步换成固定文案，帧位来自用户提交的素材（双空＝不读）。
        """
        from ..graph.pipeline import _STAGE2_CHAIN
        from ..tools.frame_auditor import required_frames
        from ..ui.wizard import resolve_effective_variant
        from .fake_pipeline import frame_descriptions, stage2_state, stage2_summary

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        brief = self._ref_brief(session)
        state = dict(session.stage_state)
        paths = self._stage2_frame_paths(state)
        if paths:
            state["frame_images"] = self._frame_records(paths)
        state.pop(FRAME_SUBMISSION_KEY, None)
        original = brief.variant
        effective = resolve_effective_variant(
            original, "first" in paths, "last" in paths)
        downgraded = ""
        if effective != original:
            brief.variant = effective
            downgraded = f"{original}→{effective}"
            logger.info("阶段 2 按提交的帧位降级（%s）：%s / %s",
                        downgraded, session_id, job_id)
        # 只描述有效变体要的帧位：多交的槽不进读图结果（与真实路径的筛选同源）。
        needed = [role for _picture, role in required_frames(effective)]
        descriptions = frame_descriptions(
            brief, effective, tuple(role for role in needed if role in paths))
        for name in _STAGE2_CHAIN:
            self._emit_progress(session_id, job_id, name, "started",
                                stage=step_machine.STEP_STAGE2)
            if cancel_event.is_set():
                raise JobCancelled(name)
            self._emit_progress(session_id, job_id, name, "done",
                                stage=step_machine.STEP_STAGE2)
        updated, prompt = stage2_state(state, brief, descriptions)
        self.store.write_text_artifact(
            session_id, ARTIFACT_VIDEO_PROMPT, prompt, job_id=job_id)
        # 桩模式同样落三件：桩不建模型，摘要正文固定，渲染与落盘仍是真实路径那份。
        self._write_summary_artifacts(session_id, job_id, stage2_summary(brief))
        # 同真实路径：交回的 brief 不带派生的 refs，也不动刚交给管线的那一份。
        persisted_brief = replace(brief, refs=list(session.brief.refs))
        return {"state": updated, "brief": persisted_brief,
                "variant_downgraded": downgraded}

    # ------------------------------------------------------------------
    # 素材循环（D7 §5.3 / §6）
    # ------------------------------------------------------------------

    @staticmethod
    def _picture_entry(ledger: Any, number: int) -> dict[str, Any]:
        """台账里第 ``number`` 件 Picture；找不到是内部不一致（待办不该存在）。"""
        for entry in ledger:
            if (str(entry.get("kind")) == KIND_PICTURE
                    and int(entry.get("number", 0)) == int(number)):
                return dict(entry)
        raise RuntimeError(f"素材台账里没有第 {number} 件 Picture")

    def _asset_step(self, session_id: str) -> tuple[str, int]:
        """``_asset_step_of`` 的读取版：``_run`` 里按 job 分派用。"""
        session = load_session(self.store.session_directory(session_id))
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        return _asset_step_of(session.asset_ledger)

    def _execute_asset_write(self, session_id: str, job_id: str,
                             cancel_event: threading.Event,
                             number: int) -> dict[str, Any]:
        """写这一件的资产图词：落 ``assets/picture-NN.md``，job 结束。

        词来自**阶段 1 已经写好的那一份**（``generation.asset_prompt_for`` 按语义名取）：
        素材循环不是重写一遍词，而是把它逐件拿去过图核对（D7 §7 的理由：合并成一件会让
        「这件当时给的是哪一版词」脱钩）。桩模式只换「谁写了阶段 1 那份词」，取数与落盘
        仍是这条路——两处各写一份就成了「产物形状随开关变」。
        """
        from ..generation import asset_prompt_for

        session = load_session(self.store.session_directory(session_id))
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        entry = self._picture_entry(session.asset_ledger, number)
        name = str(entry["name"])
        artifact_name = ASSET_PROMPT_FILENAME.format(number=number)

        self._emit_progress(session_id, job_id, ASSET_WRITE_NODE, "started",
                            stage=step_machine.STEP_ASSET_LOOP)
        if cancel_event.is_set():
            raise JobCancelled(ASSET_WRITE_NODE)
        artifact_id = f"{session_id}/{artifact_name}"
        if int(entry.get("round", 0)) > 0:
            # 轮次 > 0 ＝这件被校对打回过，盘上那一版是**按差异改过的词**。重出会把它
            # 悄悄换回阶段 1 的初稿（崩溃恢复正是这条路），所以只重开卡片、不动正文。
            self._emit_progress(session_id, job_id, ASSET_WRITE_NODE, "done",
                                stage=step_machine.STEP_ASSET_LOOP)
            return {"step": ASSET_STEP_WRITE, "number": number, "name": name,
                    "artifact_id": artifact_id}
        variant = asset_prompt_for(session.brief, session.stage_state, name)
        info = self.store.write_text_artifact(
            session_id, artifact_name,
            _render_asset_prompt(number, name, variant.positive_prompt),
            job_id=job_id)
        self._emit_progress(session_id, job_id, ASSET_WRITE_NODE, "done",
                            stage=step_machine.STEP_ASSET_LOOP)
        return {"step": ASSET_STEP_WRITE, "number": number, "name": name,
                "artifact_id": info.artifact_id}

    def _execute_asset_proofread(self, session_id: str, job_id: str,
                                 cancel_event: threading.Event,
                                 number: int) -> dict[str, Any]:
        """读用户交的图并校对（D7 §6.2）：通过 → 定稿；不通过 → 按差异改词。

        读图走**唯一入口** ``describe_image``（不另建视觉客户端）。不通过时用
        ``rewrite_asset_prompt`` 把差异清单当 feedback 重写这一件的词、覆盖同一份
        产物——用户没有「就这样吧」可点，**改词是唯一的降级通道**（spec §6.3）。

        桩模式（``fake_pipeline``）只把这个 job 的「读图与改写」换成固定结论，台账
        转移、待办重开与产物覆盖仍由收尾的同一份代码走——CI 里两条分支都要真的跑到。
        """
        if self.fake_pipeline:
            return self._execute_asset_proofread_fake(
                session_id, job_id, cancel_event, number)
        from ..asset_loop import rewrite_asset_prompt
        from ..asset_proofread import (
            build_proofread_request,
            compare_descriptions,
            parse_reading,
            render_proofread_report,
        )
        from ..generation import asset_prompt_for
        from ..model_factory import build_chat_model
        from ..tools.reference_auditor import describe_image

        session = load_session(self.store.session_directory(session_id))
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        entry = self._picture_entry(session.asset_ledger, number)
        name = str(entry["name"])
        submitted = str(entry.get(ASSET_PROOFREAD_KEY) or "").strip()
        if not submitted:
            raise RuntimeError(f"第 {number} 件没有待校对的素材图标识")
        path = self.store.resolve_image_asset(session_id, submitted)
        artifact_name = ASSET_PROMPT_FILENAME.format(number=number)
        text = (self.store.session_directory(session_id)
                / artifact_name).read_text(encoding="utf-8")
        current = _parse_asset_prompt(text)
        # 锁死清单是判偏差的唯一判据（#53）：没声明就不收窄，与 CLI 同一口径。
        variant = asset_prompt_for(session.brief, session.stage_state, name)
        locked = tuple(variant.locked_features)

        self._emit_progress(session_id, job_id, ASSET_PROOFREAD_NODE, "started",
                            stage=step_machine.STEP_ASSET_LOOP)
        if cancel_event.is_set():
            raise JobCancelled(ASSET_PROOFREAD_NODE)
        raw = describe_image(path, build_proofread_request(current, locked))
        reading = parse_reading(raw)
        report = compare_descriptions(current, reading, locked)
        if cancel_event.is_set():
            raise JobCancelled(ASSET_PROOFREAD_NODE)
        if report.is_clean():
            self._emit_progress(session_id, job_id, ASSET_PROOFREAD_NODE, "done",
                                stage=step_machine.STEP_ASSET_LOOP)
            return {"step": ASSET_STEP_PROOFREAD, "number": number, "name": name,
                    "passed": True, "reason": ""}

        reason = render_proofread_report(report)
        rewritten = rewrite_asset_prompt(current, reason, build_chat_model())
        self.store.write_text_artifact(
            session_id, artifact_name,
            _render_asset_prompt(number, name, rewritten), job_id=job_id)
        self._emit_progress(session_id, job_id, ASSET_PROOFREAD_NODE, "done",
                            stage=step_machine.STEP_ASSET_LOOP)
        return {"step": ASSET_STEP_PROOFREAD, "number": number, "name": name,
                "passed": False, "reason": reason}

    def _execute_asset_proofread_fake(self, session_id: str, job_id: str,
                                      cancel_event: threading.Event,
                                      number: int) -> dict[str, Any]:
        """桩校对（#259）：伪造读图结论，**不调 ``describe_image``**、不建模型。

        结论必须确定性且两条路都覆盖：第 1 件第一轮不通过、其余通过（判据是台账
        轮次，不是调用次数——重跑/续跑不能换结论）。不通过时照真实路径覆盖同一份
        ``assets/picture-NN.md``，只是改写由桩完成；台账转移仍在 ``_finish_asset_proofread``
        那一份代码里，桩不另写一套收尾。
        """
        from .fake_pipeline import asset_proofread_verdict, rewrite_asset_prompt_text

        session = load_session(self.store.session_directory(session_id))
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        entry = self._picture_entry(session.asset_ledger, number)
        name = str(entry["name"])
        submitted = str(entry.get(ASSET_PROOFREAD_KEY) or "").strip()
        if not submitted:
            raise RuntimeError(f"第 {number} 件没有待校对的素材图标识")
        # 归属与类型在 resolve 已判过一次；这里再确认那张图仍在盘上（读不到＝技术故障，
        # 不是「用户的图不合格」）。桩不为它开后门，只是不读图内容。
        self.store.resolve_image_asset(session_id, submitted)
        first_number = min(
            int(item["number"]) for item in session.asset_ledger
            if str(item.get("kind")) == KIND_PICTURE)
        passed, reason = asset_proofread_verdict(
            number, first_number, int(entry.get("round", 0)))

        self._emit_progress(session_id, job_id, ASSET_PROOFREAD_NODE, "started",
                            stage=step_machine.STEP_ASSET_LOOP)
        if cancel_event.is_set():
            raise JobCancelled(ASSET_PROOFREAD_NODE)
        if not passed:
            artifact_name = ASSET_PROMPT_FILENAME.format(number=number)
            text = (self.store.session_directory(session_id)
                    / artifact_name).read_text(encoding="utf-8")
            current = _parse_asset_prompt(text)
            self.store.write_text_artifact(
                session_id, artifact_name,
                _render_asset_prompt(number, name, rewrite_asset_prompt_text(current)),
                job_id=job_id)
        self._emit_progress(session_id, job_id, ASSET_PROOFREAD_NODE, "done",
                            stage=step_machine.STEP_ASSET_LOOP)
        return {"step": ASSET_STEP_PROOFREAD, "number": number, "name": name,
                "passed": passed, "reason": reason}

    def _finish_asset_write(self, session_id: str, job_id: str,
                            result: dict[str, Any]) -> None:
        """出词收尾：挂出本件唯一的 ``asset_image`` 待办，会话等人工交图。"""
        number = int(result["number"])
        item = ActionItemOut(
            item_id=uuid.uuid4().hex, kind=step_machine.ACTION_ASSET_IMAGE,
            status="pending", created_at=_now(), asset_index=number)

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            # 每件同时最多一个未决项：同类旧项先收掉再挂新的（与阶段 1 / 段循环同一条
            # 纪律）。严格串行也由这里兜住——新卡片出现时上一张必然已决议。
            items = [
                existing.model_dump(mode="json") for existing in detail.action_items
                if not (existing.kind == step_machine.ACTION_ASSET_IMAGE
                        and existing.status == "pending")]
            items.append(item.model_dump(mode="json"))
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": jobs,
                "action_items": items,
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_AWAITING_HUMAN}, job_id=job_id),
            EventDraft("action_item.created",
                       {"item_id": item.item_id, "kind": item.kind,
                        "asset_index": number}, job_id=job_id),
        ])

    def _finish_asset_proofread(self, session_id: str, job_id: str,
                                result: dict[str, Any]) -> JobSummary | None:
        """校验收尾：通过则定稿并接下一件（或转阶段 2）；不通过则同件号重开待办。

        去向只看台账（``_asset_step_of``）：全部定稿时起一个普通 ``advance`` 任务，
        由 ``_resume_step`` 落到阶段 2——阶段 2 的入口仍只有那一处。
        """
        number = int(result["number"])
        passed = bool(result["passed"])
        reason = str(result["reason"])
        # 下一个任务的 kind 由「这一件定稿之后的台账」决定。这里按磁盘快照先算一次，
        # 只为选路（继续写词 / 转阶段 2）；``_apply`` 里会按 mutate 的快照**再算一次**
        # 作为真正写盘的台账——锁内那次才是权威值。两次都调 ``_settle_asset``，规则
        # 单源，不另写一份转移规则（同一会话同时只有一个 job，resolve 在 running 时被拒）。
        session = load_session(self.store.session_directory(session_id))
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        settled = _settle_asset(session.asset_ledger, number, passed)
        step, _ = _asset_step_of(settled)
        next_job: JobSummary | None = None
        item: ActionItemOut | None = None
        if passed:
            next_job = JobSummary(
                job_id=uuid.uuid4().hex,
                # 还有下一件 → 继续素材循环；全部定稿 → 普通推进任务（进阶段 2）。
                kind=(JOB_KIND_ASSET_LOOP if step != ASSET_STEP_DONE else "advance"),
                status="running", started_at=_now())
        else:
            item = ActionItemOut(
                item_id=uuid.uuid4().hex, kind=step_machine.ACTION_ASSET_IMAGE,
                status="pending", created_at=_now(), asset_index=number,
                reason=reason)

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            changes: dict[str, Any] = {
                "asset_ledger": _settle_asset(
                    detail.asset_ledger_snapshot(), number, passed)}
            if next_job is not None:
                changes["status"] = step_machine.STATUS_RUNNING
                changes["jobs"] = _upsert_job(jobs, _job_dict(next_job))
            else:
                # 同一件号重开：同类未决旧项先收掉再挂新的（收旧挂新）。
                # 未通过的这一支一定造了待办；闭包里 mypy 不追 ``if passed`` 的对应
                # 关系，显式取一次局部值把类型收窄（运行期不可能为空）。
                assert item is not None
                items = [
                    existing.model_dump(mode="json")
                    for existing in detail.action_items
                    if not (existing.kind == step_machine.ACTION_ASSET_IMAGE
                            and existing.status == "pending")]
                items.append(item.model_dump(mode="json"))
                changes["status"] = step_machine.STATUS_AWAITING_HUMAN
                changes["jobs"] = jobs
                changes["action_items"] = items
            return changes

        next_status = (step_machine.STATUS_RUNNING if next_job is not None
                       else step_machine.STATUS_AWAITING_HUMAN)
        events = [EventDraft("job.succeeded", {"next": next_status}, job_id=job_id)]
        if next_job is not None:
            events.append(EventDraft(
                "job.started", {"kind": next_job.kind}, job_id=next_job.job_id))
        else:
            assert item is not None
            events.append(EventDraft(
                "action_item.created",
                {"item_id": item.item_id, "kind": item.kind,
                 "asset_index": number, "reason": reason}, job_id=job_id))
        self.store.mutate(session_id, _apply, events=events)
        return next_job

    def _execute_segment_planning(self, session_id: str, job_id: str,
                                  cancel_event: threading.Event) -> list[dict[str, Any]]:
        """线程里的分段规划（D6 spec §6）：复用 ``plan_segments``，产出只读段表。

        输入是阶段 1 的 ``shot_table``；``mode="base"`` 的段长上限是
        ``BASE_MAX_SEG_S``（4–10 秒，ADR 0012）。读图与用户修订按 CLI 的同一口径
        显式入参——规划层不读 state，看到什么在调用点一眼可见（issue #26）。
        ``segments/plan.json`` 落在**本会话目录**内，与 CLI 的同名产物互不覆盖。

        路线从 ``brief`` 读（D7 §10 / #258），不再硬编 ``"base"``：ref 走
        ``prepare_ref_segmentation`` 那一套（切点边界、段内素材子集、顺序卡与四列
        分镜表），并且**≤ 单次生成上限也不跳过分段**——直出形态同样是一个执行段。
        """
        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        shot_table = str(session.stage_state.get("shot_table") or "")
        if not shot_table.strip():
            raise RuntimeError("阶段 1 的分镜表为空，无法分段规划")

        self._emit_progress(
            session_id, job_id, SEGMENT_PLAN_NODE, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, SEGMENT_PLAN_NODE)
        if session.brief.is_ref:
            plans = self._plan_ref_segments(session_id, job_id, directory, session)
            if cancel_event.is_set():
                raise JobCancelled(SEGMENT_PLAN_NODE)
        else:
            plans = self._plan_base_segments(session, shot_table)
            # 规划是一次慢速模型调用：按「当前步骤结束才停」的取消语义收尾。
            if cancel_event.is_set():
                raise JobCancelled(SEGMENT_PLAN_NODE)
            if not plans:
                raise RuntimeError(
                    "分段规划回复不合格（段长档/边界/段尾钩子），没有可用的段表")
            self._write_segment_plan(directory, plans)
        self._emit_progress(
            session_id, job_id, SEGMENT_PLAN_NODE, "done",
            stage=step_machine.STEP_SEGMENTING)
        return [self._segment_row(index, plan) for index, plan in enumerate(plans)]

    def _plan_base_segments(self, session: Any, shot_table: str) -> Any:
        """base 路线的分段规划：一字不变地走 ``plan_segments(mode="base")``。"""
        if self.fake_pipeline:
            # 桩模式一个模型都不建（D4 spec §11）：桩规划与真实规划同一份段长上限。
            from .fake_pipeline import segment_plan

            return segment_plan(int(round(session.brief.duration)))
        from ..model_factory import build_chat_model
        from ..segment_planner import plan_segments
        from ..segment_prompts import frame_anchor_context, render_revision_context

        return plan_segments(
            shot_table, session.brief.duration, build_chat_model(),
            # 图是用户提交的产物、分镜表是计划：冲突以图为准（与 CLI 同源）。
            frame_context=frame_anchor_context(session.stage_state),
            revision_context=render_revision_context(session.stage_state),
            mode="base")

    def _plan_ref_segments(self, session_id: str, job_id: str,
                           directory: Path, session: Any) -> Any:
        """ref 路线的分段规划：整片提示词 + 台账 → 段表、顺序卡、四列分镜表。

        两条路线共用 ``prepare_ref_segmentation``（不另写一套分段算法）。分流的是
        **段数形态**：≤ ``MAX_SEG_S`` 时整片就是唯一执行段（``is_single_segment_route``
        从常量派生，不写字面 15），段词直接取整片提示词原样——直出路线不重写、不套
        分段注记（段数=1 无从对位，与 CLI ``single_segment_result`` 同口径）。
        """
        from ..ref_segments import is_single_segment_route, prepare_ref_segmentation

        whole_prompt = str(session.stage_state.get("final_prompt") or "")
        if not whole_prompt.strip():
            raise RuntimeError("阶段 2 的整片提示词为空，无法做多参分段规划")
        ref_meta, asset_paths = self._ref_plan_inputs(session)
        film_media = self._ref_film_media(session)
        single = is_single_segment_route(session.brief.duration)
        llm = None
        if not single and not self.fake_pipeline:
            from ..model_factory import build_chat_model

            llm = build_chat_model()
        # 直出形态传 llm=None：它本来就不拆段，多一次规划调用只会得到同一个窗口。
        job = prepare_ref_segmentation(
            whole_prompt=whole_prompt, total_s=session.brief.duration,
            out_dir=directory, llm=llm, ref_meta=ref_meta,
            asset_paths=asset_paths or None, film_media=film_media)
        if single:
            # 段词＝整片六段式原样（直出）。写段 job 走 ``reuse_written`` 直接复用这份
            # 文件，不再付一次 LLM——整片提示词就是那一段的提示词。
            atomic_write_text(
                directory / SEGMENTS_DIRNAME / "segment-01.md", whole_prompt)
        # 两件多参只读清单由 ``prepare_ref_segmentation`` 落盘，这里登记进产物表
        # （plan.json 是机器结构，已由 SegmentOut 表达，不进产物）。
        for name in (ARTIFACT_CONCAT_ORDER_CARD, ARTIFACT_REF_SHOT_TABLE):
            self.store.register_artifact(session_id, name, job_id=job_id)
        return list(job.plans)

    def _ref_plan_inputs(
            self, session: Any) -> tuple[list[tuple[int, str, str]], dict[int, str]]:
        """台账 → 段内子集算法要的 ``(ref_meta, 原号→正本路径)``。

        编号与语义名取**台账**（D7 §6.4 的会话真源），不按剧本重排：工作台的编号在
        上传时就定了，规划再排一次会让段内重编号与素材台账对不上。
        """
        ref_meta: list[tuple[int, str, str]] = []
        asset_paths: dict[int, str] = {}
        for entry in session.asset_ledger:
            if str(entry.get("kind")) != KIND_PICTURE:
                continue
            number = int(entry["number"])
            ref_meta.append((number, str(entry.get("name") or ""), ""))
            try:
                asset_paths[number] = str(
                    self.store.resolve_asset_media(str(entry["asset_id"])).path)
            except Exception as exc:  # noqa: BLE001 - 卡上少一条路径，不阻断规划
                logger.warning("第 %d 件素材的正本解析不到（%s）：%s",
                               number, entry.get("asset_id"), exc)
        return ref_meta, asset_paths

    @staticmethod
    def _ref_film_media(session: Any) -> list[Any]:
        """整片视频/音频（没有段内子集，每次请求都带全）：容量闸的另一个来源。

        台账住会话状态，不是 ``out_dir`` 里那份 CLI 台账文件，所以显式交给
        ``_check_segment_capacity``——9/3/3/12 的数字不在这里重抄一份。
        """
        from ..ref_capacity import ReferenceItem

        return [
            ReferenceItem(str(entry["kind"]), int(entry["number"]),
                          str(entry.get("name") or ""))
            for entry in session.asset_ledger
            if str(entry.get("kind")) != KIND_PICTURE
        ]

    @staticmethod
    def _write_segment_plan(directory: Path, plans: Any) -> None:
        """段表落盘：形状与 CLI 的 ``segments/plan.json`` 一致（``to_dict`` 列表）。"""
        target = directory / "segments" / SEGMENT_PLAN_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            target,
            json.dumps([plan.to_dict() for plan in plans],
                       ensure_ascii=False, indent=2))

    @staticmethod
    def _segment_row(index: int, plan: Any) -> dict[str, Any]:
        """规划结果 → 快照里的段条目（``index`` 转成 1 起的执行段号）。

        段内参考素材子集（D7 §10 / #258）随段条目只读回显：``number`` 是段内重编号
        后的号（段词正文引用的就是它），``original_number`` 是整片原号。子集来自规划期
        定案并落进 plan.json 的那一份，不在这里重算。
        """
        assets: list[dict[str, Any]] = []
        stored = getattr(plan, "assets_in_segment", None)
        if stored:
            from ..segment_refs import SegmentAssetScope

            scope = SegmentAssetScope.from_dict(stored)
            assets = [
                {"number": item.new_picture, "name": item.name,
                 "original_number": item.original_picture}
                for item in scope.pictures
            ]
        return {
            "index": index + 1,
            "start_s": int(plan.start_s),
            "end_s": int(plan.end_s),
            "shots_in_segment": [int(shot) for shot in plan.shots_in_segment],
            "summary": str(plan.summary),
            "end_hook": str(plan.end_hook),
            "status": SEGMENT_STATUS_PENDING,
            "prompt_artifact_id": "",
            "video_asset_id": "",
            "assets": assets,
        }

    def _segment_step(self, session_id: str) -> tuple[str, int]:
        """下一次 segmenting job 该做哪件：看**已落盘**的段状态，不看 job 记录。

        段按 index 顺序推进：当前段＝第一条非 passed 的段。段状态本身就是步骤判据，
        于是失败重试无需额外记账——``validating`` 重跑校验、``pending`` 重跑写词。
        全部段都 passed 时只剩拼接（``assemble``）：拼接失败会落 ``failed``，重试时
        这里照样给出 ``assemble``，不需要另记一笔「还没拼」。
        """
        session = load_session(self.store.session_directory(session_id))
        if session is None or not session.segments:
            return (SEGMENT_STEP_PLANNING, 0)
        if session.brief.is_director:
            return self._director_segment_step(session_id, session)
        for row in sorted(
                session.segments, key=lambda item: int(item.get("index", 0))):
            status = str(row.get("status") or "")
            if status == SEGMENT_STATUS_PASSED:
                continue
            number = int(row.get("index") or 0)
            if status == SEGMENT_STATUS_VALIDATING:
                return (SEGMENT_STEP_VALIDATE, number)
            if status == SEGMENT_STATUS_PENDING:
                return (SEGMENT_STEP_WRITE, number)
            # waiting_video：在等人交视频，本不该有 job 在跑；给一个温和收尾。
            return (SEGMENT_STEP_DONE, number)
        return (SEGMENT_STEP_ASSEMBLE, 0)

    def _director_segment_step(
        self, session_id: str, session: Any,
    ) -> tuple[str, int]:
        """导演台段循环下一次做什么：**全部读盘**（段状态 + 词文件 + 段首图 sidecar）。

        与 base/ref 的区别只在「pending 的下一步」：一段在等视频之前，先要按需词
        （切镜段首帧词、运镜/收尾段尾帧词），切镜段还要等用户挂一张本段首帧图。
        段状态仍是同一套四态——``validating`` 只表示「视频收了、慢收尾还没落完」。
        """
        from ..director import frame_meta
        from ..director.frames import FIRST_SEGMENT_TYPE, needs_first_frame_image
        from ..director.pack import film_end_still
        from ..director.planning import plan_path, read_plan_json
        from ..director.firstframe import FIRST_FRAME_IMAGE_FIELD, firstframe_path
        from ..director.tailframe import needs_tailframe, tailframe_path

        directory = self.store.session_directory(session_id)
        # 段表先读、先校验：坏表不能在「当前段在等视频」这一支被静默放过——续跑
        # 之后交付要读同一张表，坏表晚一点炸只会让用户更晚知道（#262 的同一条纪律）。
        try:
            plan = read_plan_json(plan_path(directory))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"导演台段表读不回来，段循环无法继续：{exc}") from exc
        current = next(
            (row for row in sorted(
                session.segments, key=lambda item: int(item.get("index", 0)))
             if str(row.get("status")) != SEGMENT_STATUS_PASSED), None)
        if current is None:
            # 全部段通过：交付待办**已决议**（带两个素材 id）才跑交付 job；否则是
            # 刚挂出待办、正等用户交件——`_segment_step` 本不该被调到，但用它兜住
            # 能让「技巧性命名」这种错判退化成一次温和收尾，而不是起一份空交付。
            if self._director_delivery_submitted(session):
                return (DIRECTOR_STEP_DELIVERY, 0)
            return (DIRECTOR_STEP_DONE, 0)
        number = int(current.get("index") or 0)
        status = str(current.get("status") or "")
        if status == SEGMENT_STATUS_VALIDATING:
            return (DIRECTOR_STEP_FINALIZE, number)
        if status in (SEGMENT_STATUS_WAITING_VIDEO, SEGMENT_STATUS_PASSED):
            return (DIRECTOR_STEP_DONE, number)

        segment = next(
            (item for item in plan.segments if item.index == number), None)
        if segment is None:
            raise RuntimeError(f"导演台段表里找不到第 {number} 段")
        # 切镜段：首帧词自动产出；产完后等用户把用它生成的图挂回来。开场段的首帧
        # 已经在阶段 2 前的 director_start_image 收过，不再重复产词（CLI 同一判据）。
        if (segment.type != FIRST_SEGMENT_TYPE
                and needs_first_frame_image(segment.type, plan.mode)):
            prompt = firstframe_path(directory, number)
            if not prompt.is_file():
                return (DIRECTOR_STEP_FIRSTFRAME, number)
            meta = frame_meta.read_frame_meta(prompt) or {}
            image = str(meta.get(FIRST_FRAME_IMAGE_FIELD) or "")
            if not image or not Path(image).is_file():
                return (DIRECTOR_STEP_SEGMENT_IMAGE, number)
        # 运镜/收尾段：尾帧词自动产出；``ending`` 有作者给的全片尾帧时跳过（#149：
        # 作者终点优先，不写词、不花那次模型调用）。
        if needs_tailframe(segment.type, plan.mode):
            skip_ending = (segment.type == "ending"
                           and film_end_still(directory) is not None)
            if not skip_ending and not tailframe_path(directory, number).is_file():
                return (DIRECTOR_STEP_TAILFRAME, number)
        return (DIRECTOR_STEP_WAIT_VIDEO, number)

    @staticmethod
    def _director_delivery_submitted(session: Any) -> bool:
        """交付待办是否已被决议且带着两个素材 id（#266 的续跑判据）。

        只看快照里的事实：崩溃/重启后重放会把这两个 id 补回待办上，所以「该跑交付」
        不依赖任何 job 记录或内存。
        """
        for item in session.action_items:
            if (str(item.get("kind")) != step_machine.ACTION_DIRECTOR_DELIVERY
                    or str(item.get("status")) != "resolved"):
                continue
            if (str(item.get("video_asset_id") or "").strip()
                    and str(item.get("pack_asset_id") or "").strip()):
                return True
        return False

    @staticmethod
    def _load_segment_plans(directory: Path) -> list[Any]:
        """读回本会话的 ``segments/plan.json``（与 CLI 同形，``from_dict`` 往返）。

        写词与校验都要 ``plan.end_hook`` 与段长；plan.json 是规划时落下的**同一套分段**
        事实（重算是另一套边界），所以只读它、不重算。
        """
        path = directory / SEGMENTS_DIRNAME / SEGMENT_PLAN_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        from ..segment_planner import SegmentPlan

        try:
            return [SegmentPlan.from_dict(entry) for entry in raw]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"段表读不回来：{path}（{exc}）") from exc

    def _execute_segment_write(self, session_id: str, job_id: str,
                               cancel_event: threading.Event, number: int) -> dict[str, Any]:
        """写第 ``number`` 段提示词并落成产物（D6 §5.3/§7，D7 §10 分叉）。

        base：请求要带第 N-1 段视频的尾帧锚——它由上一段校验通过时写进
        ``stage_state.bridge_frame_descriptions``，这里只把 state 原样交给写段器，
        不另抄一份锚。写词必须**逐段增量**，不能一次把所有段写完再收视频。

        ref：没有桥接帧，段词落 ``segments/segment-NN.md``（与 base 的 ``shot-NN.md``
        并列的两条模式），走 ``RefSegmentJob.write_segment``。
        """
        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        if session.brief.is_ref:
            return self._write_ref_segment(
                session_id, job_id, cancel_event, number, directory, session)
        plans = self._load_segment_plans(directory)
        plan = next((item for item in plans if item.index == number - 1), None)
        if plan is None:
            raise RuntimeError(f"段表里找不到第 {number} 段")
        state = dict(session.stage_state)
        node = f"{SEGMENT_WRITE_NODE}-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        if self.fake_pipeline:
            from .fake_pipeline import segment_prompt

            text: str | None = segment_prompt(plan, plans, state)
        else:
            from ..model_factory import build_chat_model
            from ..segment_prompts import write_segment_v2

            text = write_segment_v2(plan, plans, state, build_chat_model())
        if cancel_event.is_set():
            raise JobCancelled(node)
        if not text or not str(text).strip():
            raise RuntimeError(f"第 {number} 段写词没有产出可用提示词")
        name = f"{SEGMENTS_DIRNAME}/shot-{number:02d}.md"
        artifact = self.store.write_text_artifact(
            session_id, name, str(text), job_id=job_id)
        self._emit_progress(
            session_id, job_id, node, "done",
            stage=step_machine.STEP_SEGMENTING)
        return {"number": number, "artifact_id": artifact.artifact_id}

    def _write_ref_segment(self, session_id: str, job_id: str,
                           cancel_event: threading.Event, number: int,
                           directory: Path, session: Any) -> dict[str, Any]:
        """多参写段（D7 §10 / #258）：从盘上重建作业，**不重新规划**。

        规划是慢速模型调用，重跑会得到另一套段边界，而已通过的段只对产出它的那一套
        有意义。所以写段只读 ``segments/plan.json``（边界已定案）＋ ``video-prompt.md``
        （写单段的取材素材），这条重建口与 CLI 续跑同源
        （``load_ref_segments_for_resume``）。直出形态的段文件在规划时就写好了，
        ``reuse_written`` 让它零 LLM 直接复用。
        """
        from ..ref_segments import load_ref_segments_for_resume

        ref_meta, _asset_paths = self._ref_plan_inputs(session)
        llm = None
        if not self.fake_pipeline:
            from ..model_factory import build_chat_model

            llm = build_chat_model()
        node = f"{SEGMENT_WRITE_NODE}-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started", stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        job = load_ref_segments_for_resume(
            directory, total_s=session.brief.duration, llm=llm, ref_meta=ref_meta)
        if number > len(job.plans):
            raise RuntimeError(f"段表里找不到第 {number} 段")
        text, _issues = job.write_segment(number)
        if cancel_event.is_set():
            raise JobCancelled(node)
        if not str(text).strip():
            raise RuntimeError(f"第 {number} 段写词没有产出可用提示词")
        name = f"{SEGMENTS_DIRNAME}/segment-{number:02d}.md"
        # 产物写入仍走 store 的唯一入口（原子写 + 登记 + 事件）；``write_segment``
        # 已按 CLI 口径落过同一份文件，这里再写一次是为了让工作台的产物账与事件成对。
        artifact = self.store.write_text_artifact(
            session_id, name, str(text), job_id=job_id)
        # 段级注入卡（#257 的唯一写入口）：段词落地即卡落地，与段词同屏。
        self._write_segment_injection_card(
            session_id, number, job, job_id=job_id)
        self._emit_progress(
            session_id, job_id, node, "done", stage=step_machine.STEP_SEGMENTING)
        return {"number": number, "artifact_id": artifact.artifact_id}

    def _execute_segment_validation(self, session_id: str, job_id: str,
                                    cancel_event: threading.Event,
                                    number: int) -> dict[str, Any]:
        """剥尾帧 → 读图 → 与本段 ``end_hook`` 对照（D6 §5.4 / §8）。

        三种结局严格分开，**只有「通过」与「被拒」在这里返回**：
        - 技术异常（文件读不到、读图服务不可用、判据回复不可解析）一律抛错，由 ``_run``
          落 ``failed``——重试重跑同一次校验，不要求重交素材；
        - 读图与末态不符返回 ``not_reached``，由收尾重建同一段的待办（不是 failed）。

        读图走唯一读图口 ``describe_bridge_frame``（内部 ``describe_image``）；桩管线把这
        一步连同判据一起桩掉，但**剥尾帧始终是真的**（cv2，无需模型）。
        """
        from ..tools.frame_auditor import describe_bridge_frame, extract_last_frame
        from ..segment_validation import END_NOT_REACHED, END_REACHED

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        if session.brief.is_ref:
            # ref 没有桥接帧、也没有「尾帧对照末态」这一道（ADR 0011）：段尾语义在
            # 写段时由段内注记判过，提交时只看技术档（resolve 已判）+ 段词还在。
            return self._validate_ref_segment(
                session_id, job_id, cancel_event, number, directory, session)
        row = next(
            (item for item in session.segments
             if int(item.get("index", 0)) == int(number)), None)
        if row is None:
            raise RuntimeError(f"快照里找不到第 {number} 段")
        asset_id = str(row.get("video_asset_id") or "")
        if not asset_id:
            raise RuntimeError(f"第 {number} 段没有已交的视频")
        # 已交素材的归属/mp4 容器在 resolve 时判过；这里只取路径（失败即技术异常）。
        resource = self.store.resolve_asset_media(asset_id)
        plans = self._load_segment_plans(directory)
        plan = next((item for item in plans if item.index == number - 1), None)
        if plan is None:
            raise RuntimeError(f"段表里找不到第 {number} 段")
        node = f"{SEGMENT_VALIDATE_NODE}-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        tail = extract_last_frame(
            resource.path,
            directory / SEGMENTS_DIRNAME / SEGMENT_BRIDGE_DIRNAME
            / f"shot-{number:02d}-start.png",
            # 首尾帧链恒取末帧（issue #98）：取到倒数第几帧决定下一段回退几帧。
            sharpest=False)
        if cancel_event.is_set():
            raise JobCancelled(node)
        if self.fake_pipeline:
            from .fake_pipeline import judge_segment_end_hook, segment_tail_description

            tail_description = segment_tail_description(plan)
            reached = judge_segment_end_hook(plan.end_hook, tail_description)
        else:
            # 读图返回可空；先落到独立变量再判空，否则 mypy 会按前一分支的 str 收窄。
            described = describe_bridge_frame(tail)
            if not described:
                # 读图服务不可用＝系统故障，不是「用户的视频不合格」。
                raise RuntimeError(f"第 {number} 段尾帧读图失败：视觉服务不可用")
            tail_description = described
            from ..model_factory import build_chat_model
            from ..segment_validation import judge_end_hook

            reached = judge_end_hook(plan.end_hook, tail_description, build_chat_model())
        if cancel_event.is_set():
            raise JobCancelled(node)
        self._emit_progress(
            session_id, job_id, node, "done",
            stage=step_machine.STEP_SEGMENTING)
        return {
            "number": number,
            "outcome": END_REACHED if reached else END_NOT_REACHED,
            "description": tail_description,
            # 拒绝原因要带上「本段要求」：收尾处据此给用户可读的原因（D6 §5.4）。
            "end_hook": plan.end_hook,
        }

    def _validate_ref_segment(self, session_id: str, job_id: str,
                              cancel_event: threading.Event, number: int,
                              directory: Path, session: Any) -> dict[str, Any]:
        """ref 段校验：**不建桥接帧、不剥尾帧、不调读图、不判 end_hook**（ADR 0011）。

        多参的段尾语义靠写段时的段内注记（``validate_ref_segment``）判，不在提交时
        再读一次图：读图判据是首尾帧链的东西（下一段的帧锚要从尾帧来），ref 没有下一段
        的帧锚可喂。这里只剩两件：技术档（resolve 同步判过，重判会漂）与「段词还在」
        ——段落定成 ``passed`` 的前提是这一段的产出与提交对得上。
        """
        from ..segment_validation import END_REACHED

        row = next(
            (item for item in session.segments
             if int(item.get("index", 0)) == int(number)), None)
        if row is None:
            raise RuntimeError(f"快照里找不到第 {number} 段")
        asset_id = str(row.get("video_asset_id") or "")
        if not asset_id:
            raise RuntimeError(f"第 {number} 段没有已交的视频")
        # 归属与容器在 resolve 判过；这里只确认素材仍在盘上（读不到＝技术故障）。
        self.store.resolve_asset_media(asset_id)
        prompt_path = directory / SEGMENTS_DIRNAME / f"segment-{number:02d}.md"
        if not prompt_path.is_file() or not prompt_path.read_text(
                encoding="utf-8").strip():
            raise RuntimeError(
                f"第 {number} 段的段词不见了（{prompt_path.name}）："
                "无从确认这份提交对应哪一段提示词")
        node = f"{SEGMENT_VALIDATE_NODE}-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started", stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        if cancel_event.is_set():
            raise JobCancelled(node)
        self._emit_progress(
            session_id, job_id, node, "done", stage=step_machine.STEP_SEGMENTING)
        # ``description`` 留空：收尾据此跳过桥接帧锚的写入（ref 没有下段的帧锚）。
        return {"number": number, "outcome": END_REACHED,
                "description": "", "end_hook": ""}

    def _execute_segment_assemble(self, session_id: str, job_id: str,
                                  cancel_event: threading.Event) -> Any:
        """把本会话**已通过**的段接成 ``final.mp4``（D6 §9）：只调用 ``assemble_film``。

        ``plans`` 读本会话的 ``segments/plan.json``；``rows`` 由本会话段状态构造——
        CLI 的 ``segments/shot-ledger.jsonl`` 在这里一次都不读（spec §2/§10：两套风格
        互不续接）。「未通过即不拼」不住在这里：它住在 ``assemble_film`` 里，这里只
        提供「哪几段过了」这份事实。
        """
        from ..final_film import ConcatStrategy, assemble_film

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        plans = self._load_segment_plans(directory)
        rows: dict[int, dict[str, Any]] = {}
        for row in session.segments:
            if str(row.get("status")) != SEGMENT_STATUS_PASSED:
                continue
            number = int(row.get("index") or 0)
            asset_id = str(row.get("video_asset_id") or "")
            if not asset_id:
                # 通过却没有视频引用：不当成「这段可以拼」静默放行——缺席的段由
                # ``assemble_film`` 点名，用户才知道该补哪一段。
                continue
            try:
                resource = self.store.resolve_asset_media(asset_id)
            except WorkbenchError as exc:
                raise RuntimeError(
                    f"第 {number} 段的视频素材读不到（{asset_id}）：{exc}") from exc
            rows[number] = {"video": resource.path}

        node = SEGMENT_ASSEMBLE_NODE
        self._emit_progress(
            session_id, job_id, node, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        if cancel_event.is_set():
            raise JobCancelled(node)
        # 拼接口径是**路线属性**（D7 §10 / ``final_film`` 的既有纪律）：首尾帧链的第
        # N+1 段首帧就是第 N 段末帧，原样接会重一帧，所以裁首帧；ref 没有帧链、段与段
        # 之间没有共享首帧，裁首帧只会吃掉用户交的帧（成片少一帧）。
        if session.brief.is_ref:
            strategy = ConcatStrategy.STREAM_COPY_FIRST
        else:
            strategy = ConcatStrategy.TRIM_FIRST_FRAME
        film = assemble_film(
            plans=plans, rows=rows, total_s=int(session.brief.duration),
            out_path=directory / ARTIFACT_FINAL_FILM,
            strategy=strategy,
            reencode_crf=16,
            remedy_hint="重跑本段并重新验收通过后，成片会自动取用最新的那份视频。",
        )
        if cancel_event.is_set():
            raise JobCancelled(node)
        if film.path is None:
            # 不产出成片＝任务失败；reason 原样带走（它已经说了缺哪一段）。
            raise SegmentAssemblyRefused(film.reason)
        for note in film.warnings:
            # 成片提醒（只报短、接缝退回重复首帧）没有别的事件位可进，落日志至少留痕。
            logger.warning("成片提醒：%s / %s", session_id, note)
        self._emit_progress(
            session_id, job_id, node, "done",
            stage=step_machine.STEP_SEGMENTING)
        return film

    def _execute_revise(self, session_id: str, job_id: str,
                        cancel_event: threading.Event,
                        entries: list[dict[str, Any]]) -> dict[str, Any]:
        """线程里跑的修订重跑：设定级按层次截断重跑阶段 1，画面级重出关键帧提示词。

        多条修订的执行顺序＝**链上最上游的层次先跑，画面级最后**：设定级改动会换掉
        画面级节点的输入，反过来先跑画面级等于拿旧设定出词。
        """
        if self.fake_pipeline:
            return self._execute_revise_fake(session_id, job_id, cancel_event)
        from ..graph.pipeline import _STAGE1_CHAIN
        from ..user_revisions import (
            LAYER_FRAME,
            LAYER_SPECS,
            SETTING_REVISION_KEY,
            active_revisions,
            log_frame_round,
            mark_revision_applied,
        )

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        brief = self._ref_brief(session)
        state = dict(session.stage_state)
        chain = list(_STAGE1_CHAIN)
        setting_entries = [entry for entry in entries if entry["layer"] in LAYER_SPECS]
        setting_entries.sort(key=lambda entry: chain.index(
            LAYER_SPECS[entry["layer"]].resume_from))
        frame_entries = [entry for entry in entries if entry["layer"] == LAYER_FRAME]

        for entry in setting_entries:
            spec = LAYER_SPECS[entry["layer"]]
            initial_state = dict(state)
            # 本轮这条修订经独立入参键进重跑（唯一读者 user_revisions.setting_revision_block）
            initial_state[SETTING_REVISION_KEY] = {
                "layer": spec.value, "text": entry["text"]}
            state = self._rerun_stage1(
                session_id, job_id, cancel_event, brief, initial_state, spec.resume_from)
            # 入参块与断点元数据都不是产物，不许顺着 state 流到下一次重跑或阶段 2。
            state.pop(SETTING_REVISION_KEY, None)
            state.pop("_progress", None)
            # 先标已落地再落盘：清单与台账记的必须是同一个事实（重跑失败时走不到这里）。
            mark_revision_applied(state, entry)
            self._save_revision_state(session_id, state)
            log_frame_round(
                directory, round_index=int(entry["round"]), layer=spec.value,
                feedback_this_round=entry["text"],
                active_revisions=active_revisions(state),
                bundle=state.get("fl2va_prompt_bundle"),
                base_used=False)  # 设定级重跑不上基线（它改的就是设定产物本身）

        if frame_entries:
            state = self._rerun_frame_prompts(
                session_id, job_id, cancel_event, brief, state, frame_entries)
            self._save_revision_state(session_id, state)

        state.pop("_progress", None)
        # 每轮修订重跑完成都把审查面刷新一遍（spec §7 / §14 风险表）：审查看到的
        # 必须是这一稿，而不是上一轮意见之前的旧稿。
        self._write_stage1_artifacts(session_id, job_id, state)
        # 修订重跑用的是派生的 brief（带台账 refs）；回执里交回原样，避免任何人顺手
        # 把它写回会话文件（台账是唯一真源）。
        return {"state": state, "brief": session.brief}

    def _execute_revise_fake(self, session_id: str, job_id: str,
                             cancel_event: threading.Event) -> dict[str, Any]:
        """桩修订：把本轮意见拼进固定稿并刷新产物，不区分层次。

        内容是假的不等于形状是假的：state 里仍带累积修订（``_decide`` 已写入），
        产物仍走同一个落盘入口——浏览器 E2E 要验的是「重跑后看到新稿」。
        """
        from ..graph.pipeline import _STAGE1_CHAIN
        from .fake_pipeline import stage1_state

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        self._emit_progress(session_id, job_id, _STAGE1_CHAIN[0], "started")
        if cancel_event.is_set():
            raise JobCancelled(_STAGE1_CHAIN[0])
        state = stage1_state(self._ref_brief(session), dict(session.stage_state))
        self._emit_progress(session_id, job_id, _STAGE1_CHAIN[-1], "done")
        self._write_stage1_artifacts(session_id, job_id, state)
        return {"state": state, "brief": session.brief}

    def _save_revision_state(self, session_id: str,
                             state: dict[str, Any]) -> None:
        """把一次重跑后的 state 落盘（会话仍在 running）：已落地标记与累积清单不能丢。"""
        self.store.mutate(session_id, lambda _detail: {
            "status": step_machine.STATUS_RUNNING, "stage_state": state})

    def _rerun_stage1(self, session_id: str, job_id: str,
                      cancel_event: threading.Event, brief: Any,
                      initial_state: dict[str, Any],
                      start_node: str) -> dict[str, Any]:
        """从 ``start_node`` 截断重跑阶段 1（复用 ``run_stage1(resume_from_node=...)``）。"""
        from ..graph.pipeline import _STAGE1_CHAIN, run_stage1

        steps = list(_STAGE1_CHAIN)[
            list(_STAGE1_CHAIN).index(start_node):]

        def on_step_done(node_name: str, current_state: dict) -> None:
            if node_name in steps:
                progress_state = dict(current_state)
                progress_state["_progress"] = {"last_completed_node": node_name}
                self._save_progress(
                    session_id, job_id, progress_state, node_name)
                self._emit_progress(session_id, job_id, node_name, "done")
            if cancel_event.is_set():
                raise JobCancelled(node_name)

        # 只报起点开始、随后按真实回调逐个报完成：中间有些节点在本路线不出产物、
        # 回调根本不会来，替它们编 started 会留下一条永远不结束的假事件。
        self._emit_progress(session_id, job_id, steps[0], "started")
        return dict(run_stage1(
            brief, self.config, initial_state=initial_state,
            resume_from_node=start_node, on_step_done=on_step_done)[0])

    def _rerun_frame_prompts(self, session_id: str, job_id: str,
                             cancel_event: threading.Event, brief: Any,
                             state: dict[str, Any],
                             entries: list[dict[str, Any]]) -> dict[str, Any]:
        """画面级修订：按向导既有语义重出 ``fl2va_frame_prompts``（基线块注入）。

        节点从 state 读累积清单（``render_revision_block(pending_revisions(...))``），
        所以这里只负责补上**修订基线**这个独立入参键——与 ``_user_revision_loop``
        同一条接线。撤销块留空：决议接口这一版不承载撤销（清单的撤走走向导）。
        """
        from ..graph.nodes import make_nodes
        from ..ui.wizard import _build_role_agents_on_demand
        from ..user_revisions import (
            FRAME_BASELINE_KEY,
            FRAME_REVOKED_KEY,
            LAYER_FRAME,
            active_revisions,
            log_frame_round,
            render_baseline_block,
        )

        directory = self.store.session_directory(session_id)
        model, agents = _build_role_agents_on_demand(brief)
        nodes = make_nodes(agents, model, brief, self.config)
        updated = dict(state)
        updated["brief"] = brief
        baseline = render_baseline_block(state.get("fl2va_prompt_bundle"))
        if baseline:
            updated[FRAME_BASELINE_KEY] = baseline
        else:
            updated.pop(FRAME_BASELINE_KEY, None)
        updated.pop(FRAME_REVOKED_KEY, None)

        self._emit_progress(
            session_id, job_id, step_machine.FRAME_PROMPT_NODE, "started")
        update = nodes[step_machine.FRAME_PROMPT_NODE](updated)
        # 台账的 base_used 取「节点真的拿到了基线」，不重判渲染结果（同向导接线）。
        base_used = FRAME_BASELINE_KEY in updated
        if isinstance(update, dict):
            updated.update(update)
        # 基线是本轮入参、不是产物：跑完即摘，不许顺着 state 流到阶段 2。
        updated.pop(FRAME_BASELINE_KEY, None)
        self._emit_progress(
            session_id, job_id, step_machine.FRAME_PROMPT_NODE, "done")
        if cancel_event.is_set():
            raise JobCancelled(step_machine.FRAME_PROMPT_NODE)

        for entry in entries:
            log_frame_round(
                directory, round_index=int(entry["round"]), layer=LAYER_FRAME,
                feedback_this_round=entry["text"],
                active_revisions=active_revisions(updated),
                bundle=updated.get("fl2va_prompt_bundle"),
                base_used=base_used)
        return updated

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def _job_or_running(self, job_id: str, detail: Any) -> JobSummary:
        job = _find_job(detail.jobs, job_id)
        return job or JobSummary(job_id=job_id, status="running")

    def _succeed_job(self, detail: Any, job_id: str, *,
                     detail_text: str | None = None) -> list[dict[str, Any]]:
        """把本任务标成 ``succeeded`` 并 upsert 回快照，返回新的 jobs 列表。

        十几个收尾闭包逐字重复这三行（#253 复审：``_hang_director_todo`` /
        ``_finish_director_finalize`` / ``_finish_pack`` 等）。status 与 finished_at
        的写法一旦改口径，漏一处就是某个任务永远停在 running——单住这一处。
        ``detail_text`` 只在需要把结论写进 job 详情的收尾上传。
        """
        update: dict[str, Any] = {"status": "succeeded", "finished_at": _now()}
        if detail_text is not None:
            update["detail"] = detail_text
        job = self._job_or_running(job_id, detail).model_copy(update=update)
        return _upsert_job(detail.jobs, _job_dict(job))

    def _save_progress(self, session_id: str, job_id: str,
                       stage_state: dict[str, Any], node: str, *,
                       elapsed_ms: int | None = None) -> None:
        def _apply(detail: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, detail)
            return {
                "status": "running",
                "stage_state": stage_state,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
            }

        payload: dict[str, Any] = {
            "stage": step_machine.STEP_STAGE1, "node": node, "status": "done"}
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        # 读与写在存储层会话锁内完成：取消请求可能刚把 job 标成 cancel_requested，
        # 先读后写会把那个标记覆盖回旧值（#217 复审）。事件与快照同一把锁、先事件后
        # 快照，序号与状态转移因此不会分家（#218）。
        self.store.mutate(
            session_id, _apply,
            events=[EventDraft("job.progress", payload, job_id=job_id)])
        self._write_active_job(session_id, job_id, node)

    def _emit_progress(self, session_id: str, job_id: str, node: str,
                       status: str, *,
                       stage: str = step_machine.STEP_STAGE1) -> None:
        """没有状态变化的进度（节点开始）：只追加事件，不动快照字段。"""
        self.store.record_events(session_id, [
            EventDraft("job.progress",
                       {"stage": stage, "node": node, "status": status},
                       job_id=job_id)])

    def _finish_succeeded(self, session_id: str, job_id: str,
                          stage_state: dict[str, Any]) -> None:
        item = ActionItemOut(
            item_id=uuid.uuid4().hex, kind=step_machine.ACTION_STAGE1_REVIEW,
            status="pending", created_at=_now())

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            # 「恰好一条」是 AC5 的原话：同一种待办若有未决的旧项，先收掉再挂新的。
            items = [
                existing.model_dump(mode="json") for existing in detail.action_items
                if not (existing.kind == step_machine.ACTION_STAGE1_REVIEW
                        and existing.status == "pending")
            ]
            items.append(item.model_dump(mode="json"))
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "stage_state": stage_state,
                "jobs": jobs,
                "action_items": items,
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_AWAITING_HUMAN},
                       job_id=job_id),
            EventDraft("action_item.created",
                       {"item_id": item.item_id, "kind": item.kind},
                       job_id=job_id),
        ])

    # ------------------------------------------------------------------
    # 导演台段循环（D8 §5.5-§5.6 / §8.3-§8.4 / #264）
    # ------------------------------------------------------------------

    @staticmethod
    def _director_plan_segment(directory: Path, number: int) -> tuple[Any, Any]:
        """读回段表与第 ``number`` 段；坏表/缺段都是技术故障（不能让 job 静默跳过）。"""
        from ..director.planning import plan_path, read_plan_json

        try:
            plan = read_plan_json(plan_path(directory))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"导演台段表读不回来，第 {number} 段推进不了：{exc}") from exc
        segment = next(
            (item for item in plan.segments if item.index == int(number)), None)
        if segment is None:
            raise RuntimeError(f"导演台段表里找不到第 {number} 段")
        return plan, segment

    def _execute_director_firstframe(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event, number: int,
    ) -> dict[str, Any]:
        """切镜段的段首帧词（#264）：轮到该段才写，盘上已有就沿用、不重付。

        慢调用（模型）只在这里；请求阶段只起 job。
        """
        from ..director.firstframe import firstframe_path, write_firstframe_prompt

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        path = firstframe_path(directory, number)
        if path.is_file():
            return {"number": number, "skipped": True}
        _plan, segment = self._director_plan_segment(directory, number)
        node = f"director_firstframe-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        if self.fake_pipeline:
            from . import fake_pipeline

            text = fake_pipeline.director_firstframe_prompt(segment)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text.rstrip() + "\n")
        else:
            from ..image_config import load_frame_image_model

            text = write_firstframe_prompt(
                segment, llm=build_chat_model(),
                shot_table=str(session.stage_state.get("shot_table") or ""),
                brief=session.brief, session_dir=directory,
                model=load_frame_image_model(), state=session.stage_state)
            if not text:
                raise RuntimeError(
                    f"第 {number} 段段首帧词没有产出（调用失败或回复里没有正文）")
        if cancel_event.is_set():
            raise JobCancelled(node)
        self.store.register_artifact(
            session_id, director_first_frame_prompt_name(number), job_id=job_id)
        self._emit_progress(
            session_id, job_id, node, "done",
            stage=step_machine.STEP_SEGMENTING)
        return {"number": number, "skipped": False}

    def _director_start_image(
        self, directory: Path, plan: Any, segment: Any,
    ) -> Path:
        """本段尾帧词的起点画面（D8 §8.4）：切镜段用**自己的**首帧图，引导段用
        上一段的终点图，没有就取上一段成片视频的末帧。

        不拿段缓存当参考：插件默认 raw 编码常常读不了；用户实际跑的是哪一版视频，
        提示词就该描述哪一版（#114）。终点图优先于视频末帧：视频末帧每段都会漂。
        """
        from ..director import cache, ledger
        from ..director.frames import needs_first_frame_image
        from ..director.frame_meta import read_frame_meta
        from ..director.firstframe import FIRST_FRAME_IMAGE_FIELD, firstframe_path
        from ..director.tailframe import last_frame_png_path

        if needs_first_frame_image(segment.type, plan.mode):
            meta = read_frame_meta(firstframe_path(directory, segment.index)) or {}
            raw = str(meta.get(FIRST_FRAME_IMAGE_FIELD) or "")
            if not raw or not Path(raw).is_file():
                raise RuntimeError(
                    f"第 {segment.index} 段的段首帧图不见了（sidecar 记的是 {raw or '空'}）："
                    "尾帧词没有可用的起点画面")
            return Path(raw)
        previous = segment.index - 1
        if previous < 1:
            raise RuntimeError(
                f"第 {segment.index} 段不该要终点画面：首段没有起点画面")
        try:
            row = ledger.passed_director_rows(directory).get(previous) or {}
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"段产出账读不回来：{exc}") from exc
        end_image = str(row.get("end_image") or "")
        if end_image and Path(end_image).is_file():
            return Path(end_image)
        video = str(row.get("video") or "")
        if not video or not Path(video).is_file():
            raise RuntimeError(
                f"第 {segment.index} 段要终点画面，但账里没有上一段（第 {previous} 段）"
                "可用的起点画面（终点图与成片视频都没有）")
        return cache.export_last_frame(
            video, last_frame_png_path(directory, previous))

    def _execute_director_tailframe(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event, number: int,
    ) -> dict[str, Any]:
        """运镜/收尾段的尾帧词（#264）：只在轮到时写；``ending`` 有全片尾帧则跳过。"""
        from dataclasses import replace

        from ..director.pack import film_end_still
        from ..director.tailframe import (
            needs_tailframe, tailframe_path, write_tailframe_prompts,
        )

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        plan, segment = self._director_plan_segment(directory, number)
        if not needs_tailframe(segment.type, plan.mode):
            return {"number": number, "skipped": True}
        if segment.type == "ending" and film_end_still(directory) is not None:
            # 作者给的全片终点优先（#149）：不写词、不花那次模型调用。
            return {"number": number, "skipped": True}
        path = tailframe_path(directory, number)
        if path.is_file():
            return {"number": number, "skipped": True}
        start_image = self._director_start_image(directory, plan, segment)
        node = f"director_tailframe-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        if self.fake_pipeline:
            from . import fake_pipeline

            text = fake_pipeline.director_tailframe_prompt(segment, start_image)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text.rstrip() + "\n")
        else:
            written = write_tailframe_prompts(
                replace(plan, segments=(segment,)), llm=build_chat_model(),
                session_dir=directory, start_image=start_image,
                identity_baseline=session.stage_state.get("character_design"))
            if not written:
                raise RuntimeError(f"第 {number} 段尾帧词没有产出")
        if cancel_event.is_set():
            raise JobCancelled(node)
        self.store.register_artifact(
            session_id, director_tailframe_prompt_name(number), job_id=job_id)
        self._emit_progress(
            session_id, job_id, node, "done",
            stage=step_machine.STEP_SEGMENTING)
        return {"number": number, "skipped": False}

    def _director_cache_file(self, session: Any, number: int) -> Path | None:
        """缓存段文件**只作定位**（#112 起不是证据）：找得到就记路径，找不到传 None。"""
        from ..director import cache

        root = getattr(self.config, "director_comfy_output_root", None)
        if root is None:
            return None
        try:
            directory = cache.cache_dir(
                root(), session.brief.director_cache_name,
                session.brief.director_node_id)
            for candidate in cache.segment_cache_files(directory, number):
                if candidate.is_file():
                    return candidate
        except (OSError, ValueError):
            return None
        return None

    def _execute_director_finalize(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event, number: int,
    ) -> dict[str, Any]:
        """视频已收、同步判帧已过：慢收尾——追加账本行并交回通过事实（#264）。

        账本行**幂等**：崩溃后重跑本段收尾时，同一段、同一条视频、同一实际帧数的
        通过行已经在了就不再追加（追加式账不会自己去重）。
        """
        from ..director import ledger

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        row = next(
            (item for item in session.segments
             if int(item.get("index", 0)) == int(number)), None)
        if row is None:
            raise RuntimeError(f"快照里找不到第 {number} 段")
        asset_id = str(row.get("video_asset_id") or "")
        if not asset_id:
            raise RuntimeError(f"第 {number} 段没有已交的视频")
        resource = self.store.resolve_asset_media(asset_id)
        _plan, segment = self._director_plan_segment(directory, number)
        actual, _verdict = self._judge_director_segment_video(
            session_id, number, resource.path)
        node = f"director_finalize-{number:02d}"
        self._emit_progress(
            session_id, job_id, node, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, node)
        previous = ledger.passed_director_rows(directory).get(number)
        already = bool(
            previous
            and Path(str(previous.get("video") or "")).resolve()
            == resource.path.resolve()
            and previous.get("actual_frames") == actual)
        if not already:
            ledger.log_director_output(
                directory,
                segment=number,
                start_frame=segment.start_frame,
                end_frame=segment.end_frame,
                declared_frames=segment.declared_frames,
                export_frames=segment.export_frames,
                actual_frames=actual,
                video_path=resource.path,
                cache_path=self._director_cache_file(session, number),
            )
        if cancel_event.is_set():
            raise JobCancelled(node)
        self._emit_progress(
            session_id, job_id, node, "done",
            stage=step_machine.STEP_SEGMENTING)
        return {"number": number, "actual_frames": actual,
                "already_logged": already}

    def _execute_director_delivery(
        self, session_id: str, job_id: str,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        """交付归档的线程体（D8 §12.2 / #266）：复制成片 → ``deliver`` → 补宽高。

        读的是**快照里已决议的交付待办**（不是本次 resolve 的参数）：崩溃/重启后
        ``advance`` 重跑同一份交付时，素材 id 只能从那里拿回来。
        """
        import shutil

        from ..director import delivery
        from ..director.planning import plan_path, read_plan_json
        from ..tools.frame_match import video_meta

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        item = next(
            (row for row in session.action_items
             if str(row.get("kind")) == step_machine.ACTION_DIRECTOR_DELIVERY
             and str(row.get("status")) == "resolved"
             and str(row.get("video_asset_id") or "").strip()
             and str(row.get("pack_asset_id") or "").strip()),
            None)
        if item is None:
            raise RuntimeError(
                "交付待办已决议但读不到两个素材 id：不能归档半份交付")
        video = self.store.resolve_video_asset(
            session_id, str(item["video_asset_id"]))
        pack = self.store.resolve_pack_asset(
            session_id, str(item["pack_asset_id"]))
        try:
            plan = read_plan_json(plan_path(directory))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"导演台段表读不回来，无法交付：{exc}") from exc
        self._emit_progress(
            session_id, job_id, DIRECTOR_DELIVERY_NODE, "started",
            stage=step_machine.STEP_SEGMENTING)
        self._write_active_job(session_id, job_id, DIRECTOR_DELIVERY_NODE)
        if cancel_event.is_set():
            raise JobCancelled(DIRECTOR_DELIVERY_NODE)
        # 成片落在固定名 ``final.mp4``（既有 video 产物）：产物分区与 final-film.json
        # 因此指向同一份文件，不是两套坐标。
        film = directory / ARTIFACT_FINAL_FILM
        existed = film.exists()
        shutil.copyfile(video.path, film)
        try:
            report = delivery.deliver(
                directory, plan=plan, film_path=film, pack_zip=pack.path)
        except Exception:
            # deliver 先在内存里校验完才落盘；走到这里说明连成片副本都不该留。
            # 只删这一次自己建的那份，不碰用户原有的文件。
            if not existed:
                film.unlink(missing_ok=True)
            raise
        # 宽高是工作台这一层追加的（spec §13）：CLI 的 ``record_final_film`` 不写它，
        # 改它会把 CLI 的记录形状也一起改掉。
        record_path = delivery.final_film_path(directory)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        meta = video_meta(film)
        width = int(meta.get("width") or 0)
        height = int(meta.get("height") or 0)
        record["width"] = width
        record["height"] = height
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        self._emit_progress(
            session_id, job_id, DIRECTOR_DELIVERY_NODE, "done",
            stage=step_machine.STEP_SEGMENTING)
        detail = "\n".join([
            report.render().strip(),
            "",
            f"成片宽高：{width}×{height}",
            "初始包＝计划，最终包＝结论：改词后的差异已补记成 rewrite 行，"
            "初始包没有被重建。",
        ])
        return {
            "changes": len(report.changes),
            "detail": detail,
        }

    def _finish_director_delivery(
        self, session_id: str, job_id: str, result: dict[str, Any],
    ) -> JobSummary | None:
        """交付成功：登记三件产物并置 ``completed``（D8 §12 / #266）。

        先登记再落终态（事件说的必须是已发生的事实）：``final.mp4``、最终包、
        成片记录都已落盘；校验值不单独登记（spec §13），它随最终包显示。
        """
        self.store.register_artifact(
            session_id, ARTIFACT_FINAL_FILM, job_id=job_id)
        self.store.register_artifact(
            session_id, ARTIFACT_DIRECTOR_FINAL_PACK, job_id=job_id)
        self.store.register_artifact(
            session_id, ARTIFACT_DIRECTOR_FINAL_FILM, job_id=job_id)

        def _apply(current: Any) -> dict[str, Any]:
            return {
                "status": step_machine.STATUS_COMPLETED,
                "jobs": self._succeed_job(
                    current, job_id,
                    detail_text=str(result.get("detail") or "")),
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_COMPLETED}, job_id=job_id),
        ])
        return None

    def _hang_director_todo(
        self, session_id: str, job_id: str, number: int, todo_kind: str,
    ) -> JobSummary | None:
        """挂出导演台段循环的下一步待办（#264），并把本段状态推到对应档位。

        两种待办共用这一处：``director_segment_image`` 只等图（段仍是 ``pending``）；
        ``segment_video`` 等视频（段转 ``waiting_video``）。每段恰好一个同类待办。
        """
        waits_video = todo_kind == step_machine.ACTION_SEGMENT_VIDEO
        item = ActionItemOut(
            item_id=uuid.uuid4().hex, kind=todo_kind, status="pending",
            created_at=_now(), segment_index=number)

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            rows = [row.model_dump() for row in detail.segments]
            if waits_video:
                for row in rows:
                    if int(row["index"]) == number:
                        row["status"] = SEGMENT_STATUS_WAITING_VIDEO
                        row["rejection_reason"] = ""
            items = [
                existing.model_dump(mode="json")
                for existing in detail.action_items
                if not (existing.kind == todo_kind
                        and existing.status == "pending")]
            items.append(item.model_dump(mode="json"))
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": jobs,
                "segments": rows,
                "action_items": items,
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_AWAITING_HUMAN},
                       job_id=job_id),
            EventDraft("action_item.created",
                       {"item_id": item.item_id, "kind": item.kind,
                        "segment_index": number}, job_id=job_id),
        ])
        return None

    def _finish_director_firstframe(
        self, session_id: str, job_id: str, result: dict[str, Any],
    ) -> JobSummary | None:
        """段首帧词写完：挂出本段的 ``director_segment_image`` 待办（#264）。"""
        return self._hang_director_todo(
            session_id, job_id, int(result["number"]),
            step_machine.ACTION_DIRECTOR_SEGMENT_IMAGE)

    def _finish_director_tailframe(
        self, session_id: str, job_id: str, result: dict[str, Any],
    ) -> JobSummary | None:
        """尾帧词写完（或全片尾帧已给、跳过）：挂出本段 ``segment_video`` 待办。"""
        return self._hang_director_todo(
            session_id, job_id, int(result["number"]),
            step_machine.ACTION_SEGMENT_VIDEO)

    def _finish_director_todo(
        self, session_id: str, job_id: str, result: dict[str, Any],
    ) -> JobSummary | None:
        """「只剩挂待办」的步骤（无慢调用）：按 todo 类型落到同一处挂法。"""
        todo_kind = (
            step_machine.ACTION_SEGMENT_VIDEO
            if result.get("todo_kind") == DIRECTOR_STEP_WAIT_VIDEO
            else step_machine.ACTION_DIRECTOR_SEGMENT_IMAGE)
        return self._hang_director_todo(
            session_id, job_id, int(result["number"]), todo_kind)

    def _finish_director_finalize(
        self, session_id: str, job_id: str, result: dict[str, Any],
    ) -> JobSummary | None:
        """通过行落盘后：段进 ``passed``，立刻起下一段的 job（#264）。

        下一段该写词还是只挂待办由 ``_segment_step`` 按盘判；这里只负责把 job 放出去，
        不在这儿重写一套子步骤判据。
        """
        number = int(result["number"])
        session = load_session(self.store.session_directory(session_id))
        total = len(session.segments) if session is not None else 0
        next_job = (
            JobSummary(job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT,
                       status="running", started_at=_now())
            if number < total else None)
        next_status = (step_machine.STATUS_RUNNING if next_job is not None
                       else step_machine.STATUS_AWAITING_HUMAN)
        # 最后一段通过＝该交付了（D8 §12 / #266）：挂出唯一一道交付闸门，载荷在
        # 决议时才带（两张素材还没交）。这是人工待办，所以会话停在 awaiting_human。
        delivery_item = (
            ActionItemOut(
                item_id=uuid.uuid4().hex,
                kind=step_machine.ACTION_DIRECTOR_DELIVERY,
                status="pending", created_at=_now())
            if next_job is None else None)

        def _apply(detail: Any) -> dict[str, Any]:
            rows = [row.model_dump() for row in detail.segments]
            for row in rows:
                if int(row["index"]) == number:
                    row["status"] = SEGMENT_STATUS_PASSED
                    row["rejection_reason"] = ""
            jobs = self._succeed_job(detail, job_id)
            if next_job is not None:
                jobs = _upsert_job(jobs, _job_dict(next_job))
            changes: dict[str, Any] = {
                "status": next_status,
                "jobs": jobs,
                "segments": rows,
            }
            if delivery_item is not None:
                items = [
                    item.model_dump(mode="json") for item in detail.action_items]
                items.append(delivery_item.model_dump(mode="json"))
                changes["action_items"] = items
            return changes

        events = [
            EventDraft("job.succeeded", {"next": next_status}, job_id=job_id)]
        if next_job is not None:
            events.append(EventDraft(
                "job.started", {"kind": next_job.kind},
                job_id=next_job.job_id))
        if delivery_item is not None:
            events.append(EventDraft(
                "action_item.created",
                {"item_id": delivery_item.item_id,
                 "kind": delivery_item.kind}, job_id=job_id))
        self.store.mutate(session_id, _apply, events=events)
        return next_job

    def _finish_pack(self, session_id: str, job_id: str,
                     result: dict[str, Any]) -> JobSummary | None:
        """打包成功：装配结论与节点宽高进 job 详情，立刻接上段循环（#264）。

        不写 ``completed``：初始包只是「交给导演台去跑」的计划，交付在 #266。
        段循环的第一件（按需词或挂 ``segment_video``）由 ``_segment_step`` 按盘判。
        """
        detail = "\n".join([str(result["report"]), "", *result["node_lines"]])
        session = load_session(self.store.session_directory(session_id))
        start_segments = bool(
            session is not None and session.segments
            and any(str(row.get("status")) != SEGMENT_STATUS_PASSED
                    for row in session.segments))
        next_job = (
            JobSummary(job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT,
                       status="running", started_at=_now())
            if start_segments else None)
        next_status = (step_machine.STATUS_RUNNING if next_job is not None
                       else step_machine.STATUS_AWAITING_HUMAN)

        def _apply(current: Any) -> dict[str, Any]:
            jobs = self._succeed_job(current, job_id, detail_text=detail)
            if next_job is not None:
                jobs = _upsert_job(jobs, _job_dict(next_job))
            return {
                "status": next_status,
                "jobs": jobs,
            }

        events = [
            EventDraft("job.succeeded", {"next": next_status}, job_id=job_id),
        ]
        if next_job is not None:
            events.append(EventDraft(
                "job.started", {"kind": next_job.kind}, job_id=next_job.job_id))
        self.store.mutate(session_id, _apply, events=events)
        return next_job

    def _finish_stage2(self, session_id: str, job_id: str,
                       result: dict[str, Any]) -> JobSummary | None:
        """阶段 2 收尾：短视频 completed；长片改挂分段规划任务（D6 spec §5.3）。

        短视频这条路一个字段都不变（D4 的交付语义：阶段 2 成功即 completed，没有交付
        确认）。长片不能落 completed——规划紧接着跑，会话仍是 running；新任务的摘要
        与旧任务的 ``succeeded`` **在同一次落盘**里写出，读者不会看到「两个任务同时
        running」或「已成功却没人接」的中间态。返回新任务给调用方起执行线程。
        """
        next_job: JobSummary | None = None
        director = bool(result.get("director"))
        if director:
            # 导演台阶段 2 的成功不等于交付：立刻接上**打包**（#263）。与长片的
            # 「阶段 2 后接分段规划」同一形态——新任务摘要与旧任务的 ``succeeded``
            # 在同一次落盘里写出，读者看不到「已成功却没人接」的中间态。
            next_job = JobSummary(
                job_id=uuid.uuid4().hex, kind=JOB_KIND_PACK, status="running",
                started_at=_now())
        elif self._needs_segment_loop(result["brief"]):
            next_job = JobSummary(
                job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT, status="running",
                started_at=_now())
        # 有后续任务时不能落 completed（页面会以为片子做完了）；导演台的交付在 #266。
        next_status = (step_machine.STATUS_RUNNING if next_job is not None
                       else step_machine.STATUS_COMPLETED)

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            if next_job is not None:
                jobs = _upsert_job(jobs, _job_dict(next_job))
            changes: dict[str, Any] = {
                "status": next_status,
                "stage_state": result["state"],
                "jobs": jobs,
                "brief": result["brief"],
                "variant_downgraded": result["variant_downgraded"],
            }
            if director:
                # 段表进快照（#262 AC）：字段来自 director/plan.json，运行态从旧行
                # 保留（重建快照不能把已通过的段抹回 pending）。
                changes["segments"] = result["segments"]
            return changes

        events = [
            EventDraft("job.succeeded", {"next": next_status}, job_id=job_id),
        ]
        if next_job is not None:
            events.append(EventDraft(
                "job.started", {"kind": next_job.kind}, job_id=next_job.job_id))
        self.store.mutate(session_id, _apply, events=events)
        return next_job

    def _finish_segment_planning(self, session_id: str, job_id: str,
                                 segments: list[dict[str, Any]]) -> JobSummary | None:
        """分段规划收尾：段列表进快照，立刻接上第 1 段写词任务（D6 §5.3）。

        规划结果是**只读**的（Q4）：不挂「确认分段」待办、不需要用户点一下。写完即起
        第 1 段写词 job——这一步替换掉 D6-1 留下的「awaiting_human 无待办」过渡态。
        """
        next_job: JobSummary | None = None
        if segments:
            next_job = JobSummary(
                job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT, status="running",
                started_at=_now())
        next_status = (step_machine.STATUS_RUNNING if next_job is not None
                       else step_machine.STATUS_AWAITING_HUMAN)

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            if next_job is not None:
                jobs = _upsert_job(jobs, _job_dict(next_job))
            return {
                "status": next_status,
                "jobs": jobs,
                "segments": segments,
            }

        events = [EventDraft("job.succeeded", {"next": next_status}, job_id=job_id)]
        if next_job is not None:
            events.append(EventDraft(
                "job.started", {"kind": JOB_KIND_SEGMENT}, job_id=next_job.job_id))
        self.store.mutate(session_id, _apply, events=events)
        return next_job

    def _finish_segment_write(self, session_id: str, job_id: str,
                              result: dict[str, Any]) -> JobSummary | None:
        """第 N 段写词收尾：段转「等视频」，挂出本段唯一的 ``segment_video`` 待办。

        写词/校验/交视频的顺序按 spec §5.3：一段写完就停下来收本段视频，绝不一次写完全
        部段。待办载荷带段号（``segment_index``），等待期间会话是 ``awaiting_human``。
        """
        number = int(result["number"])
        artifact_id = str(result["artifact_id"])
        item = ActionItemOut(
            item_id=uuid.uuid4().hex, kind=step_machine.ACTION_SEGMENT_VIDEO,
            status="pending", created_at=_now(), segment_index=number)

        def _apply(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            rows = [row.model_dump() for row in detail.segments]
            for row in rows:
                if int(row["index"]) == number:
                    row["status"] = SEGMENT_STATUS_WAITING_VIDEO
                    row["prompt_artifact_id"] = artifact_id
            # 每段恰好一个待办：同类未决旧项先收掉再挂新的（与阶段 1 收尾同一条纪律）。
            items = [
                existing.model_dump(mode="json")
                for existing in detail.action_items
                if not (existing.kind == step_machine.ACTION_SEGMENT_VIDEO
                        and existing.status == "pending")]
            items.append(item.model_dump(mode="json"))
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": jobs,
                "segments": rows,
                "action_items": items,
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_AWAITING_HUMAN}, job_id=job_id),
            EventDraft("action_item.created",
                       {"item_id": item.item_id, "kind": item.kind,
                        "segment_index": number}, job_id=job_id),
        ])
        return None

    def _finish_segment_validation(self, session_id: str, job_id: str,
                                   result: dict[str, Any]) -> JobSummary | None:
        """段校验收尾：通过则落段状态并起下一段写词；被拒则重建待办（D6 §5.4）。

        - 通过：段转 ``passed``，**立刻持久化**；非末段把尾帧锚写进 state 并起下一段
          写词 job；末段起拼接 job（D6 §9：不要求用户点任何按钮，也没有交付确认闸门）。
        - 被拒：段转回 ``waiting_video``、**不落 passed**，重建同一段的待办并发
          ``action_item.created``。用户的素材不合格不是系统故障，**不记 failed**。
        """
        from ..segment_validation import END_REACHED

        number = int(result["number"])
        description = str(result.get("description") or "")
        passed = str(result.get("outcome")) == END_REACHED

        if passed:
            # 通过就一定有下一步：非末段接下一段写词，末段接拼接（D6 §5.3/§9）。
            # 两者由 ``_segment_step`` 按已落盘的段状态分辨，这里只负责把 job 放出去。
            next_job = JobSummary(
                job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT, status="running",
                started_at=_now())
            next_status = step_machine.STATUS_RUNNING

            def _apply(detail: Any) -> dict[str, Any]:
                rows = [row.model_dump() for row in detail.segments]
                for row in rows:
                    if int(row["index"]) == number:
                        row["status"] = SEGMENT_STATUS_PASSED
                        # 重交成功后不再展示上一轮的原因（D6 §5.4）。
                        row["rejection_reason"] = ""
                jobs = self._succeed_job(detail, job_id)
                jobs = _upsert_job(jobs, _job_dict(next_job))
                changes: dict[str, Any] = {
                    "status": next_status,
                    "jobs": jobs,
                    "segments": rows,
                }
                # 尾帧锚在**锁内**基于当前 state 合并（D6 §5.4）：校验是慢速调用，开始前
                # 读到的 state 到收尾时可能已被别的写者推进，整份写回会盖掉那些键。
                # 段号取下一段的 0-based index（＝本段 1-based 段号），与
                # ``segment_prompts._frame_anchor_note`` 的取法同源。
                if number < len(rows) and description:
                    anchor_state = detail.stage_state_snapshot()
                    anchors = [
                        entry for entry in
                        anchor_state.get("bridge_frame_descriptions") or []
                        if isinstance(entry, dict)]
                    anchors.append({"segment": number, "description": description})
                    anchor_state["bridge_frame_descriptions"] = anchors
                    changes["stage_state"] = anchor_state
                return changes

            events = [EventDraft("job.succeeded", {"next": next_status}, job_id=job_id)]
            events.append(EventDraft(
                "job.started", {"kind": JOB_KIND_SEGMENT}, job_id=next_job.job_id))
            self.store.mutate(session_id, _apply, events=events)
            return next_job

        # 具体原因随段状态与待办一起给到用户（D6 §5.4「被拒时看到具体原因」）。
        reason = _segment_rejection_reason(
            str(result.get("end_hook") or ""), description)
        item = ActionItemOut(
            item_id=uuid.uuid4().hex, kind=step_machine.ACTION_SEGMENT_VIDEO,
            status="pending", created_at=_now(), segment_index=number,
            reason=reason)

        def _reject(detail: Any) -> dict[str, Any]:
            jobs = self._succeed_job(detail, job_id)
            rows = [row.model_dump() for row in detail.segments]
            for row in rows:
                if int(row["index"]) == number:
                    # 被拒的视频不算数：清掉素材 id，回到「等用户换一段视频重交」。
                    row["status"] = SEGMENT_STATUS_WAITING_VIDEO
                    row["video_asset_id"] = ""
                    row["rejection_reason"] = reason
            items = [
                existing.model_dump(mode="json")
                for existing in detail.action_items
                if not (existing.kind == step_machine.ACTION_SEGMENT_VIDEO
                        and existing.status == "pending")]
            items.append(item.model_dump(mode="json"))
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": jobs,
                "segments": rows,
                "action_items": items,
            }

        self.store.mutate(session_id, _reject, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_AWAITING_HUMAN}, job_id=job_id),
            EventDraft("action_item.created",
                       {"item_id": item.item_id, "kind": item.kind,
                        "segment_index": number, "reason": reason},
                       job_id=job_id),
        ])
        return None

    def _finish_segment_assembly(self, session_id: str, job_id: str,
                                 film: Any) -> JobSummary | None:
        """拼接收尾：登记 ``final.mp4``，会话转 ``completed``（D6 §9）。

        「拼不出来」在 ``_execute_segment_assemble`` 里已经抛成任务失败，走到这里的
        只有成功一条：成片已落盘，先登记产物再落终态——事件说的必须是已发生的事实，
        反过来会让订阅者拿到一个还不存在的 ``artifact.created``。
        """
        self.store.register_artifact(
            session_id, ARTIFACT_FINAL_FILM, job_id=job_id)

        def _apply(detail: Any) -> dict[str, Any]:
            return {
                "status": step_machine.STATUS_COMPLETED,
                "jobs": self._succeed_job(detail, job_id),
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_COMPLETED}, job_id=job_id),
        ])
        return None

    def _finish_segment_done(self, session_id: str, job_id: str) -> JobSummary | None:
        """没有下一步可做（当前段在等人交视频）：温和收尾到 ``awaiting_human``。

        全部段都已通过时不走这里——那时该做的是拼接（``SEGMENT_STEP_ASSEMBLE``）。
        """
        def _apply(detail: Any) -> dict[str, Any]:
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": self._succeed_job(detail, job_id),
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_AWAITING_HUMAN}, job_id=job_id),
        ])
        return None

    def _finish_cancelled(self, session_id: str, job_id: str, node: str) -> None:
        def _apply(detail: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "cancelled", "finished_at": _now(),
                        "cancelled_at_node": node or None,
                        "cancel_requested": True})
            return {
                "status": step_machine.STATUS_CANCELLED,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.cancelled", {"at_node": node or ""}, job_id=job_id)])

    def _finish_failed(self, session_id: str, job_id: str, code: str,
                       detail: str) -> None:
        def _apply(current: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, current).model_copy(
                update={"status": "failed", "finished_at": _now(),
                        "error_code": code, "detail": detail})
            return {
                "status": step_machine.STATUS_FAILED,
                "jobs": _upsert_job(current.jobs, _job_dict(job)),
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.failed", {"code": code, "detail": detail},
                       job_id=job_id)])

    def _request_cancel(self, session_id: str, job_id: str) -> JobSummary:
        holder: dict[str, JobSummary] = {}

        def _apply(detail: Any) -> dict[str, Any]:
            job = _find_job(detail.jobs, job_id)
            if job is None:
                raise JobNotFound(f"任务不存在：{job_id}")
            if job.status != "running":
                raise JobNotRunning(
                    f"任务已结束（{job.status}），没有可取消的执行。")
            requested = job.model_copy(update={"cancel_requested": True})
            holder["job"] = requested
            # 落盘让 GET 立刻看得到标记；执行器在节点边界读到同一个标记后收尾。
            return {"jobs": _upsert_job(detail.jobs, _job_dict(requested))}

        self.store.mutate(session_id, _apply)
        # 标记落盘之后才唤醒执行器：反过来会让「执行器已停、GET 还看不到取消」。
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        return holder["job"]

    # ------------------------------------------------------------------
    # 在跑标记与启动对账
    # ------------------------------------------------------------------

    def _active_job_path(self, session_id: str) -> Path:
        return self.store.session_directory(session_id) / ACTIVE_JOB_FILENAME

    def _write_active_job(self, session_id: str, job_id: str, node: str) -> None:
        payload = {
            "pid": os.getpid(),
            "job_id": job_id,
            "started_at": self._started_at.get(job_id, _now()),
            "node": node,
        }
        atomic_write_text(
            self._active_job_path(session_id),
            json.dumps(payload, ensure_ascii=False, indent=2))

    def _remove_active_job(self, session_id: str, job_id: str) -> None:
        path = self._active_job_path(session_id)
        data = _read_json(path)
        if isinstance(data, dict) and data.get("job_id") not in (None, job_id):
            # 标记已属于另一个任务：不能替它删。
            return
        path.unlink(missing_ok=True)

    def reconcile(self) -> list[str]:
        """启动对账：把上一个进程留下的 ``running`` 会话改成 ``interrupted``。

        **不自动续跑**（spec §6）：用户再发一次 ``advance`` 才继续，起点由阶段 1 的
        ``_progress`` 决定。判据（spec §6 原文）：标记里的 pid **不是本进程且该进程还
        活着**才跳过；否则一律 interrupted。启动时刻本进程不可能已有 job 在跑，所以
        「pid 等于自己」的陈旧标记也必须收掉——否则它永远停在 running，再也推进不了。
        """
        marked: list[str] = []
        for directory in self.store.session_directories():
            try:
                session = load_session(directory)
                session_id = directory.resolve().relative_to(
                    self.store.root.resolve()).as_posix()
            except (ValueError, OSError) as exc:
                logger.warning("对账跳过损坏的会话：%s（%s）", directory, exc)
                continue
            if session is None or session.status != step_machine.STATUS_RUNNING:
                continue
            marker = _read_json(directory / ACTIVE_JOB_FILENAME)
            pid = marker.get("pid") if isinstance(marker, dict) else None
            if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
                # 另一个还活着的服务进程在持有这个会话：不动它，避免双写。
                continue
            try:
                def _mark(detail: Any) -> dict[str, Any]:
                    if detail.status != step_machine.STATUS_RUNNING:
                        return {}
                    jobs = []
                    for item in detail.jobs:
                        if item.status == "running":
                            item = item.model_copy(update={
                                "status": step_machine.STATUS_INTERRUPTED,
                                "finished_at": _now()})
                        jobs.append(_job_dict(item))
                    return {"status": step_machine.STATUS_INTERRUPTED, "jobs": jobs}

                # 事件按快照里「正在跑」的任务逐个记（对账发生在启动时，读到的
                # 快照就是唯一事实）；一个都没有时记一条不带 job_id 的，重放时按
                # 「所有 running」收口。_mark 判定无事可做时这些事件不会落盘。
                interrupted_events = [
                    EventDraft("job.interrupted", {},
                               job_id=str(item.get("job_id") or "") or None)
                    for item in session.jobs
                    if item.get("status") == "running"
                ] or [EventDraft("job.interrupted", {})]
                self.store.mutate(session_id, _mark, events=interrupted_events)
            except (ValueError, OSError) as exc:
                logger.warning("对账失败：%s（%s）", session_id, exc)
                continue
            (directory / ACTIVE_JOB_FILENAME).unlink(missing_ok=True)
            marked.append(session_id)
        return marked


__all__ = ["ACTIVE_JOB_FILENAME", "JOB_HISTORY_LIMIT", "JobCancelled", "JobRunner"]
