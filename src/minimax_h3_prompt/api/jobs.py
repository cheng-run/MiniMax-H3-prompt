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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import step_machine
from ..errors import error_code
from ..session_store import FRAME_SUBMISSION_KEY, atomic_write_text, load_session
from ..summary import PromptSummary, render_summary_zh
from ..workbench_store import (
    ARTIFACT_FIRST_FRAME_PROMPT,
    ARTIFACT_LAST_FRAME_PROMPT,
    ARTIFACT_SCRIPT,
    ARTIFACT_SHOT_TABLE,
    ARTIFACT_SUMMARY_JSON,
    ARTIFACT_SUMMARY_MARKDOWN,
    ARTIFACT_VIDEO_PROMPT,
    ActionItemNotFound,
    ActionItemOut,
    ActionItemPending,
    ActionItemResolved,
    EventDraft,
    FrameInvalid,
    JobNotFound,
    JobNotRunning,
    JobSummary,
    RevisionOut,
    WorkbenchSessionStore,
)

logger = logging.getLogger(__name__)

ACTIVE_JOB_FILENAME = "active-job.json"
# 会话文件里保留的 job 摘要条数（spec §5.2：最近 N 条，完整历史在事件流里）。
JOB_HISTORY_LIMIT = 20

# advance 没有请求体；幂等键绑定的「请求摘要」是一个常量，同 key 重发永远算同一份命令。
_JOB_FINGERPRINT = hashlib.sha256(b"advance").hexdigest()


