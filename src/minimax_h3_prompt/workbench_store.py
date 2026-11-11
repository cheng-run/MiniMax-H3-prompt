"""工作台会话存储：建会话、列表、详情；文件是唯一真源（D2 spec §5）。

为什么不做成数据库：一个会话是「一个目录 + 若干产物」，用户会直接看、直接拷、
直接删；文件布局本身就是交付物的一部分。事件流与快照的分工见 ADR 0032。

这一层只认 ``sessions_root`` 之下的相对路径，不接受任意本地路径——``session_id``
是登记根之下的标识（spec §5.1），越界一律按 404 处理，不泄漏根外文件是否存在。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal, Sequence, cast

from pydantic import BaseModel, Field

from . import step_machine
from .brief_parser import Brief
from .session_store import (
    SESSION_FILENAME,
    WORKBENCH_SCHEMA_VERSION,
    SessionState,
    atomic_write_text,
    load_session,
    save_session,
)
logger = logging.getLogger(__name__)

SessionStatus = Literal[
    "created", "running", "awaiting_human", "completed", "failed", "cancelled",
    "interrupted",
]
SESSION_STATUSES: tuple[str, ...] = (
    "created", "running", "awaiting_human", "completed", "failed", "cancelled",
    "interrupted",
)

# 旧向导状态 → 工作台状态。历史会话全部是 completed；未完成的旧会话（用户还在
# 向导里续接的那些）对 API 也是 legacy，映射只是给列表/详情一个可读的状态，
# **不承诺它们能被 advance**——legacy 标记才是那个判据。
_LEGACY_STATUS = {
    "completed": "completed",
    "awaiting_frames": "awaiting_human",
    "stage1_running": "interrupted",
    "segmented_running": "interrupted",
    "ref_segments_running": "interrupted",
    "director_segments_running": "interrupted",
}

# 产物登记范围（D2 spec §10、D3 #222 §7）：两张表各管一种响应形态——文本产物
# 走纯文本（1 MiB 上限），媒体产物走字节 + 单段 Range。分成两张表是为了让「某个
# 名字按哪种形态读」由登记范围派生，而不是在读取端点再抄一份白名单。
# 产物名字是契约的一部分：登记表、执行器写盘与用例都从这些常量取，不各写一份字面量
# （「守卫形态要从常量派生」——字面重抄在改名时会静默指到旧名字）。
ARTIFACT_VIDEO_PROMPT = "video-prompt.md"
# 阶段 1 的中间产物（D4 spec §7）：剧本与分镜表每次阶段 1 完成都落；首/尾帧词只有
# 帧链会话（bundle 里真有那一帧）才落，两个文件同 kind ``frame_prompt``。
ARTIFACT_SCRIPT = "script.md"
ARTIFACT_SHOT_TABLE = "shot-table.md"
ARTIFACT_FIRST_FRAME_PROMPT = "first-frame-prompt.md"
ARTIFACT_LAST_FRAME_PROMPT = "last-frame-prompt.md"

_ARTIFACT_KINDS = {
    ARTIFACT_VIDEO_PROMPT: "video_prompt",
    ARTIFACT_SCRIPT: "script",
    ARTIFACT_SHOT_TABLE: "shot_table",
    ARTIFACT_FIRST_FRAME_PROMPT: "frame_prompt",
    ARTIFACT_LAST_FRAME_PROMPT: "frame_prompt",
    "summary-zh.md": "summary_markdown",
    "summary-zh.json": "summary_json",
}
# 媒体产物：名字 → Content-Type。目前只有成片；列表里的 kind 仍是 ``video``。
ARTIFACT_MEDIA_TYPES = {
    "final.mp4": "video/mp4",
}
# 「哪些名字算产物」的唯一来源：清单扫描、登记守卫、内容读取都从它派生。
_KNOWN_ARTIFACTS = {
    **_ARTIFACT_KINDS,
    **{name: "video" for name in ARTIFACT_MEDIA_TYPES},
}

# 文本内容读取的上限（spec §10）：1 MiB，超出回 413 artifact_too_large。
ARTIFACT_MAX_BYTES = 1024 * 1024

# ---------------------------------------------------------------------------
# 上传素材（D3-2，#222 §5）
# ---------------------------------------------------------------------------
ASSETS_DIRNAME = "assets"
# 临时文件的固定前缀：崩溃残留靠它认出来（启动清理只删这一种）。
ASSET_TMP_PREFIX = ".tmp-"
# 上传的读块大小。它同时是「边写边判」的粒度：一次最多多读一块就能发现超限，
# 不会出现「先把 512 MiB 读完再判」。
ASSET_CHUNK_BYTES = 64 * 1024
# 嗅探窗口。spec §5.2 写的是「前 16 字节」——PNG / JPEG / WebP / WAV / MP3 / MP4 的
# 魔数在这个窗口里就够。EBML 不够：WebM 与 Matroska 共用同一个 4 字节头，判据是后面的
# DocType（``webm`` / ``matroska``，通常落在第 24–40 字节）。所以窗口取 64，
# 前 16 字节就能定的格式行为不变。
ASSET_SNIFF_BYTES = 64
ASSET_NAME_MAX_CHARS = 120

# 支持的类型与各自上限（#222 §5.2）。按类型分开是因为视频本来就比图片大两个数量级：
# 一个统一上限要么拦不住图，要么把短视频也卡死。
ASSET_KIND_LIMITS: dict[str, int] = {
    "image": 20 * 1024 * 1024,
    "video": 512 * 1024 * 1024,
    "audio": 64 * 1024 * 1024,
}

# 落盘扩展名 → (kind, 规范化格式)。嗅探与目录扫描共用这一张表：两处各写一份
# 「哪些扩展名算图片/视频/音频」时，改一处就会让列表与上传各说各话。
# ``jpg``/``jpeg`` 是同一个格式的两个写法，落盘一律取 ``jpg``。
_ASSET_FORMATS: dict[str, tuple[str, str]] = {
    "png": ("image", "png"),
    "jpg": ("image", "jpeg"),
    "jpeg": ("image", "jpeg"),
    "webp": ("image", "webp"),
    "mp4": ("video", "mp4"),
    "webm": ("video", "webm"),
    "wav": ("audio", "wav"),
    "mp3": ("audio", "mp3"),
}

# 规范化格式 → Content-Type。媒体读取端点与它自己的契约共用这一张表：端点按磁盘上
# 的扩展名给 Content-Type，契约里列出的可选类型也从这里派生，两处不会各说各话。
_FORMAT_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}
# 扩展名 → Content-Type：从 ``_ASSET_FORMATS`` **现算**，不给新扩展名留一条静默漏判
# 的路（加了扩展名却忘了媒体类型，这里当场 KeyError）。
ASSET_MEDIA_TYPES: dict[str, str] = {
    ext: _FORMAT_MEDIA_TYPES[fmt] for ext, (_kind, fmt) in _ASSET_FORMATS.items()
}

# ISO 基础媒体文件（MP4）的 major brand 白名单（``ftyp`` 盒第 8–12 字节）。
# 只认 ``ftyp`` 盒子不够：QuickTime（``qt  ``）、HEIC（``heic``/``mif1``）与 MP4 用
# 同一个盒子结构，前者不在 §5.2 白名单里、后者根本是图片——只判盒子会让它们以
# ``kind=video`` 落成 ``.mp4``（复审实测）。名单取常见封装器的输出。
_MP4_BRANDS = frozenset({
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso8", b"iso9",
    b"mp41", b"mp42", b"M4V ", b"M4A ", b"avc1", b"dash", b"MSNV", b"f4v ",
})

# 素材标识里的内容哈希前缀长度（#222 §5.4）。两条正则与 ``store_asset`` 截前缀都从
# 它派生——字面重抄三份的话，改宽度会静默漏改一处。
ASSET_HASH_CHARS = 16

# 素材文件名形如 ``<sha256 前 16 位>.<已知扩展名>``。``.tmp-*`` 不匹配这个形状；
# 边车 ``<hash>.json`` 形状上匹配，但扩展名不在 ``_ASSET_FORMATS`` 里，同样落选——
# 「残片/边车不出现在列表里」由这两条判据共同保证，不需要额外的排除清单。
_ASSET_NAME_RE = re.compile(
    rf"^(?P<hash>[0-9a-f]{{{ASSET_HASH_CHARS}}})\.(?P<ext>[a-z0-9]+)$")
# 素材标识的第二段：只有内容哈希前缀、没有扩展名（#222 §5.4）。扩展名是落盘形态，
# 不是标识的一部分——客户端拿列表里的 ``asset_id`` 就能拼出媒体 URL。
_ASSET_ID_RE = re.compile(rf"^[0-9a-f]{{{ASSET_HASH_CHARS}}}$")

_GEN_RE = re.compile(r"^GEN(\d+)$")
_IDEMPOTENCY_CAPACITY = 50


class WorkbenchError(Exception):
    """工作台领域错误；HTTP 层据此构造 problem+json。"""

    status_code = 500
    code = "workbench_error"


class SessionNotFound(WorkbenchError):
    status_code = 404
    code = "session_not_found"


class SessionLegacy(WorkbenchError):
    status_code = 409
    code = "session_legacy"


class SessionBusy(WorkbenchError):
    status_code = 409
    code = "session_busy"


class SessionCompleted(WorkbenchError):
    status_code = 409
    code = "session_completed"


class ActionItemPending(WorkbenchError):
    status_code = 409
    code = "action_item_pending"


class ActionItemNotFound(WorkbenchError):
    """待办不存在，或不属于 URL 里那个会话（两种情况不回不同码，避免泄漏他会话的 id）。"""

    status_code = 404
    code = "action_item_not_found"


class ActionItemResolved(WorkbenchError):
    status_code = 409
    code = "action_item_resolved"


class IdempotencyConflict(WorkbenchError):
    status_code = 409
    code = "idempotency_conflict"


class JobNotFound(WorkbenchError):
    status_code = 404
    code = "job_not_found"


class JobNotRunning(WorkbenchError):
    status_code = 409
    code = "job_not_running"


class ArtifactNotFound(WorkbenchError):
    """产物不存在、标识越界，或名字不在登记范围内。

    这几种情况不回不同码：越界与不存在若能区分，就等于泄漏登记根之外是否真有文件。
    """

    status_code = 404
    code = "artifact_not_found"


class ArtifactTooLarge(WorkbenchError):
    status_code = 413
    code = "artifact_too_large"


class UploadInvalid(WorkbenchError):
    """multipart 结构不对（不是表单、没有 ``file`` 字段、边界坏掉）。

    与 FastAPI 的 422 分开：422 说的是「字段值不合法」，这里说的是「请求根本不是
    一份能读的上传」——前端对这两种的处理不同（补字段 vs 改发送方式）。
    """

    status_code = 400
    code = "upload_invalid"


class UnsupportedMediaType(WorkbenchError):
    """魔数不在白名单（含「声明的扩展名与字节不符」）。

    判定只认字节（#222 §5.2）：用户文件名只作显示名。声明的扩展名若是**已知格式**却
    与嗅探结果不符，同样按 415 拒绝——文件名与内容互相矛盾时不猜哪一个是真的。
    """

    status_code = 415
    code = "unsupported_media_type"


class AssetTooLarge(WorkbenchError):
    status_code = 413
    code = "asset_too_large"


class AssetNotFound(WorkbenchError):
    """素材不存在、标识越界，或不是素材命名形态（#222 §8 / §10）。

    三种情况同码：能区分「越界」与「不存在」就等于泄漏登记根之外是否真有文件。
    """

    status_code = 404
    code = "asset_not_found"


class StorageUnavailable(WorkbenchError):
    """素材落盘失败（磁盘满、只读目录）。请求本身没错，是这台机器的存储不行。"""

    status_code = 507
    code = "storage_unavailable"


# ---------------------------------------------------------------------------
# 事件流（D2 spec §5.3 / §8，ADR 0032）
# ---------------------------------------------------------------------------
EVENTS_FILENAME = "events.jsonl"

# 十种闭合类型（spec §8）。**闭合**指写入侧：不在表里的类型一律拒绝，加字段可以、
# 改语义不行。新增类型要连 spec 与前端生成客户端一起改，不能在某条调用路径上偷偷加。
EVENT_TYPES: tuple[str, ...] = (
    "session.created",
    "job.started",
    "job.progress",
    "job.succeeded",
    "job.failed",
    "job.cancelled",
    "job.interrupted",
    "action_item.created",
    "action_item.resolved",
    "artifact.created",
)

# 进度事件的取值从步骤机常量派生：阶段与状态各有一份声明，这里重抄一遍就会在
# 步骤机改名时静默放过错值（本仓「守卫形态要从常量派生」）。
_PROGRESS_STAGES = frozenset({step_machine.STEP_STAGE1, step_machine.STEP_STAGE2})
_PROGRESS_STATUSES = frozenset({"started", "done"})
_SUCCEEDED_NEXT = frozenset(
    {step_machine.STATUS_AWAITING_HUMAN, step_machine.STATUS_COMPLETED})
_DECISIONS = frozenset({step_machine.DECISION_APPROVE, step_machine.DECISION_REVISE})


class UnknownEventType(ValueError):
    """写入了一个不在闭合集合里的事件类型（编程错误，不是用户输入错误）。"""


class InvalidEventPayload(ValueError):
    """事件 payload 缺少该类型的必需字段，或取值不在声明的范围内。"""


def _require_str(payload: dict[str, Any], key: str) -> None:
    if not isinstance(payload.get(key), str):
        raise InvalidEventPayload(f"事件 payload 缺字符串字段 {key!r}：{payload!r}")


def _require_choice(payload: dict[str, Any], key: str, allowed: frozenset[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise InvalidEventPayload(
            f"事件 payload 的 {key!r} 必须是 {sorted(allowed)} 之一：{value!r}")


def _validate_progress(payload: dict[str, Any]) -> None:
    _require_choice(payload, "stage", _PROGRESS_STAGES)
    _require_str(payload, "node")
    _require_choice(payload, "status", _PROGRESS_STATUSES)
    elapsed = payload.get("elapsed_ms")
    if elapsed is not None and (type(elapsed) is not int or elapsed < 0):
        raise InvalidEventPayload(f"事件的 elapsed_ms 必须是非负整数：{elapsed!r}")


def _no_required_fields(_payload: dict[str, Any]) -> None:
    return None


def _validate_failed(payload: dict[str, Any]) -> None:
    _require_str(payload, "code")
    _require_str(payload, "detail")


def _validate_action_item_created(payload: dict[str, Any]) -> None:
    _require_str(payload, "item_id")
    _require_str(payload, "kind")


def _validate_action_item_resolved(payload: dict[str, Any]) -> None:
    _require_str(payload, "item_id")
    _require_choice(payload, "decision", _DECISIONS)
    # 修订载荷是可选的加字段（D4 spec §7）：写了就必须是 [{layer, text}, …]，
    # 否则重放时 ``ActionItemOut`` 会拿半截字典去建模型并在**读详情**时报错。
    revisions = payload.get("revisions")
    if revisions is None:
        return
    if not isinstance(revisions, list):
        raise InvalidEventPayload(f"事件的 revisions 必须是列表：{revisions!r}")
    for entry in revisions:
        if not isinstance(entry, dict):
            raise InvalidEventPayload(f"修订条目必须是字典：{entry!r}")
        _require_str(entry, "layer")
        _require_str(entry, "text")


def _validate_artifact_created(payload: dict[str, Any]) -> None:
    _require_str(payload, "artifact_id")
    _require_str(payload, "kind")
    _require_str(payload, "name")


_PAYLOAD_RULES: dict[str, Callable[[dict[str, Any]], None]] = {
    "session.created": lambda payload: _require_str(payload, "status"),
    "job.started": lambda payload: _require_str(payload, "kind"),
    "job.progress": _validate_progress,
    "job.succeeded": lambda payload: _require_choice(
        payload, "next", _SUCCEEDED_NEXT),
    "job.failed": _validate_failed,
    "job.cancelled": lambda payload: _require_str(payload, "at_node"),
    "job.interrupted": _no_required_fields,
    "action_item.created": _validate_action_item_created,
    "action_item.resolved": _validate_action_item_resolved,
    "artifact.created": _validate_artifact_created,
}


@dataclass(frozen=True)
class EventDraft:
    """待写入的一条事件；``seq`` 由 :class:`EventLog` 在会话锁内分配。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None


