"""工作台会话存储：建会话、列表、详情；文件是唯一真源（D2 spec §5）。

为什么不做成数据库：一个会话是「一个目录 + 若干产物」，用户会直接看、直接拷、
直接删；文件布局本身就是交付物的一部分。事件流与快照的分工见 ADR 0032。

这一层只认 ``sessions_root`` 之下的相对路径，不接受任意本地路径——``session_id``
是登记根之下的标识（spec §5.1），越界一律按 404 处理，不泄漏根外文件是否存在。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Sequence, cast

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

# 产物登记范围（D2 spec §10）：只认已知文件名的文本产物；视频类留给 D3。
_ARTIFACT_KINDS = {
    "video-prompt.md": "video_prompt",
    "summary-zh.md": "summary_markdown",
    "summary-zh.json": "summary_json",
}

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
            for item in action_items:
                if item.get("item_id") == payload["item_id"]:
                    item.update({"status": "resolved",
                                 "decision": payload["decision"],
                                 "resolved_at": event.at})
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


class ActionItemOut(BaseModel):
    item_id: str
    kind: str
    status: str
    created_at: str = ""
    resolved_at: str = ""
    decision: str | None = None


class ArtifactInfo(BaseModel):
    artifact_id: str
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


def _artifacts(session_id: str, directory: Path) -> list[ArtifactInfo]:
    """会话目录下已知文件名的产物元数据；不读内容（内容读取归 #220）。"""
    found: list[ArtifactInfo] = []
    for name, kind in sorted(_ARTIFACT_KINDS.items()):
        path = directory / name
        if not path.is_file():
            continue
        stat = path.stat()
        found.append(ArtifactInfo(
            artifact_id=f"{session_id}/{name}",
            kind=kind,
            name=name,
            bytes=stat.st_size,
            created_at=datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc).isoformat(),
        ))
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
            artifacts=_artifacts(derived, directory),
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
    "ArtifactInfo", "BriefOut",
    "EVENT_TYPES", "EVENTS_FILENAME", "Event", "EventDraft", "EventLog",
    "IdempotencyConflict", "IdempotencyRegistry", "InvalidEventPayload",
    "JobNotFound", "JobNotRunning",
    "JobSummary", "RefOut", "SessionBusy", "SessionCompleted", "SessionDetail",
    "SessionLegacy", "SessionNotFound", "SessionPage", "SessionSummary",
    "SESSION_STATUSES", "UnknownEventType", "VideoOutputOut", "WorkbenchError",
    "WorkbenchSessionStore",
]