def _resolve_fingerprint(item_id: str, decision: str | None,
                         revisions: list[dict[str, str]],
                         frames: dict[str, str] | None) -> str:
    """resolve 的请求摘要：待办、决定、修订内容与帧槽都算进去（同 key 换内容＝冲突）。"""
    canonical = json.dumps(
        {"item_id": item_id, "decision": decision, "revisions": revisions,
         "frames": frames or {}},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# resolve 的三个去向。待办 kind 与 brief 建好后不再改写，所以先在锁外读一次决定
# 「要不要起任务」是安全的；「待办是否仍 pending」仍由 _decide 在存储层锁内判定。
# 帧选择在决议时落进 ``session_store.FRAME_SUBMISSION_KEY``（为什么要先落盘见那里）。
_BRANCH_REVIEW = "review"
_BRANCH_FRAME_GATE = "frame_gate"
_BRANCH_FRAMES = "frames"


class JobCancelled(Exception):
    """协作式取消在节点边界生效；``node`` 是停下时刚跑完的节点。"""

    def __init__(self, node: str) -> None:
        super().__init__(f"任务在当前节点结束后取消（{node or '未知'}）")
        self.node = node


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dict(job: JobSummary | dict[str, Any]) -> dict[str, Any]:
    return job.model_dump(mode="json") if isinstance(job, JobSummary) else dict(job)


def _upsert_job(jobs: Any, job: dict[str, Any]) -> list[dict[str, Any]]:
    """按 job_id 覆盖或追加，并只保留最近 ``JOB_HISTORY_LIMIT`` 条。"""
    items = [_job_dict(item) for item in jobs if item.job_id != job["job_id"]]
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
            self._started_at[job.job_id] = job.started_at
            await asyncio.to_thread(self._write_active_job, session_id, job.job_id, "")
            event = threading.Event()
            self._cancel_events[job.job_id] = event
            self._tasks[job.job_id] = asyncio.create_task(
                self._run(session_id, job.job_id, event))
            if idempotency_key and registry is not None:
                await asyncio.to_thread(
                    registry.record, idempotency_key,
                    fingerprint=_JOB_FINGERPRINT, response=_job_dict(job))
            return job, False

    async def resolve(
        self, session_id: str, item_id: str, *, decision: str | None,
        revisions: list[dict[str, str]] | None = None,
        frames: dict[str, str] | None = None,
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
                item_id, decision, list(revisions or []), frames)
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
                job_id=uuid.uuid4().hex, kind="advance", status="running",
                started_at=_now())
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
                    for role in ("first", "last"):
                        asset_id = str(frames.get(role) or "").strip()
                        if asset_id:
                            # 归属、存在性、类型都在这里判；错误一律 422 frame_invalid。
                            resolved_frames[role] = str(
                                self.store.resolve_frame_asset(session_id, asset_id))
                    holder["frames"] = resolved_frames
                    recorded_decision = step_machine.DECISION_SUBMIT_FRAMES
                    # 帧选择随决议一起落进 stage_state：慢速读图在任务里，崩溃在
                    # 两者之间时续跑仍知道用户交了哪一对素材（而不是静默降级）。
                    session = load_session(self.store.session_directory(session_id))
                    if session is None:
                        raise RuntimeError(f"会话文件读不到：{session_id}")
                    stage_state = dict(session.stage_state)
                    stage_state[FRAME_SUBMISSION_KEY] = dict(resolved_frames)
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
                "decision": (step_machine.DECISION_SUBMIT_FRAMES
                             if branch == _BRANCH_FRAMES else decision),
                "revisions": event_revisions,
            }
            if frames is not None:
                # 帧槽载荷是用户输入的一部分（D4 §9）：重放与排障要看出用户当时
                # 选了哪两个素材。审查待办没有这一项，就不写空对象进事件。
                resolved_payload["frames"] = dict(frames)
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
            self._tasks[job.job_id] = asyncio.create_task(
                self._run_decision(
                    session_id, job.job_id, event, holder["decision"], entries))
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
        if kind == step_machine.ACTION_FRAME_SUBMISSION:
            return _BRANCH_FRAMES
        if (kind == step_machine.ACTION_STAGE1_REVIEW
                and decision == step_machine.DECISION_APPROVE
                and step_machine.needs_frame_submission(session.brief.variant)):
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

    async def _run(self, session_id: str, job_id: str,
                   cancel_event: threading.Event) -> None:
        # 续跑落点由**已完成到哪**决定，不能一律从阶段 1 重进：#221 真实跑在阶段 2
        # 被网关卡住后强杀，重启再 advance 若回到阶段 1，只会重新开一个人工闸门
        # （阶段 1 已跑完、链已耗尽），阶段 2 永远走不到。
        step = await asyncio.to_thread(self._resume_step, session_id)
        try:
            if step == step_machine.STEP_STAGE2:
                result = await asyncio.to_thread(
                    self._execute_stage2, session_id, job_id, cancel_event)
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
                await asyncio.to_thread(
                    self._finish_stage2, session_id, job_id, result)
            else:
                await asyncio.to_thread(
                    self._finish_succeeded, session_id, job_id, result)
        finally:
            await asyncio.to_thread(self._cleanup, session_id, job_id)

    def _resume_step(self, session_id: str) -> str:
        """下次 advance 该跑阶段 1 还是阶段 2：看**已落盘的事实**，不看内存。

        判据两条同时成立才进阶段 2：阶段 1 已过闸门（存在 stage1_review 待办）、
        最新待办已被 approve 决议；有未决的新待办（revise 跑完会挂新待办）仍属阶段 1。
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
            return step_machine.STEP_STAGE2
        return step_machine.STEP_STAGE1

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
                await asyncio.to_thread(
                    self._finish_stage2, session_id, job_id, result)
            else:
                # 修订跑完回到**同一个人工闸门**：新待办、状态 awaiting_human。
                await asyncio.to_thread(
                    self._finish_succeeded, session_id, job_id, result["state"])
        finally:
            await asyncio.to_thread(self._cleanup, session_id, job_id)

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
                session.brief, self.config, initial_state=state,
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
        final_state = stage1_state(session.brief, dict(session.stage_state))
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

    def _execute_stage2(self, session_id: str, job_id: str,
                        cancel_event: threading.Event) -> dict[str, Any]:
        """线程里跑的阶段 2（approve / 交帧之后）。

        有帧则先读图再组装（D4 §9）：读图是慢速视觉调用，放在任务里，请求在 202
        就返回了。没有帧时的**自动降级**与向导同源（``resolve_effective_variant``），
        并把降级事实记进会话（``variant_downgraded``）——降级不静默。
        ``video-prompt.md`` 与向导同目录同名，写完才算成功。
        """
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
        brief = session.brief
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
        return {"state": dict(final_state), "brief": brief,
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
        brief = session.brief
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
        return {"state": updated, "brief": brief,
                "variant_downgraded": downgraded}

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
        brief = session.brief
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
        return {"state": state, "brief": brief}

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
        state = stage1_state(session.brief, dict(session.stage_state))
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
                       result: dict[str, Any]) -> None:
        """阶段 2 收尾：会话 completed，降级事实与生效变体一起落盘（D2-AC7 的前半）。"""
        def _apply(detail: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "succeeded", "finished_at": _now()})
            return {
                "status": step_machine.STATUS_COMPLETED,
                "stage_state": result["state"],
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
                "brief": result["brief"],
                "variant_downgraded": result["variant_downgraded"],
            }

        self.store.mutate(session_id, _apply, events=[
            EventDraft("job.succeeded",
                       {"next": step_machine.STATUS_COMPLETED},
                       job_id=job_id),
        ])

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