@dataclass(frozen=True)
class Event:
    """已落盘的一条事件（包封见 spec §8）。"""

    seq: int
    type: str
    at: str
    session_id: str
    payload: dict[str, Any]
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "seq": self.seq,
            "type": self.type,
            "at": self.at,
            "session_id": self.session_id,
        }
        if self.job_id is not None:
            body["job_id"] = self.job_id
        body["payload"] = self.payload
        return body

    def sse_frame(self) -> str:
        """SSE 帧：``id`` / ``event`` / ``data`` 三行加一个空行（spec §8）。"""
        return (f"id: {self.seq}\nevent: {self.type}\n"
                f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n")


class EventLog:
    """``<会话目录>/events.jsonl`` 的只追加读写面。

    **序号分配必须在会话锁内**——``last_seq`` 与随后的写入之间若别人插进来，
    两条事件会拿到同一个号（D2-AC11）。本类自己不加锁：调用方是
    ``WorkbenchSessionStore``，它持有每会话一把 ``threading.Lock``。
    """

    def __init__(self, directory: str | Path, session_id: str = "") -> None:
        self.directory = Path(directory)
        self.session_id = session_id

    @property
    def path(self) -> Path:
        return self.directory / EVENTS_FILENAME

    def append(self, drafts: Sequence[EventDraft]) -> list[Event]:
        """按顺序追加一组事件并返回带 seq 的落盘结果；空列表不触碰文件。"""
        if not drafts:
            return []
        for draft in drafts:
            if draft.type not in EVENT_TYPES:
                raise UnknownEventType(
                    f"不是闭合集合里的事件类型：{draft.type!r}；"
                    f"可用类型：{', '.join(EVENT_TYPES)}")
            _PAYLOAD_RULES[draft.type](draft.payload)
        seq = self.last_seq()
        # 崩溃可能把最后一行写了一半：它从未落进快照，续写前先截掉，否则新事件会
        # 粘在残行后面，读回时整行作废（崩在这条事件上的序号被复用是正确的）。
        self._drop_torn_tail()
        at = datetime.now(timezone.utc).isoformat()
        events = [
            Event(seq=seq + offset, type=draft.type, at=at,
                  session_id=self.session_id, payload=dict(draft.payload),
                  job_id=draft.job_id)
            for offset, draft in enumerate(drafts, start=1)
        ]
        text = "".join(
            json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        return events

    def read(self, *, after_seq: int = 0) -> list[Event]:
        return [event for event in self._read_all() if event.seq > after_seq]

    def last_seq(self) -> int:
        events = self._read_all()
        return events[-1].seq if events else 0

    def _read_all(self) -> list[Event]:
        if not self.path.is_file():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("事件文件读不出来，按空流处理：%s（%s）", self.path, exc)
            return []
        events: list[Event] = []
        previous = 0
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                seq = item["seq"]
                if type(seq) is not int:
                    raise TypeError(f"seq 不是整数：{seq!r}")
                event = Event(
                    seq=seq,
                    type=str(item["type"]),
                    at=str(item["at"]),
                    session_id=str(item["session_id"]),
                    payload=dict(item.get("payload") or {}),
                    job_id=item.get("job_id"),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                # 半截行只可能来自「写事件」那一刻被强杀；它没有落进任何快照，
                # 跳过即可。真正的损坏（序号回退）在下面大声报错。
                logger.warning("事件文件第 %d 行无法解析，已跳过：%s（%s）",
                               number, self.path, exc)
                continue
            if event.seq <= previous:
                raise ValueError(
                    f"事件序号不单调：{self.path} 第 {number} 行 seq={event.seq}，"
                    f"上一行是 {previous}")
            previous = event.seq
            events.append(event)
        return events

    def _drop_torn_tail(self) -> None:
        if not self.path.is_file():
            return
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        with self.path.open("r+b") as handle:
            handle.truncate(data.rfind(b"\n") + 1)


def _replay_event_job(jobs: list[dict[str, Any]], event: Event,
                      updates: dict[str, Any]) -> None:
    """把一条 job 事件的结论并回快照的 job 摘要（按 ``job_id`` 覆盖）。"""
    if event.job_id is None:
        return
    for item in jobs:
        if item.get("job_id") == event.job_id:
            item.update(updates)
            return
    jobs.append({
        "job_id": event.job_id, "kind": "advance", "status": "",
        "started_at": "", "finished_at": "", "error_code": None,
        "cancelled_at_node": None, "cancel_requested": False, **updates,
    })


def _replay_interrupted(jobs: list[dict[str, Any]], event: Event) -> None:
    """``job.interrupted`` 的 payload 是空的：命中的任务由事件上的 ``job_id`` 指名，
    没有 ``job_id``（启动对账不认识具体任务）时按「所有 running」收。"""
    for item in jobs:
        if event.job_id is not None and item.get("job_id") != event.job_id:
            continue
        if item.get("status") == "running":
            item.update({"status": step_machine.STATUS_INTERRUPTED,
                         "finished_at": event.at})


def _replay_snapshot(
    session: SessionState, events: Sequence[Event],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """由事件流推出快照里被它们覆盖的字段（spec §5.3 的「重放补齐」）。

    只重放**快照真有对应字段**的事件：``job.progress`` 只代表「还在跑」，不写任何
    摘要；``artifact.created`` 是空操作——产物清单由 ``_artifacts`` 现扫目录得到，
    多记一份就是同一件事两处口径。

    幂等靠两点：待办按 ``item_id`` 去重、其余字段一律**绝对赋值**而非累加。同一批
    事件重放两次与一次的结果相同，所以「重复加载」不会长出第二条待办。
    """
    status = session.status
    jobs = [dict(item) for item in session.jobs]
    action_items = [dict(item) for item in session.action_items]
    for event in events:
        payload = event.payload
        if event.type == "session.created":
            status = str(payload["status"])
        elif event.type == "job.started":
            status = step_machine.STATUS_RUNNING
            _replay_event_job(jobs, event, {"status": step_machine.STATUS_RUNNING,
                                            "started_at": event.at})
        elif event.type == "job.progress":
            status = step_machine.STATUS_RUNNING
        elif event.type == "job.succeeded":
            status = str(payload["next"])
            _replay_event_job(jobs, event, {"status": "succeeded",
                                            "finished_at": event.at})
        elif event.type == "job.failed":
            status = step_machine.STATUS_FAILED
            _replay_event_job(jobs, event, {"status": "failed",
                                            "finished_at": event.at,
                                            "error_code": payload["code"]})
        elif event.type == "job.cancelled":
            status = step_machine.STATUS_CANCELLED
            _replay_event_job(jobs, event, {
                "status": "cancelled", "finished_at": event.at,
                "cancelled_at_node": payload["at_node"] or None,
                "cancel_requested": True})
        elif event.type == "job.interrupted":
            status = step_machine.STATUS_INTERRUPTED
            _replay_interrupted(jobs, event)
        elif event.type == "action_item.created":
            if not any(item.get("item_id") == payload["item_id"]
                       for item in action_items):
                action_items.append({
                    "item_id": payload["item_id"], "kind": payload["kind"],
                    "status": "pending", "created_at": event.at,
                    "resolved_at": "", "decision": None,
                })
        elif event.type == "action_item.resolved":
            revisions = payload.get("revisions")
            for item in action_items:
                if item.get("item_id") == payload["item_id"]:
                    item.update({"status": "resolved",
                                 "decision": payload["decision"],
                                 "resolved_at": event.at,
                                 # 修订历史只从这条事件补（D4 spec §7）：快照落后时
                                 # 也要能回显用户当时说了什么。
                                 "revisions": (
                                     [dict(entry) for entry in revisions]
                                     if isinstance(revisions, list) else [])})
        # artifact.created：产物清单从目录现扫（_artifacts），没有快照字段要补。
    return status, jobs, action_items


class RefOut(BaseModel):
    picture: int
    name: str = ""
    description: str = ""
    path: str = ""


class VideoOutputOut(BaseModel):
    ratio: str
    megapixels: float
    multiple: int


class BriefOut(BaseModel):
    mode: str
    variant: str
    director_mode: str = ""
    duration: float
    style: str
    language: str
    plot: str
    clarifications: str = ""
    draft: str = ""
    canvases: dict[str, list[int]] = Field(default_factory=dict)
    video_output: VideoOutputOut | None = None
    refs: list[RefOut] = Field(default_factory=list)


class JobSummary(BaseModel):
    job_id: str
    kind: str = "advance"
    status: str
    started_at: str = ""
    finished_at: str = ""
    error_code: str | None = None
    cancelled_at_node: str | None = None
    # 取消是协作式的：请求后只打标记，job 要等当前节点跑完才进 cancelled。
    cancel_requested: bool = False


class RevisionOut(BaseModel):
    """一条用户修订的对外回显：内部四档层次 + 意见原文（D4 spec §7）。

    历史从**待办列表**重建（决议事件带着当时的载荷），与 ``stage_state.user_revisions``
    是两本账：前者是「用户在这张闸门上说过什么」，后者带轮次与落地标记、只给重跑用。
    """

    layer: str
    text: str


class ActionItemOut(BaseModel):
    item_id: str
    kind: str
    status: str
    created_at: str = ""
    resolved_at: str = ""
    decision: str | None = None
    # 决议时提交的「层次 + 意见」，按提交顺序原样回显；未决议与 approve 是空表。
    revisions: list[RevisionOut] = Field(default_factory=list)


class ArtifactInfo(BaseModel):
    artifact_id: str
    kind: str
    name: str
    bytes: int
    created_at: str


@dataclass(frozen=True)
class MediaResource:
    """一份可读字节的磁盘文件及其缓存标识（#222 §6.1）。

    ``etag`` 由各来源自己给：素材的文件名**就是**内容哈希，成片则用「mtime+大小」
    ——为一次请求去哈希几百 MiB 的成片是拿磁盘换一个不会变的字符串。
    """

    path: Path
    media_type: str
    size: int
    mtime: float
    etag: str
    # 显示名（进 ``Content-Disposition``）；素材取边车里的名字，产物就是文件名。
    filename: str = ""


class Asset(BaseModel):
    """上传素材的对外形状（#222 §9）。``asset_id`` 与 ``artifact_id`` 同形：
    ``<session_id>/<sha256 前 16 位>``——路径参数按 ``{asset_id:path}`` 承接。"""

    asset_id: str
    kind: str
    name: str
    bytes: int
    created_at: str


class SessionSummary(BaseModel):
    session_id: str
    status: SessionStatus
    legacy: bool
    revision: int
    created_at: str
    updated_at: str
    plot: str
    mode: str
    variant: str


class SessionDetail(SessionSummary):
    brief: BriefOut
    last_event_seq: int
    current_job: JobSummary | None = None
    jobs: list[JobSummary] = Field(default_factory=list)
    action_items: list[ActionItemOut] = Field(default_factory=list)
    artifacts: list[ArtifactInfo] = Field(default_factory=list)
    # 素材是目录现扫的结果，与产物同款（#222 §5.3）：不进快照、不占会话状态写入。
    assets: list[Asset] = Field(default_factory=list)


class SessionPage(BaseModel):
    items: list[SessionSummary]
    next_cursor: str | None = None


class IdempotencyRegistry:
    """``key → 响应`` 的最近 N 组登记表（spec §6）。

    每会话一份（``<会话目录>/.idempotency.json``），建会话用根目录下的一份。
    FIFO 淘汰：文件里仍保留插入顺序，超过容量就丢最老的键。
    """

    def __init__(self, path: Path, capacity: int = _IDEMPOTENCY_CAPACITY) -> None:
        self.path = Path(path)
        self.capacity = capacity

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 登记表坏了不等于请求该失败；但也不能静默——丢掉它意味着幂等重发会
            # 重复建目录，日志要留下线索。
            logger.warning("幂等登记表无法读取，按空表处理：%s（%s）", self.path, exc)
            return {}
        entries = raw.get("entries") if isinstance(raw, dict) else None
        return dict(entries) if isinstance(entries, dict) else {}

    def replay(self, key: str, *, fingerprint: str) -> dict[str, Any] | None:
        entry = self._read().get(key)
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise IdempotencyConflict(
                f"Idempotency-Key {key!r} 已用于另一份请求体；换一个键或原样重发。")
        response = entry.get("response")
        return response if isinstance(response, dict) else None

    def record(self, key: str, *, fingerprint: str, response: dict[str, Any]) -> None:
        entries = self._read()
        entries[key] = {
            "fingerprint": fingerprint,
            "response": response,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        while len(entries) > self.capacity:
            entries.pop(next(iter(entries)))
        atomic_write_text(
            self.path,
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
        )


def _status_for(raw_status: str, *, legacy: bool) -> SessionStatus:
    if not legacy:
        if raw_status not in SESSION_STATUSES:
            raise ValueError(f"工作台会话的状态非法：{raw_status!r}")
        return cast(SessionStatus, raw_status)
    return cast(SessionStatus, _LEGACY_STATUS.get(raw_status, "interrupted"))


def _brief_out(brief: Brief) -> BriefOut:
    return BriefOut(
        mode=brief.mode,
        variant=brief.variant,
        director_mode=brief.director_mode,
        duration=brief.duration,
        style=brief.style,
        language=brief.language,
        plot=brief.plot,
        clarifications=brief.clarifications,
        draft=brief.draft,
        canvases={family: [width, height]
                  for family, (width, height) in brief.canvases.items()},
        video_output=(
            VideoOutputOut(
                ratio=brief.video_output.ratio,
                megapixels=brief.video_output.megapixels,
                multiple=brief.video_output.multiple,
            )
            if brief.video_output is not None else None
        ),
        refs=[RefOut(picture=item.picture, name=item.name,
                    description=item.description, path=item.path)
              for item in brief.refs],
    )


def _artifact_info(session_id: str, name: str, path: Path) -> ArtifactInfo:
    stat = path.stat()
    return ArtifactInfo(
        artifact_id=f"{session_id}/{name}",
        kind=_KNOWN_ARTIFACTS[name],
        name=name,
        bytes=stat.st_size,
        created_at=datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc).isoformat(),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _split_artifact_id(artifact_id: str) -> tuple[str, str]:
    """``<会话标识>/<文件名>`` → 两段；缺会话段或文件名一律 404。

    会话标识本身可以含斜杠，所以按**最后一个**斜杠切；越界与不存在的判定在各自的
    ``_resolve`` / ``_artifact_path`` 里，不在这里替它们下结论。
    """
    session_id, _, name = artifact_id.rpartition("/")
    if not session_id or not name:
        raise ArtifactNotFound(f"产物标识必须形如 <会话标识>/<文件名>：{artifact_id}")
    return session_id, name


def _unlink_quietly(path: Path) -> None:
    """删临时文件；已经不在就当成功。清理路径不该因为「本来就没有」再失败一次。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除素材临时文件失败：%s（%s）", path, exc)


def _artifacts(session_id: str, directory: Path, root: Path) -> list[ArtifactInfo]:
    """会话目录下已知文件名的产物元数据；不读内容（内容读取归 #220）。

    解析后不在登记根之下的条目不出现：名字合法不代表落点在根内（符号链接）。
    """
    found: list[ArtifactInfo] = []
    for name in sorted(_KNOWN_ARTIFACTS):
        path = (directory / name).resolve()
        if not path.is_file() or not _is_within(path, root):
            continue
        found.append(_artifact_info(session_id, name, path))
    return found


def _is_webm(head: bytes) -> bool:
    """EBML 容器里只接受 WebM：DocType 元素（ID ``0x4282``）的值必须是 ``webm``。

    Matroska（``matroska``）与 WebM 共用同一个 4 字节 EBML 头。只认头会把 ``.mkv``
    当视频收进来，而它不在 §5.2 的白名单里（复审实测）。
    """
    marker = head.find(b"\x42\x82")
    if marker < 0:
        return False
    return b"webm" in head[marker:marker + 16]


def _sniff_asset(head: bytes) -> tuple[str, str] | None:
    """按前若干字节判定 ``(kind, 落盘扩展名)``；认不出返回 None。

    只认魔数、不解码（#222 §5.2）：魔数对但内容损坏的文件放行，交给下游报错。
    RIFF 同时是 WebP 与 WAV 的容器头，``ftyp`` 同时被 MP4 / MOV / HEIC 使用，
    EBML 同时被 WebM / Matroska 使用——这三处都必须再往后看一层才算定案。
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpg"
    if head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image", "webp"
        if head[8:12] == b"WAVE":
            return "audio", "wav"
        return None
    if head[4:8] == b"ftyp":
        if head[8:12] in _MP4_BRANDS:
            return "video", "mp4"
        return None
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ("video", "webm") if _is_webm(head) else None
    # MP3 有两种开头：ID3v2 标签，或直接是帧同步（``0xFF`` + 高三位全 1）。
    # JPEG 的 ``0xFF 0xD8`` 不满足后半条（``0xD8 & 0xE0 != 0xE0``），不会撞车。
    if head.startswith(b"ID3"):
        return "audio", "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio", "mp3"
    return None


def _claimed_extension(filename: str) -> str:
    """取用户文件名声明的扩展名（小写、不含点）；取不出返回空串。"""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    _, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 8:
        return ""
    return ext.lower()


def _check_declared_extension(filename: str, ext: str) -> None:
    """声明的扩展名与嗅探结果矛盾时按 415 拒绝（#222 §5.2 / §14）。

    为什么不是「以字节为准、悄悄改扩展名」：扩展名是发送方的声明，字节是事实；
    两者矛盾说明这份上传的来源本身不可信，替它挑一个不报错，等于把矛盾藏起来。
    声明里没有扩展名（或扩展名不在已知表里）时不做这层判定——那不算矛盾。
    """
    claimed = _claimed_extension(filename)
    known = _ASSET_FORMATS.get(claimed)
    if known is None:
        return
    if known[0] != _ASSET_FORMATS[ext][0] or known[1] != _ASSET_FORMATS[ext][1]:
        raise UnsupportedMediaType(
            f"文件内容的实际格式（{ext}）与文件名声明的扩展名（{claimed}）不符。")


def _display_name(filename: str, fallback: str) -> str:
    """显示名：去掉路径分隔符与控制字符，超长保留尾部（保住扩展名）。

    它**永不参与路径构造**（#222 §5.2）：落盘名永远是内容哈希加嗅探出的扩展名，
    所以这里清洗不干净也不会影响落点，只影响用户看到的名字。
    """
    cleaned = "".join(
        ch for ch in filename
        if ch not in '\\/:*?"<>|' and ord(ch) >= 32 and ch != "\x7f")
    cleaned = cleaned.strip()
    if not cleaned:
        return fallback
    return cleaned[-ASSET_NAME_MAX_CHARS:]


def _read_asset_sidecar(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 边车坏了不影响正本可用：按最小形态退回（显示名由哈希现拼）。
        logger.warning("素材边车无法读取，按最小形态处理：%s（%s）", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def _asset_sidecar(name: str, kind: str, size: int, created_at: str) -> str:
    return json.dumps(
        {"name": name, "kind": kind, "bytes": size, "created_at": created_at},
        ensure_ascii=False, indent=2)


def _asset_default_name(hash16: str, ext: str) -> str:
    """没有可用显示名时的兜底名：孤儿正本与「用户没给文件名」都走它。"""
    return f"asset-{hash16}.{ext}"


def _resolved_asset_file(path: Path, root: Path) -> tuple[Path, re.Match[str]] | None:
    """素材正本 → ``(解析后的路径, 名字匹配)``；残片、边车与根外落点返回 ``None``。

    列表扫描与媒体读取共用这一份判定：两处各写一遍时，改「什么算素材」只会改到一处
    （``.tmp-*`` 与边车靠的是同一个命名正则）。
    """
    match = _ASSET_NAME_RE.match(path.name)
    if match is None:
        return None
    if match.group("ext") not in _ASSET_FORMATS:
        return None
    resolved = path.resolve()
    if not resolved.is_file() or not _is_within(resolved, root):
        return None
    return resolved, match


def _asset_from_path(session_id: str, path: Path, root: Path) -> Asset | None:
    """把一个磁盘文件解释成素材；不是素材形态（临时文件、边车、未知扩展名）返回 None。

    ``bytes`` 与 ``kind`` 一律从磁盘取：边车只是**显示名与首次创建时间**的载体，
    它坏了或被改过都不该让列表里的字节数说谎。
    """
    found = _resolved_asset_file(path, root)
    if found is None:
        return None
    resolved, match = found
    ext = match.group("ext")
    known = _ASSET_FORMATS[ext]
    hash16 = match.group("hash")
    stat = resolved.stat()
    kind = known[0]
    name = _asset_default_name(hash16, ext)
    created_at = datetime.fromtimestamp(
        stat.st_mtime, tz=timezone.utc).isoformat()
    sidecar = _read_asset_sidecar(resolved.with_suffix(".json"))
    if sidecar is not None:
        if isinstance(sidecar.get("name"), str) and sidecar["name"]:
            name = str(sidecar["name"])
        if isinstance(sidecar.get("created_at"), str) and sidecar["created_at"]:
            created_at = str(sidecar["created_at"])
    return Asset(asset_id=f"{session_id}/{hash16}", kind=kind, name=name,
                 bytes=stat.st_size, created_at=created_at)


def _assets_of(session_id: str, directory: Path, root: Path) -> list[Asset]:
    """会话素材清单：``assets/`` 目录现扫（#222 §5.3，与产物登记同款）。

    没有登记表要维护，所以「残片不出现在列表里」不靠清洗、靠命名规则：只有
    ``<16 位十六进制>.<已知扩展名>`` 才是素材，``.tmp-*`` 与边车天然落选。
    """
    assets_dir = directory / ASSETS_DIRNAME
    if not assets_dir.is_dir():
        return []
    found: list[Asset] = []
    for path in sorted(assets_dir.iterdir()):
        asset = _asset_from_path(session_id, path, root)
        if asset is not None:
            found.append(asset)
    found.sort(key=lambda item: (item.created_at, item.asset_id))
    return found


def _encode_cursor(updated_at: str, session_id: str) -> str:
    raw = json.dumps(
        {"u": updated_at, "s": session_id}, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    return str(raw["u"]), str(raw["s"])


def _is_after(item: SessionSummary, cursor: tuple[str, str]) -> bool:
    """列表顺序：``updated_at`` 降序，同刻按 ``session_id`` 升序。"""
    updated_at, session_id = cursor
    if item.updated_at != updated_at:
        return item.updated_at < updated_at
    return item.session_id > session_id


class WorkbenchSessionStore:
    """``sessions_root`` 之上的会话仓库。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # 建会话涉及「分配 GEN 号 + 写文件 + 记幂等」三步，进程内串行避免两发请求
        # 同时看到同一个空目录、都挑 GEN001。
        self._create_lock = threading.Lock()
        # 每会话一把进程内互斥锁，覆盖「读快照 → 改字段 → 整份写回」。执行器的进度写
        # （worker 线程）与取消请求（请求线程）会并发落到同一个会话文件；没有这把锁，
        # 先读到快照的一方会把另一方刚写的字段覆盖回旧值（#217 复审）。它是
        # ``threading.Lock`` 而不是 ``asyncio.Lock``：调用方在 ``asyncio.to_thread``
        # 的线程里，跨线程加锁只能靠线程原语。
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _resolve(self, session_id: str) -> Path:
        """把 ``session_id`` 解析成会话目录；越界、绝对路径、``..`` 一律 404。"""
        if not session_id or "\\" in session_id or session_id.startswith("/"):
            raise SessionNotFound("会话标识必须形如 <主题>/GEN00N。")
        parts = session_id.split("/")
        if any(part in ("", ".", "..") or ":" in part for part in parts):
            raise SessionNotFound("会话标识必须形如 <主题>/GEN00N。")
        root = self.root.resolve()
        directory = (root / Path(*parts)).resolve()
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise SessionNotFound("会话不在登记目录之下。") from exc
        if directory == root or not directory.is_dir():
            raise SessionNotFound(f"会话不存在：{session_id}")
        return directory

    def _derived_session_id(self, directory: Path) -> str:
        return directory.relative_to(self.root.resolve()).as_posix()

    def _load(self, session_id: str) -> tuple[Path, Any, str, bool]:
        directory = self._resolve(session_id)
        session = load_session(directory)
        if session is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        derived = self._derived_session_id(directory)
        legacy = (session.schema_version != WORKBENCH_SCHEMA_VERSION
                  or not session.session_id)
        if not legacy and session.session_id != derived:
            # 冗余存的 session_id 是自校验用的：对不上说明文件被搬过或改过，
            # 静默按目录继续等于把一份来历不明的快照当成合法会话。
            raise ValueError(
                f"会话文件的 session_id 与目录不符：{session.session_id!r} != {derived!r}")
        return directory, session, derived, legacy

    def _summary(self, session_id: str, session: Any, legacy: bool) -> SessionSummary:
        return SessionSummary(
            session_id=session_id,
            status=_status_for(session.status, legacy=legacy),
            legacy=legacy,
            revision=session.revision,
            created_at=session.created_at,
            updated_at=session.updated_at,
            plot=session.brief.plot,
            mode=session.brief.mode,
            variant=session.brief.variant,
        )

    def create(
        self,
        brief: Brief,
        *,
        idempotency_key: str | None = None,
        fingerprint: str,
    ) -> tuple[SessionDetail, bool]:
        """建会话；返回 ``(详情, 是否幂等重放)``。

        ``fingerprint`` 是请求体的摘要：同一个 key 配不同请求体是冲突，不能把
        第一次的响应冒充成第二次的答案（spec 错误码 ``idempotency_conflict``）。
        """
        registry = IdempotencyRegistry(
            self.root / ".workbench" / "create-idempotency.json")
        with self._create_lock:
            if idempotency_key:
                cached = registry.replay(idempotency_key, fingerprint=fingerprint)
                if cached is not None:
                    return SessionDetail.model_validate(cached), True

            # 延迟导入：``topic_generation`` 顶层拉起 LangGraph 管线，API 启动不该为
            # 一个 slug 函数付整条生成链的导入成本。
            from .topic_generation import topic_slug

            slug_dir = self.root / topic_slug(brief.plot)
            slug_dir.mkdir(parents=True, exist_ok=True)
            numbers = [
                int(match.group(1))
                for path in slug_dir.iterdir()
                if path.is_dir() and (match := _GEN_RE.match(path.name))
            ]
            generation = f"GEN{max(numbers, default=0) + 1:03d}"
            directory = slug_dir / generation
            directory.mkdir(parents=True, exist_ok=False)
            session_id = f"{slug_dir.name}/{generation}"
            # 与 ``update`` 共用同一把会话锁：建完立刻就有读者/写者时（幂等重放、
            # 紧跟着的详情查询），不会看到「文件已写、幂等表还没记」的中间态。
            with self._lock_for(session_id):
                # 先事件、后快照（ADR 0032 第 5 条）：崩在两者之间时，快照的
                # last_event_seq 落后，下一次读取按事件重放补齐。
                created = EventLog(directory, session_id).append(
                    [EventDraft("session.created", {"status": "created"})])[0]
                save_session(
                    directory, brief, {},
                    status="created",
                    session_id=session_id,
                    last_event_seq=created.seq,
                )
                detail = self._detail_unlocked(session_id)
                if idempotency_key:
                    registry.record(
                        idempotency_key, fingerprint=fingerprint,
                        response=detail.model_dump(mode="json"))
            return detail, False

    def get_session(self, session_id: str) -> SessionDetail:
        """会话详情；快照落后于事件流时先按事件重放补齐（spec §5.3）。

        为什么要在这里重放而不是只在启动时：崩溃窗口落在「事件已落盘、快照还没写」
        之间时，进程可能还活着（只有那个 job 崩了），读者下一次查询就该看到补齐后的
        真相。补齐是落盘的，所以这件事每个会话最多发生一次。
        """
        with self._lock_for(session_id):
            self._replay_if_needed(session_id)
            return self._detail_unlocked(session_id)

    def _detail_unlocked(self, session_id: str) -> SessionDetail:
        directory, session, derived, legacy = self._load(session_id)
        summary = self._summary(derived, session, legacy)
        jobs = [JobSummary.model_validate(item) for item in session.jobs]
        action_items = [ActionItemOut.model_validate(item)
                        for item in session.action_items]
        current_job = next(
            (job for job in reversed(jobs) if job.status == "running"), None)
        return SessionDetail(
            **summary.model_dump(),
            brief=_brief_out(session.brief),
            last_event_seq=session.last_event_seq,
            current_job=current_job,
            jobs=jobs,
            action_items=action_items,
            artifacts=_artifacts(derived, directory, self.root.resolve()),
            assets=_assets_of(derived, directory, self.root.resolve()),
        )

    def _replay_if_needed(self, session_id: str) -> None:
        """快照的 ``last_event_seq`` 落后于事件流尾号时，把差额重放进快照并落盘。

        必须是幂等的：事件只是记录、不是命令（ADR 0032 第 4 条），重放同一批事件
        两次与一次结果相同，见 ``_replay_snapshot``。
        """
        directory, session, derived, legacy = self._load(session_id)
        if legacy:
            # 历史会话没有事件流，也不参与补齐（spec §5.4）。
            return
        log = EventLog(directory, derived)
        tail = log.last_seq()
        if tail <= session.last_event_seq:
            return
        status, jobs, action_items = _replay_snapshot(
            session, log.read(after_seq=session.last_event_seq))
        logger.info("快照落后事件流，已重放补齐：%s（%d → %d）",
                    session_id, session.last_event_seq, tail)
        self._state_with(
            directory, session, derived, status=status, jobs=jobs,
            action_items=action_items, last_event_seq=tail).save()

    def _state_with(
        self,
        directory: Path,
        session: SessionState,
        derived: str,
        *,
        status: str | None = None,
        stage_state: dict[str, Any] | None = None,
        jobs: Sequence[dict[str, Any]] | None = None,
        action_items: Sequence[dict[str, Any]] | None = None,
        last_event_seq: int | None = None,
        brief: Brief | None = None,
        variant_downgraded: str | None = None,
    ) -> SessionState:
        """以现有快照为底，覆盖调用方点名要改的字段。"""
        return SessionState(
            directory=directory,
            brief=session.brief if brief is None else brief,
            stage_state=(
                dict(stage_state) if stage_state is not None
                else dict(session.stage_state)),
            status=session.status if status is None else status,
            variant_downgraded=(
                session.variant_downgraded if variant_downgraded is None
                else variant_downgraded),
            frame_images=session.frame_images,
            created_at=session.created_at,
            updated_at=session.updated_at,
            schema_version=session.schema_version,
            session_id=derived,
            revision=session.revision,
            last_event_seq=(
                session.last_event_seq if last_event_seq is None
                else last_event_seq),
            jobs=tuple(jobs) if jobs is not None else session.jobs,
            action_items=(
                tuple(action_items) if action_items is not None
                else session.action_items),
        )

    def list_sessions(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> SessionPage:
        limit = max(1, min(limit, 200))
        summaries: list[SessionSummary] = []
        if self.root.is_dir():
            for path in self.root.glob(f"**/{SESSION_FILENAME}"):
                directory = path.parent
                session_id = directory.resolve().relative_to(
                    self.root.resolve()).as_posix()
                try:
                    session = load_session(directory)
                except ValueError as exc:
                    # 单个损坏文件不许炸掉整个列表；它也不许静默消失（留日志）。
                    logger.warning("会话文件损坏，列表已跳过：%s（%s）", directory, exc)
                    continue
                if session is None:
                    continue
                legacy = (session.schema_version != WORKBENCH_SCHEMA_VERSION
                          or not session.session_id)
                summaries.append(self._summary(session_id, session, legacy))
        # 两趟稳定排序：先按 id 升序，再按 updated_at 降序。
        summaries.sort(key=lambda item: item.session_id)
        summaries.sort(key=lambda item: item.updated_at, reverse=True)

        if cursor:
            after = _decode_cursor(cursor)
            summaries = [item for item in summaries if _is_after(item, after)]
        page = summaries[:limit]
        next_cursor = None
        if page and len(summaries) > limit:
            last = page[-1]
            next_cursor = _encode_cursor(last.updated_at, last.session_id)
        return SessionPage(items=page, next_cursor=next_cursor)

    def ensure_can_advance(self, detail: SessionDetail) -> None:
        """推进前的闸门（#217 的作业入口复用这一处判定）。"""
        if detail.legacy:
            raise SessionLegacy("历史会话（legacy）只读，不能起任务。")
        if detail.status == "running":
            raise SessionBusy("会话已有任务在跑；同一会话同时只允许一个任务。")
        if detail.status == "completed":
            raise SessionCompleted("会话已完成，不能再推进。")
        if detail.status == "awaiting_human":
            raise ActionItemPending("会话停在人工待办上，请先提交决定。")

    def session_directory(self, session_id: str) -> Path:
        """会话目录（执行器要在旁边写 ``active-job.json``）。"""
        return self._resolve(session_id)

    def write_text_artifact(self, session_id: str, name: str, text: str,
                            *, job_id: str | None = None) -> ArtifactInfo:
        """写一份文本产物并登记：写盘与发事件只此一处，后来的产出方照这条入口走。

        先落盘再登记——事件说的必须是已经发生的事实，反过来会让订阅者拿到一个
        还不存在的产物标识。
        """
        if name not in _ARTIFACT_KINDS:
            raise ValueError(f"不是文本产物名：{name!r}；可用：{sorted(_ARTIFACT_KINDS)}")
        directory = self.session_directory(session_id)
        atomic_write_text(directory / name, text)
        return self.register_artifact(session_id, name, job_id=job_id)

    def register_artifact(self, session_id: str, name: str, *,
                          job_id: str | None = None) -> ArtifactInfo:
        """产物登记的唯一入口：文件必须已在盘上，这里只发 ``artifact.created``。

        产物清单本身从目录现扫（``_artifacts``），事件是给订阅者的「它出现了」信号；
        重复产出同名文件是真实的第二次创建，每次都发，不在登记表里去重。
        """
        if name not in _KNOWN_ARTIFACTS:
            raise ArtifactNotFound(f"不是登记范围内的产物名：{name!r}")
        directory = self.session_directory(session_id)
        path = self._artifact_path(directory, name)
        if path is None:
            raise ArtifactNotFound(f"产物不存在：{session_id}/{name}")
        info = _artifact_info(session_id, name, path)
        self.record_events(session_id, [EventDraft(
            "artifact.created",
            {"artifact_id": info.artifact_id, "kind": info.kind, "name": info.name},
            job_id=job_id)])
        return info

    def read_artifact_text(self, artifact_id: str) -> tuple[ArtifactInfo, str]:
        """文本产物全文；``artifact_id`` = ``<session_id>/<文件名>``（spec §7 / §10）。

        媒体类与越界标识共用 404：能区分「不存在」与「越界」就等于泄漏登记根之外
        是否真有文件（spec §12）。
        """
        session_id, name = _split_artifact_id(artifact_id)
        if name not in _ARTIFACT_KINDS:
            raise ArtifactNotFound(f"没有可读的文本产物：{artifact_id}")
        try:
            directory = self.session_directory(session_id)
        except SessionNotFound as exc:
            # 产物端点不回报「会话存在与否」：越界、不存在的会话与不存在的文件同码。
            raise ArtifactNotFound(f"产物不存在：{artifact_id}") from exc
        path = self._artifact_path(directory, name)
        if path is None:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}")
        size = path.stat().st_size
        if size > ARTIFACT_MAX_BYTES:
            raise ArtifactTooLarge(
                f"文本产物 {artifact_id} 有 {size} 字节，超过 "
                f"{ARTIFACT_MAX_BYTES} 字节上限。")
        return _artifact_info(session_id, name, path), path.read_text(encoding="utf-8")

    def is_media_artifact(self, artifact_id: str) -> bool:
        """该名字按**媒体**形态读吗（False ⇒ 纯文本 + 1 MiB 上限）。

        形态由登记表派生（文本表与媒体表各一张），端点不再抄第二份白名单
        （#222 §7）。名字不在任何一张表里就 404，与读内容同码。
        """
        _session_id, name = _split_artifact_id(artifact_id)
        if name in ARTIFACT_MEDIA_TYPES:
            return True
        if name in _ARTIFACT_KINDS:
            return False
        raise ArtifactNotFound(f"没有可读的产物：{artifact_id}")

    def resolve_artifact_media(self, artifact_id: str) -> MediaResource:
        """媒体产物的可读字节（#222 §7）：与素材共用同一套 Range 口径。"""
        session_id, name = _split_artifact_id(artifact_id)
        media_type = ARTIFACT_MEDIA_TYPES.get(name)
        if media_type is None:
            raise ArtifactNotFound(f"没有可读的媒体产物：{artifact_id}")
        try:
            directory = self.session_directory(session_id)
        except SessionNotFound as exc:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}") from exc
        path = self._artifact_path(directory, name)
        if path is None:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}")
        stat = path.stat()
        return MediaResource(
            path=path,
            media_type=media_type,
            size=stat.st_size,
            mtime=stat.st_mtime,
            # 不哈希成片：一次请求读几百 MiB 换个缓存标识不值，mtime+大小够本机用。
            etag=f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
            filename=name,
        )

    def _artifact_path(self, directory: Path, name: str) -> Path | None:
        """解析成真实文件并确认仍在登记根之下；符号链接逃逸返回 ``None``。

        名字白名单已经排除了 ``/`` 与 ``..``，这里再解析一次是因为**文件本身**可能
        是指向根外的符号链接——名字合法不代表落点在根内。
        """
        path = (directory / name).resolve()
        if not path.is_file() or not _is_within(path, self.root.resolve()):
            return None
        return path

    def ensure_upload_target(self, session_id: str) -> None:
        """上传前的会话判定：存在且非 legacy（#222 §5.2 的第一条）。

        单独成方法是为了让路由能在**读请求体之前**就把 404/409 发出去：先收完 512 MiB
        再告诉对方「这个会话不存在」没有道理，也不是 spec 的检查顺序。
        """
        _directory, _session, _derived, legacy = self._load(session_id)
        if legacy:
            raise SessionLegacy("历史会话（legacy）只读，不能上传素材。")

    def list_assets(self, session_id: str) -> list[Asset]:
        """会话的素材清单（目录现扫）；会话不存在与产物清单同口径，一律 404。"""
        directory, _session, derived, _legacy = self._load(session_id)
        return _assets_of(derived, directory, self.root.resolve())

    def resolve_asset_media(self, asset_id: str) -> MediaResource:
        """把 ``<会话标识>/<16 位内容哈希>`` 解析成可读字节的素材文件。

        ``asset_id`` 只认 §5.4 的形态（哈希前缀、无扩展名）：扩展名由磁盘上的文件
        决定，客户端拼 URL 时不该猜它。非素材命名形态、根外落点、符号链接逃逸与
        不存在的素材一律 ``asset_not_found``（#222 §8，与产物读取同一取舍）。
        """
        session_id, _, hash16 = asset_id.rpartition("/")
        if not session_id or _ASSET_ID_RE.match(hash16) is None:
            raise AssetNotFound("素材标识必须形如 <会话标识>/<16 位内容哈希>。")
        try:
            directory, _session, derived, _legacy = self._load(session_id)
        except SessionNotFound as exc:
            raise AssetNotFound(f"素材不存在：{asset_id}") from exc
        root = self.root.resolve()
        assets_dir = directory / ASSETS_DIRNAME
        # 前缀碰撞时同一哈希可能对应多个扩展名文件；按名字排序取第一个，结果稳定。
        for candidate in sorted(assets_dir.glob(f"{hash16}.*")):
            found = _resolved_asset_file(candidate, root)
            if found is None:
                continue  # 边车（.json）、临时文件与根外落点都落选
            resolved, match = found
            asset = _asset_from_path(derived, resolved, root)
            if asset is None:
                continue
            stat = resolved.stat()
            return MediaResource(
                path=resolved,
                media_type=ASSET_MEDIA_TYPES[match.group("ext")],
                size=stat.st_size,
                mtime=stat.st_mtime,
                etag=match.group("hash"),
                filename=asset.name,
            )
        raise AssetNotFound(f"素材不存在：{asset_id}")

    def store_asset(
        self,
        session_id: str,
        source: BinaryIO,
        *,
        filename: str = "",
        idempotency_key: str | None = None,
    ) -> tuple[Asset, bool]:
        """把一份上传写进 ``<会话目录>/assets``；返回 ``(素材, 是否幂等重放)``。

        顺序照 #222 §5.2：会话存在且非 legacy → 嗅探（顺带定上限）→ 边写边判超限。
        嗅探**先于**读完整份内容：类型不在白名单就立刻 415，不陪一份 512 MiB 的垃圾
        走完全程。

        「流式」的边界要说清：multipart 的解析由 Starlette 完成，>1 MiB 的部件先落
        **系统临时目录**里的 SpooledTemporaryFile——那一层不归本函数管（#222 §13 已
        记录）。这里保证的是「从部件到会话目录」这一段逐块搬运、逐块计数、超限即停，
        整份内容从不进内存。

        幂等键的指纹是**完整内容哈希**：同一个键配另一份内容属于冲突，不能把第一次的
        素材冒充成第二次的答案（与建会话的 ``idempotency_conflict`` 同一口径）。指纹
        就是内容本身，所以重放判定只能在读完之后——那一次仍会先落临时文件再删掉；
        「不重复写盘」说的是**正式正本**一份都不多写（#222 §5.3 的去重同款）。
        """
        # 会话判定与路由共用一处（``ensure_upload_target``）：两处各写一遍 legacy
        # 判据时，改判据的人只会改到一处。
        self.ensure_upload_target(session_id)
        directory, _session, derived, _legacy = self._load(session_id)
        assets_dir = directory / ASSETS_DIRNAME
        tmp = assets_dir / f"{ASSET_TMP_PREFIX}{secrets.token_hex(8)}"
        digest = hashlib.sha256()
        total = 0
        kind = ""
        ext = ""
        limit = 0
        try:
            try:
                assets_dir.mkdir(parents=True, exist_ok=True)
                handle = tmp.open("wb")
            except OSError as exc:
                raise StorageUnavailable(f"素材目录写不进去：{exc}") from exc
            with handle:
                while True:
                    chunk = source.read(ASSET_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not ext:
                        sniffed = _sniff_asset(chunk[:ASSET_SNIFF_BYTES])
                        if sniffed is None:
                            raise UnsupportedMediaType(
                                "认不出上传内容的类型：只接受 PNG / JPEG / WebP 图片、"
                                "MP4 / WebM 视频与 WAV / MP3 音频。")
                        kind, ext = sniffed
                        _check_declared_extension(filename, ext)
                        limit = ASSET_KIND_LIMITS[kind]
                    # 先判后写：超限的那一块一个字节都不落盘，残片也不会长过上限。
                    if total + len(chunk) > limit:
                        raise AssetTooLarge(
                            f"素材超过 {kind} 类型的 {limit} 字节上限，已中止写入。")
                    handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            if not ext:
                raise UnsupportedMediaType("上传内容为空：无法判定类型。")
        except OSError as exc:
            # 磁盘满、只读目录：请求本身没错，是这台机器的存储不行（#222 §10 的 507）。
            _unlink_quietly(tmp)
            raise StorageUnavailable(f"素材落盘失败：{exc}") from exc
        except BaseException:
            # 任何失败（超限、客户端断开）都不许留下残片。
            _unlink_quietly(tmp)
            raise

        hash_full = digest.hexdigest()
        hash16 = hash_full[:ASSET_HASH_CHARS]
        target = assets_dir / f"{hash16}.{ext}"
        registry = self.session_registry(session_id)
        with self._lock_for(session_id):
            if idempotency_key:
                cached = registry.replay(idempotency_key, fingerprint=hash_full)
                if cached is not None:
                    _unlink_quietly(tmp)
                    return Asset.model_validate(cached), True
            if target.exists():
                # 去重：同内容不产生第二份文件，``created_at`` 保持首次（#222 §5.3）。
                _unlink_quietly(tmp)
            else:
                try:
                    os.replace(tmp, target)
                except OSError as exc:
                    _unlink_quietly(tmp)
                    raise StorageUnavailable(f"素材落盘失败：{exc}") from exc
            sidecar = target.with_suffix(".json")
            if not sidecar.exists():
                # 先正本后边车（#222 §5.3）：崩在两者之间只留孤儿正本，启动时补边车。
                try:
                    atomic_write_text(sidecar, _asset_sidecar(
                        _display_name(filename, _asset_default_name(hash16, ext)),
                        kind, target.stat().st_size,
                        datetime.now(timezone.utc).isoformat()))
                except OSError as exc:
                    # 正本已在盘上、列表也能按最小形态读出它，但这次请求确实没完整落盘；
                    # 按存储错误回报，别让调用方以为一切正常（重发会走去重补上边车）。
                    raise StorageUnavailable(f"素材边车写入失败：{exc}") from exc
            asset = _asset_from_path(derived, target, self.root.resolve())
            if asset is None:
                raise StorageUnavailable(f"素材落盘后读不回来：{target.name}")
            if idempotency_key:
                registry.record(idempotency_key, fingerprint=hash_full,
                                response=asset.model_dump(mode="json"))
            return asset, False

    def cleanup_assets(self) -> None:
        """启动清理（#222 §5.3）：删崩溃残留的 ``.tmp-*``，给孤儿正本补最小边车。

        为什么在启动做：「残片不出现在列表里」已由命名规则保证（见 ``_assets_of``），
        这里要的是**把盘上的垃圾清掉**，而崩溃残留只可能来自上一个进程。单个文件读写
        失败不阻断启动——与 ``list_sessions`` 跳过坏文件是同一个取舍。
        """
        if not self.root.is_dir():
            return
        for path in self.root.glob(f"**/{ASSETS_DIRNAME}/*"):
            try:
                if not path.is_file():
                    continue
                if path.name.startswith(ASSET_TMP_PREFIX):
                    _unlink_quietly(path)
                    continue
                match = _ASSET_NAME_RE.match(path.name)
                if match is None:
                    continue
                known = _ASSET_FORMATS.get(match.group("ext"))
                if known is None:
                    continue
                sidecar = path.with_suffix(".json")
                if sidecar.exists():
                    continue
                hash16 = match.group("hash")
                stat = path.stat()
                atomic_write_text(sidecar, _asset_sidecar(
                    _asset_default_name(hash16, match.group("ext")), known[0],
                    stat.st_size,
                    datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()))
            except OSError as exc:
                logger.warning("启动清理素材时跳过：%s（%s）", path, exc)

    def session_directories(self) -> list[Path]:
        """登记根下所有会话目录；单个坏文件由调用方自己决定怎么跳过。"""
        if not self.root.is_dir():
            return []
        return sorted(path.parent for path in self.root.glob(f"**/{SESSION_FILENAME}"))

    def mutate(
        self,
        session_id: str,
        mutator: Callable[[SessionDetail], dict[str, Any] | None],
        *,
        events: Sequence[EventDraft] = (),
    ) -> SessionDetail:
        """在会话锁内读-改-写：``mutator`` 拿到**当前**详情，返回要覆盖的字段。

        为什么需要回调而不是先 ``get_session`` 再 ``update``：调用方（执行器的进度写与
        取消请求）来自不同线程，两次文件读之间别人可能已经写过。把读也放进临界区，
        「找 job → 改字段 → 写回」才是原子的（#217 复审：取消标记曾被下一次进度写盖掉）。
        可覆盖的字段是 ``status`` / ``stage_state`` / ``jobs`` / ``action_items`` /
        ``brief`` / ``variant_downgraded``（#219 起阶段 2 收尾要把生效变体与降级事实
        一起写回，绕开会话锁用 ``update_session_file`` 会把并发写盖掉）。

        ``events`` 是这次改动要**一起**落盘的事件（#218）：事件先写、快照后写，且两者
        在同一把锁内——序号分配与状态转移因此不会被别的写者插进来。``mutator`` 返回空
        dict / None 时不写文件、也不写事件（「没事干」的判定只有一处，不会因为带了事件
        就把一次空转记成真的发生过）。没有状态变化的进展用 ``record_events``。
        """
        lock = self._lock_for(session_id)
        with lock:
            self._replay_if_needed(session_id)
            detail = self._detail_unlocked(session_id)
            changes = mutator(detail) or {}
            if not changes:
                return detail
            directory, session, derived, legacy = self._load(session_id)
            if legacy:
                raise SessionLegacy("历史会话（legacy）只读，不能改写。")
            appended = EventLog(directory, derived).append(events)
            self._state_with(
                directory, session, derived,
                status=changes.get("status"),
                stage_state=changes.get("stage_state"),
                jobs=changes.get("jobs"),
                action_items=changes.get("action_items"),
                brief=changes.get("brief"),
                variant_downgraded=changes.get("variant_downgraded"),
                last_event_seq=(
                    appended[-1].seq if appended else session.last_event_seq),
            ).save()
            return self._detail_unlocked(session_id)

    def record_events(
        self, session_id: str, events: Sequence[EventDraft],
    ) -> SessionDetail:
        """只追加事件、不改快照字段（节点开始这类「没有状态变化」的进展）。

        快照仍然重写一次：把 ``last_event_seq`` 推到事件流尾号，否则下一次读取会把刚
        写的这条事件当成缺口，再重放一遍。
        """
        if not events:
            return self.get_session(session_id)
        with self._lock_for(session_id):
            self._replay_if_needed(session_id)
            directory, session, derived, legacy = self._load(session_id)
            if legacy:
                raise SessionLegacy("历史会话（legacy）只读，不能改写。")
            appended = EventLog(directory, derived).append(events)
            self._state_with(
                directory, session, derived,
                last_event_seq=appended[-1].seq,
            ).save()
            return self._detail_unlocked(session_id)

    def update(
        self,
        session_id: str,
        *,
        status: str | None = None,
        stage_state: dict[str, Any] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        action_items: list[dict[str, Any]] | None = None,
    ) -> SessionDetail:
        """补写会话快照的若干字段（``mutate`` 的固定字段版），其余键原样保留。

        为什么绕开 ``save_session``：执行器要在**不重建**会话对象的前提下推进
        （``variant_downgraded`` / ``frame_images`` 等键必须原样留在文件里）；重新构造
        一份缺省值的 ``SessionState`` 会把它们静默抹掉。``revision`` 由 ``save`` 按
        文件现值 +1，与建会话走同一口径。整段读-改-写在会话锁内完成，不会丢并发更新。
        """
        changes: dict[str, Any] = {}
        if status is not None:
            changes["status"] = status
        if stage_state is not None:
            changes["stage_state"] = stage_state
        if jobs is not None:
            changes["jobs"] = jobs
        if action_items is not None:
            changes["action_items"] = action_items
        return self.mutate(session_id, lambda _detail: changes)

    def session_registry(self, session_id: str) -> IdempotencyRegistry:
        """会话级幂等登记表（#217 起复用；建会话用根目录的那份）。"""
        directory = self._resolve(session_id)
        return IdempotencyRegistry(directory / ".idempotency.json")


__all__ = [
    "ActionItemNotFound", "ActionItemOut", "ActionItemPending", "ActionItemResolved",
    "ARTIFACT_FIRST_FRAME_PROMPT", "ARTIFACT_LAST_FRAME_PROMPT",
    "ARTIFACT_MAX_BYTES", "ARTIFACT_SCRIPT", "ARTIFACT_SHOT_TABLE",
    "ARTIFACT_VIDEO_PROMPT",
    "Asset", "AssetTooLarge",
    "ArtifactInfo", "ArtifactNotFound", "ArtifactTooLarge", "BriefOut",
    "EVENT_TYPES", "EVENTS_FILENAME", "Event", "EventDraft", "EventLog",
    "IdempotencyConflict", "IdempotencyRegistry", "InvalidEventPayload",
    "JobNotFound", "JobNotRunning",
    "JobSummary", "RefOut", "RevisionOut", "SessionBusy", "SessionCompleted", "SessionDetail",
    "SessionLegacy", "SessionNotFound", "SessionPage", "SessionSummary",
    "SESSION_STATUSES", "StorageUnavailable", "UnknownEventType",
    "UnsupportedMediaType", "UploadInvalid", "VideoOutputOut", "WorkbenchError",
    "WorkbenchSessionStore",
]
