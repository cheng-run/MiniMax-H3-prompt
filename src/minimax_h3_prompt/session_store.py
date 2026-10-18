"""两阶段生产流的会话持久化：阶段 1 产物落盘，阶段 2 断点续接。

阶段 1 结束后用户要离开终端去 ComfyUI 生图（几十分钟到数小时），因此完整 state
必须可序列化落盘；重启后 load 回来即可直接跑阶段 2，不需要重跑任何角色 agent。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .brief_parser import Brief, RefItem

SESSION_FILENAME = "session-state.json"
_SCHEMA_VERSION = "wizard_session.v1"

# status: awaiting_frames = 阶段 1 完成、等待用户交回产出物——首尾帧路线是关键帧图，
#   多参路线是参考素材（素材交回，issue #43：缺项可停在这续接补交）。同一个「等交回」
#   语义、两种交回物，不另开状态：续接/扫描口径只认「未完成」一种分叉（issue #17 教训）；
#   completed = 视频提示词已产出
# stage1_running = 阶段 1 跑到一半中断（每个节点完成时增量落盘），重启后可以从断点继续
# segmented_running = 长视频分段陪跑进行中（阶段 2 已产出整条提示词，逐段人工生成中）。
#   这一态必须存在：阶段 2 结束时写 completed 会让「陪跑中断 → 重跑向导」另开新 GEN，
#   阶段 1/2 重做且 segments/progress.json 永远读不到（issue #17，GEN005 实测）。
STATUS_AWAITING_FRAMES = "awaiting_frames"
STATUS_COMPLETED = "completed"
STATUS_STAGE1_RUNNING = "stage1_running"
STATUS_SEGMENTED_RUNNING = "segmented_running"

# state 中允许持久化的键；brief 对象单独序列化。`_progress` 是阶段 1 断点的元数据（哪个节点跑完了）。
# `fl2va_frame_descriptions` 是用户提交帧图的读图结果——阶段 2 分段流程的唯一画面事实源，
# 续跑丢掉它，分段提示词就会退回照分镜表写（2026-09-22 首帧不锚定缺陷）。
# `user_revisions` 是累积的用户修订（唯一真源）——丢掉等于用户要说的话得再说一遍；
# `frame_round` 是它的轮次计数器（只增不减，撤条也不能回退，否则台账行号会重复）。
# `asset_proofreads` 是读图校对记录（issue #44）——丢掉等于三选裁决白做、换版后
# 旧裁决还冒充新版的裁决。
# `asset_loop` 是素材循环的队列进度（issue #50）——released 记「已出过词」的语义名
# （逐张出站的落盘节奏靠它续）、abandoned 记显式放弃的语义名；丢掉会让续接把
# 已放弃的资产重新排进待办、把出过的词再出一遍。
# **故意不在**这里的：`frame_revision_baseline`（修订基线，issue #25）与
# `setting_revision`（本轮那条设定级修订，issue #28）。两个都是人机修改循环**某一轮的
# 入参**而不是产物：前者每轮由当时那一版产物现渲染，持久化它等于把一份陈旧的基线喂给
# 续接后的自动质检循环（那是对当前产物重算的替换语义）；后者只在这一次截断重跑里有效，
# 留下来会让续接后任何一次重跑都重新回灌同一条设定要求。两者都跑完即摘。
_STATE_KEYS = (
    "production_plan", "director_brief", "creative_lock", "script",
    "character_design", "background_design", "prop_design", "art_design",
    "character_image_prompts", "prop_image_prompts", "scene_image_prompts",
    "identity_lock", "shot_table", "shot_review_lock", "visual_design",
    "fl2va_prompt_bundle", "fl2va_frame_descriptions", "subject_defs",
    "sound_design", "music", "final_prompt", "final_report", "_progress",
    "user_revisions", "frame_round", "asset_proofreads", "asset_loop",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _brief_to_dict(brief: Brief) -> dict[str, Any]:
    return {
        "mode": brief.mode,
        "variant": brief.variant,
        "duration": brief.duration,
        "style": brief.style,
        "language": brief.language,
        "plot": brief.plot,
        # 起步前置澄清的结论（issue #29）。brief 的字段是逐个显式列出的，新字段不加进
        # 这里就是静默丢弃——用户重启后要重新答一遍澄清。
        "clarifications": brief.clarifications,
        # 用户选定的画布（issue #42）：同上，漏登记就静默回落默认表。元组转列表
        # 是 JSON 的口径，读回时再转回元组。
        "canvases": {family: [width, height] for family, (width, height) in brief.canvases.items()},
        # 视频输出三元组（issue #45）：定案后（推导/声明，issue #49）回写进 brief，
        # 漏登记同样静默丢——
        # 重启续接后注入卡会按默认重出，与用户选定对不上账。
        "video_output": (
            {"ratio": brief.video_output.ratio,
             "megapixels": brief.video_output.megapixels,
             "multiple": brief.video_output.multiple}
            if brief.video_output else None
        ),
        "refs": [
            {"picture": r.picture, "name": r.name, "description": r.description, "path": r.path}
            for r in brief.refs
        ],
        "draft": brief.draft,
    }


def _canvases_from_dict(raw: Any) -> dict[str, tuple[int, int]]:
    """读回用户选定画布；错形**大声报错**（issue #42 复审）。

    静默过滤错形（旧实现的 `isinstance` 过滤）＝画布悄悄回落默认表——会话文件是我们
    自己写的，错形只可能是损坏或外人改写，吞掉它就再也对不上账；与 `parse_canvas_size`
    「画布是精确数据不是自由文本」同一口径。逐条口径（复审实测补严）：

    - 键缺失 / JSON null → 空表（旧会话没有这个键，回落默认表是既定兜底）；
    - 整字段是别的形状（[]、""、0、false、标量）→ 报错——falsy 错形不许被
      ``or {}`` 悄悄洗成空表，那正是「整表丢失、回落默认」的静默危害；
    - 条目值必须是**整数对**：``["700","1300"]`` 不许静默强转、``[700.9,…]``
      不许静默截断——数据被改而不报错比丢弃更糟。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"会话文件的画布字段错形：{raw!r}（应为 家族→[宽, 高] 的对象）")
    result: dict[str, tuple[int, int]] = {}
    for family, size in raw.items():
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise ValueError(f"会话文件的画布条目错形：{family} = {size!r}（应为 [宽, 高]）")
        if any(type(value) is not int for value in size):
            raise ValueError(f"会话文件的画布条目非整数：{family} = {size!r}")
        result[str(family)] = (size[0], size[1])
    return result


