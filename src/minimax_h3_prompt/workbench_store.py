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
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, cast

from pydantic import BaseModel, Field

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


class IdempotencyConflict(WorkbenchError):
    status_code = 409
    code = "idempotency_conflict"


class JobNotFound(WorkbenchError):
    status_code = 404
    code = "job_not_found"


class JobNotRunning(WorkbenchError):
    status_code = 409
    code = "job_not_running"


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
                save_session(
                    directory, brief, {},
                    status="created",
                    session_id=session_id,
                )
                detail = self.get_session(session_id)
                if idempotency_key:
                    registry.record(
                        idempotency_key, fingerprint=fingerprint,
                        response=detail.model_dump(mode="json"))
            return detail, False

    def get_session(self, session_id: str) -> SessionDetail:
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
    ) -> SessionDetail:
        """在会话锁内读-改-写：``mutator`` 拿到**当前**详情，返回要覆盖的字段。

        为什么需要回调而不是先 ``get_session`` 再 ``update``：调用方（执行器的进度写与
        取消请求）来自不同线程，两次文件读之间别人可能已经写过。把读也放进临界区，
        「找 job → 改字段 → 写回」才是原子的（#217 复审：取消标记曾被下一次进度写盖掉）。

        ``mutator`` 返回空 dict / None 时不写文件（不白抬 revision）；返回值是写后详情。
        """
        lock = self._lock_for(session_id)
        with lock:
            detail = self.get_session(session_id)
            changes = mutator(detail) or {}
            if not changes:
                return detail
            directory, session, derived, legacy = self._load(session_id)
            if legacy:
                raise SessionLegacy("历史会话（legacy）只读，不能改写。")
            status = changes.get("status")
            stage_state = changes.get("stage_state")
            jobs = changes.get("jobs")
            action_items = changes.get("action_items")
            state = SessionState(
                directory=directory,
                brief=session.brief,
                stage_state=(
                    dict(stage_state) if stage_state is not None
                    else dict(session.stage_state)),
                status=session.status if status is None else status,
                variant_downgraded=session.variant_downgraded,
                frame_images=session.frame_images,
                created_at=session.created_at,
                updated_at=session.updated_at,
                schema_version=session.schema_version,
                session_id=derived,
                revision=session.revision,
                last_event_seq=session.last_event_seq,
                jobs=tuple(jobs) if jobs is not None else session.jobs,
                action_items=(
                    tuple(action_items) if action_items is not None
                    else session.action_items),
            )
            state.save()
            return self.get_session(session_id)

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
    "ActionItemOut", "ActionItemPending", "ArtifactInfo", "BriefOut",
    "IdempotencyConflict", "IdempotencyRegistry", "JobNotFound", "JobNotRunning",
    "JobSummary", "RefOut", "SessionBusy", "SessionCompleted", "SessionDetail",
    "SessionLegacy", "SessionNotFound", "SessionPage", "SessionSummary",
    "SESSION_STATUSES", "VideoOutputOut", "WorkbenchError", "WorkbenchSessionStore",
]
