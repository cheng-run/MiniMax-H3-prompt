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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只用于段级注入卡写入口的签名，运行时按鸭子类型调用
    from ..ref_segments import RefSegmentJob

from .. import step_machine
from ..brief_parser import is_ref_mode
from ..errors import error_code
from ..kinds import KIND_PICTURE
from ..segment_validation import segment_submission_problems
from ..session_store import FRAME_SUBMISSION_KEY, atomic_write_text, load_session
from ..summary import PromptSummary, render_summary_zh
from ..workbench_store import (
    ARTIFACT_CONCAT_ORDER_CARD,
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
    AssetImageInvalid,
    EventDraft,
    FrameInvalid,
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
    WorkbenchError,
    WorkbenchSessionStore,
)

logger = logging.getLogger(__name__)

ACTIVE_JOB_FILENAME = "active-job.json"
# 会话文件里保留的 job 摘要条数（spec §5.2：最近 N 条，完整历史在事件流里）。
JOB_HISTORY_LIMIT = 20

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

# 段任务的进度节点名（带段号，前端据进度事件显示「正在写/校验第几段」）。
SEGMENT_WRITE_NODE = "segment_write"
SEGMENT_VALIDATE_NODE = "segment_validate"
SEGMENT_ASSEMBLE_NODE = "segment_assemble"
# 尾帧剥出的落点目录（不是产物：登记表只认 ``segments/shot-NN.md``）。
SEGMENT_BRIDGE_DIRNAME = "bridge_frames"

# 素材循环（D7 spec §5.3 / §6.2）：出词与校对是**两个独立 job**，都记这个 kind；
# 中间隔着持久化的 ``asset_image`` 待办（不是阻塞线程）。
JOB_KIND_ASSET_LOOP = "asset_loop"
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