def _video_output_from_dict(raw: Any):
    """读回视频输出三元组；None ＝ 未定案（消费方按画布自动推导，issue #49）。

    错形**大声报错**（画布同款口径）：会话文件是我们自己写的，错形只可能是损坏或
    外人改写——吞掉它就「用户明明选了、续接后按默认重出」。值**不预强转**、原样
    交给 ``VideoOutputParams`` 校验门（``"1.0"`` 强转、``32.9`` 截断都是静默改数据，
    画布条目「不许静默强转/截断」同款纪律），函数级导入防环。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"会话文件的视频输出字段错形：{raw!r}"
                         "（应为 {ratio, megapixels, multiple} 对象）")
    from .injection_card import VideoOutputParams

    return VideoOutputParams(
        ratio=raw.get("ratio", ""),
        megapixels=raw.get("megapixels", 0),
        multiple=raw.get("multiple", 0),
    )


def _brief_from_dict(raw: dict[str, Any]) -> Brief:
    brief = Brief(
        mode=str(raw.get("mode", "base")),
        variant=str(raw.get("variant", "T2VA")),
        duration=float(raw.get("duration", 5.0)),
        style=str(raw.get("style", "")),
        language=str(raw.get("language", "Chinese")),
        plot=str(raw.get("plot", "")),
        clarifications=str(raw.get("clarifications", "")),
        canvases=_canvases_from_dict(raw.get("canvases")),
        video_output=_video_output_from_dict(raw.get("video_output")),
        draft=str(raw.get("draft", "")),
    )
    for item in raw.get("refs", []):
        brief.refs.append(RefItem(
            picture=int(item.get("picture", 1)),
            name=str(item.get("name", "")),
            description=str(item.get("description", "")),
            path=str(item.get("path", "")),
        ))
    return brief


@dataclass(frozen=True)
class SessionState:
    """一个 generation 的两阶段会话。"""

    directory: Path
    brief: Brief
    stage_state: dict[str, Any]
    status: str
    variant_downgraded: str = ""
    frame_images: tuple[dict[str, Any], ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def awaiting_frames(self) -> bool:
        return self.status == STATUS_AWAITING_FRAMES

    def save(self) -> Path:
        payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "status": self.status,
            "brief": _brief_to_dict(self.brief),
            "stage_state": {k: self.stage_state[k] for k in _STATE_KEYS if k in self.stage_state},
            "variant_downgraded": self.variant_downgraded,
            "frame_images": [dict(item) for item in self.frame_images],
            "created_at": self.created_at or _now(),
            "updated_at": _now(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / SESSION_FILENAME
        if not self.created_at and target.exists():
            old = json.loads(target.read_text(encoding="utf-8"))
            payload["created_at"] = str(old.get("created_at") or payload["created_at"])
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target


def save_session(directory: str | Path, brief: Brief, stage_state: dict[str, Any], *,
                 status: str = STATUS_AWAITING_FRAMES) -> Path:
    """保存或更新会话；directory 为 generation 目录。"""
    session = SessionState(Path(directory), brief, stage_state, status)
    return session.save()


def load_session(directory: str | Path) -> SessionState | None:
    """加载会话；文件不存在或 JSON 损坏返回 None（调用方决定回退到新建流程）。

    **字段内容损坏**（如画布条目错形）不走 None、抛 ValueError——那类损坏若也吞成
    None，就是「画布悄悄回落默认表」的静默危害（issue #42 复审）；精确数据坏了要响。
    """
    target = Path(directory) / SESSION_FILENAME
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if raw.get("schema_version") != _SCHEMA_VERSION:
        return None
    return SessionState(
        directory=Path(directory),
        brief=_brief_from_dict(raw.get("brief", {})),
        stage_state=dict(raw.get("stage_state", {})),
        status=str(raw.get("status", STATUS_AWAITING_FRAMES)),
        variant_downgraded=str(raw.get("variant_downgraded", "")),
        frame_images=tuple(dict(x) for x in raw.get("frame_images", [])),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def find_awaiting_sessions(root_dir: str | Path) -> list[SessionState]:
    """扫描会话根目录下所有可续接的会话（阶段 1 中断的 + 等帧图的 + 分段陪跑中的）。

    只读，不猜测内容归属。``segmented_running`` 必须在内：分段陪跑的中断（硬阻断、
    Ctrl-C、机器重启）代价原本是阶段 2 重做 + 已完成的所有段重来（issue #17）。
    """
    root = Path(root_dir)
    results: list[SessionState] = []
    if not root.is_dir():
        return results
    resumable = (STATUS_AWAITING_FRAMES, STATUS_STAGE1_RUNNING, STATUS_SEGMENTED_RUNNING)
    for session_file in sorted(root.glob("**/" + SESSION_FILENAME)):
        try:
            session = load_session(session_file.parent)
        except ValueError as exc:
            # 单文件字段损坏（如画布错形）不许炸掉整个恢复扫描——但它也不许静默消失：
            # 打印出来跳过，用户看得到哪个会话坏了（issue #42 复审：严格读回的连带面）。
            print(f"[警告] 会话文件损坏，已跳过：{session_file}（{exc}）")
            continue
        if session is not None and session.status in resumable:
            results.append(session)
    return results


__all__ = [
    "SessionState", "SESSION_FILENAME", "STATUS_AWAITING_FRAMES", "STATUS_COMPLETED",
    "STATUS_STAGE1_RUNNING", "STATUS_SEGMENTED_RUNNING",
    "save_session", "load_session", "find_awaiting_sessions",
]