def _resolve_fingerprint(item_id: str, decision: str | None,
                         revisions: list[dict[str, str]],
                         frames: dict[str, str] | None,
                         video_asset_id: str | None = None,
                         asset_id: str | None = None) -> str:
    """resolve 的请求摘要：待办、决定、修订内容、帧槽与素材标识都算进去。

    段视频素材（``video_asset_id``）必须进摘要：不进的话，同一个 Idempotency-Key 换一段
    视频重发会**重放第一次的响应**，用户以为重交了、系统其实没执行。素材循环的交图
    （``asset_id``）同理：换一张图重发不能重放上一张的结论。
    """
    canonical = json.dumps(
        {"item_id": item_id, "decision": decision, "revisions": revisions,
         "frames": frames or {}, "video_asset_id": video_asset_id or "",
         "asset_id": asset_id or ""},
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
    ) -> tuple[ActionItemOut, bool]:
        """把一条**已通过**的 ref 段打回 ``waiting_video``，重建本段待办（#258）。

        spec §5.5 要求 ref 的已通过段能「换一段」，而 §12 又写「不新增端点」——用户
        2026-11-13 裁定：**新增段级端点**。base 不开这条边（中间段换掉会断掉第 N+1 段
        的帧锚，ADR 0023），所以 base 会话请求换段是 ``segment_resubmit_unsupported``。

        换段**只换视频**：段词与段内参考素材子集原样不动（子集是系统算的，没有手改
        入口），重交后照旧重过双闸门。
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
                    return ActionItemOut.model_validate(cached), True

            item = ActionItemOut(
                item_id=uuid.uuid4().hex,
                kind=step_machine.ACTION_SEGMENT_VIDEO,
                status="pending", created_at=_now(), segment_index=index)

            def _apply(detail: Any) -> dict[str, Any]:
                # 路线先判：base 会话压根不该有这个动作，回「段不存在」会把人带偏。
                if not is_ref_mode(detail.brief.mode):
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

            await asyncio.to_thread(
                self.store.mutate, session_id, _apply, events=[
                    EventDraft(
                        "action_item.created",
                        {"item_id": item.item_id, "kind": item.kind,
                         "segment_index": index},
                        job_id=None),
                ])
            if idempotency_key and registry is not None:
                await asyncio.to_thread(
                    registry.record, idempotency_key,
                    fingerprint=payload, response=item.model_dump(mode="json"))
            return item, False

    async def resolve(
        self, session_id: str, item_id: str, *, decision: str | None,
        revisions: list[dict[str, str]] | None = None,
        frames: dict[str, str] | None = None,
        video_asset_id: str | None = None,
        asset_id: str | None = None,
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
                asset_id)
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
            frame_gate = branch == _BRANCH_FRAME_GATE
            # 帧链通过审查不产生任务；其余去向照旧。``job`` 为 None 时下面所有
            # 与任务相关的落盘都跳过——契约里那一段的 202 是「已受理、无执行」。
            job = None if frame_gate else JobSummary(
                job_id=uuid.uuid4().hex,
                # 素材循环的两个 job 都记 ``asset_loop``（D7 §6.2）：前端/排障按 kind
                # 就能把「出词」「校对」与外层的推进任务分开。
                kind=(JOB_KIND_ASSET_LOOP if branch == _BRANCH_ASSET_LOOP
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
                            and step_machine.needs_frame_submission(
                                current.brief.variant)):
                        new_item = ActionItemOut(
                            item_id=created_item_id,
                            kind=step_machine.ACTION_FRAME_SUBMISSION,
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
                    # 段号与当前段一致 + 技术档合格（复用 check_segment_video）。
                    problems = segment_submission_problems(
                        current_index=current_seg.index, number=int(number),
                        video_path=resource.path,
                        expected_seconds=float(current_seg.end_s - current_seg.start_s))
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
                else:
                    raise FrameInvalid(f"不认识的人工待办类型：{target.kind}")
                holder["decision"] = recorded_decision

                items = [
                    (item.model_copy(update={
                        "status": "resolved", "decision": recorded_decision,
                        "resolved_at": now,
                        "revisions": recorded_revisions}).model_dump(mode="json")
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
            events = [
                EventDraft("action_item.resolved", resolved_payload,
                           job_id=job.job_id if job else None),
            ]
            if job is not None:
                events.insert(0, EventDraft(
                    "job.started", {"kind": job.kind}, job_id=job.job_id))
            if frame_gate:
                events.append(EventDraft(
                    "action_item.created",
                    {"item_id": created_item_id,
                     "kind": step_machine.ACTION_FRAME_SUBMISSION},
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
            if branch in (_BRANCH_SEGMENT_VIDEO, _BRANCH_ASSET_LOOP):
                # 段校验与素材循环都走通用分派（``_run`` 按持久化事实决定跑哪一步）：
                # 失败重试也走同一条出口，两处各写一份子步骤判定会长出两套口径。
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
        if (kind == step_machine.ACTION_STAGE1_REVIEW
                and decision == step_machine.DECISION_APPROVE):
            # ref 通过审查先走素材循环（D7 §5.3）：它不是帧链，也不直接进阶段 2。
            if session.brief.is_ref:
                return _BRANCH_ASSET_LOOP
            if step_machine.needs_frame_submission(session.brief.variant):
                return _BRANCH_FRAME_GATE
        return _BRANCH_REVIEW


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
            # 素材循环（D7 §5.3 / §14）：ref 在阶段 1 与阶段 2 之间多一档。判据只读
            # 台账——重启后照样能重建「在等第几件」，不依赖 job 历史。
            if (session.brief.is_ref
                    and _asset_step_of(session.asset_ledger)[0] != ASSET_STEP_DONE):
                return step_machine.STEP_ASSET_LOOP
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
    def _needs_segment_loop(brief: Any) -> bool:
        """阶段 2 之后接不接段循环：门槛是**路线属性**，不是会话属性（D7 §5.4）。

        base 只在 >10 秒时进分段（D6 §3，不动）；ref 一律进——≤ 单次生成上限的会话
        走「1 段」直出，它仍是同一个段循环（同一套待办、双闸门与拼接），不是长片。
        """
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
        """执行器交给管线的 Brief：ref 会话的 ``refs`` 从**素材台账**现造（D7 §5.3 / §10）。

        工作台的参考素材不在 brief 文件里——创建口不收 refs，台账才是唯一真源
        （D7 §4.1）。阶段 1 的分镜表 Subject 标签与资产图提示词命名都要按台账语义名
        对齐，所以每个「把 brief 交给管线或写词」的执行点都必须走这里；漏一处，
        真实 ref 会话就会在出词那一步对不上号。

        只收 Picture：``RefItem.picture`` 是 ``<Picture N>`` 槽位，视频/音频没有这个
        编号（它们的段内子集是 #258 的事）。``path`` 留空——本地路径既不进 DTO 也不进
        管线输入。用 ``replace`` 而不是就地改：``_finish_stage2`` 会把 brief 写回会话
        文件，就地加就是给台账造一份会变旧的副本。base 会话原样返回。
        """
        if not session.brief.is_ref:
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

    def _execute_stage2(self, session_id: str, job_id: str,
                        cancel_event: threading.Event) -> dict[str, Any]:
        """线程里跑的阶段 2（approve / 交帧之后）。

        有帧则先读图再组装（D4 §9）：读图是慢速视觉调用，放在任务里，请求在 202
        就返回了。没有帧时的**自动降级**与向导同源（``resolve_effective_variant``），
        并把降级事实记进会话（``variant_downgraded``）——降级不静默。
        ``video-prompt.md`` 与向导同目录同名，写完才算成功。
        """
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            # 每件同时最多一个未决项：同类旧项先收掉再挂新的（与阶段 1 / 段循环同一条
            # 纪律）。严格串行也由这里兜住——新卡片出现时上一张必然已决议。
            items = [
                existing.model_dump(mode="json") for existing in detail.action_items
                if not (existing.kind == step_machine.ACTION_ASSET_IMAGE
                        and existing.status == "pending")]
            items.append(item.model_dump(mode="json"))
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            jobs = _upsert_job(detail.jobs, _job_dict(job))
            changes: dict[str, Any] = {
                "asset_ledger": _settle_asset(
                    detail.asset_ledger_snapshot(), number, passed)}
            if next_job is not None:
                changes["status"] = step_machine.STATUS_RUNNING
                changes["jobs"] = _upsert_job(jobs, _job_dict(next_job))
            else:
                # 同一件号重开：同类未决旧项先收掉再挂新的（收旧挂新）。
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
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
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
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

    def _finish_stage2(self, session_id: str, job_id: str,
                       result: dict[str, Any]) -> JobSummary | None:
        """阶段 2 收尾：短视频 completed；长片改挂分段规划任务（D6 spec §5.3）。

        短视频这条路一个字段都不变（D4 的交付语义：阶段 2 成功即 completed，没有交付
        确认）。长片不能落 completed——规划紧接着跑，会话仍是 running；新任务的摘要
        与旧任务的 ``succeeded`` **在同一次落盘**里写出，读者不会看到「两个任务同时
        running」或「已成功却没人接」的中间态。返回新任务给调用方起执行线程。
        """
        next_job: JobSummary | None = None
        if self._needs_segment_loop(result["brief"]):
            next_job = JobSummary(
                job_id=uuid.uuid4().hex, kind=JOB_KIND_SEGMENT, status="running",
                started_at=_now())
        next_status = (step_machine.STATUS_RUNNING if next_job is not None
                       else step_machine.STATUS_COMPLETED)

        def _apply(detail: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            jobs = _upsert_job(detail.jobs, _job_dict(job))
            if next_job is not None:
                jobs = _upsert_job(jobs, _job_dict(next_job))
            return {
                "status": next_status,
                "stage_state": result["state"],
                "jobs": jobs,
                "brief": result["brief"],
                "variant_downgraded": result["variant_downgraded"],
            }

        events = [
            EventDraft("job.succeeded", {"next": next_status}, job_id=job_id),
        ]
        if next_job is not None:
            events.append(EventDraft(
                "job.started", {"kind": JOB_KIND_SEGMENT}, job_id=next_job.job_id))
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            jobs = _upsert_job(detail.jobs, _job_dict(job))
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
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
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
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
                job = self._job_or_running(job_id, detail).model_copy(
                    update={"status": "succeeded", "finished_at": _now()})
                rows = [row.model_dump() for row in detail.segments]
                for row in rows:
                    if int(row["index"]) == number:
                        row["status"] = SEGMENT_STATUS_PASSED
                        # 重交成功后不再展示上一轮的原因（D6 §5.4）。
                        row["rejection_reason"] = ""
                jobs = _upsert_job(detail.jobs, _job_dict(job))
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
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
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            return {
                "status": step_machine.STATUS_COMPLETED,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
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
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            return {
                "status": step_machine.STATUS_AWAITING_HUMAN,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
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
                        "error_code": code})
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
