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
from ..session_store import atomic_write_text, load_session
from ..workbench_store import (
    ActionItemNotFound,
    ActionItemOut,
    ActionItemPending,
    ActionItemResolved,
    EventDraft,
    JobNotFound,
    JobNotRunning,
    JobSummary,
    WorkbenchSessionStore,
)

logger = logging.getLogger(__name__)

ACTIVE_JOB_FILENAME = "active-job.json"
# 会话文件里保留的 job 摘要条数（spec §5.2：最近 N 条，完整历史在事件流里）。
JOB_HISTORY_LIMIT = 20

# advance 没有请求体；幂等键绑定的「请求摘要」是一个常量，同 key 重发永远算同一份命令。
_JOB_FINGERPRINT = hashlib.sha256(b"advance").hexdigest()


def _resolve_fingerprint(item_id: str, decision: str,
                         revisions: list[dict[str, str]]) -> str:
    """resolve 的请求摘要：待办、决定与修订内容都算进去（同 key 换内容＝冲突）。"""
    canonical = json.dumps(
        {"item_id": item_id, "decision": decision, "revisions": revisions},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

    def __init__(self, store: WorkbenchSessionStore, config: Any = None) -> None:
        self.store = store
        self._config = config
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
        self, session_id: str, item_id: str, *, decision: str,
        revisions: list[dict[str, str]] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[JobSummary, bool]:
        """在人工闸门上提交决定；返回 ``(job, 是否幂等重放)``（#219）。

        决定本身（待办转 resolved、修订进累积清单、会话写回 running）在**存储层会话锁内**
        一次落盘，事件与快照不分家；真正的重跑在后台任务里。``revise`` 的修订也在这次
        落盘里写进 ``user_revisions``——先记下用户说过的话，再花几分钟重跑；中途崩了
        也只是「没落地」，不会变成「系统没听见」。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            payload = _resolve_fingerprint(item_id, decision, list(revisions or []))
            registry = None
            if idempotency_key:
                registry = self.store.session_registry(session_id)
                cached = await asyncio.to_thread(
                    registry.replay, idempotency_key, fingerprint=payload)
                if cached is not None:
                    return JobSummary.model_validate(cached), True

            job = JobSummary(
                job_id=uuid.uuid4().hex, kind="advance", status="running",
                started_at=_now())
            # 记录下来的修订条目（带轮次）要立刻带给后台任务：稍后按它标「已落地」，
            # 重新按 (round, layer, text) 反查会把轮次对齐规则又抄一遍。
            entries: list[dict[str, Any]] = []

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
                items = [
                    (item.model_copy(update={
                        "status": "resolved", "decision": decision,
                        "resolved_at": now}).model_dump(mode="json")
                     if item.item_id == item_id
                     else item.model_dump(mode="json"))
                    for item in current.action_items
                ]
                changes: dict[str, Any] = {
                    "status": step_machine.STATUS_RUNNING,
                    "jobs": _upsert_job(current.jobs, _job_dict(job)),
                    "action_items": items,
                }
                if decision == step_machine.DECISION_REVISE:
                    # 累积语义只住 user_revisions.py（begin_round + record_user_revision）；
                    # 这里不另造轮次号，也不改清单形状。
                    from ..user_revisions import record_user_revision

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

            await asyncio.to_thread(
                self.store.mutate, session_id, _decide, events=[
                    EventDraft("job.started", {"kind": job.kind},
                               job_id=job.job_id),
                    EventDraft("action_item.resolved",
                               {"item_id": item_id, "decision": decision},
                               job_id=job.job_id),
                ])
            self._started_at[job.job_id] = job.started_at
            await asyncio.to_thread(self._write_active_job, session_id, job.job_id, "")
            event = threading.Event()
            self._cancel_events[job.job_id] = event
            self._tasks[job.job_id] = asyncio.create_task(
                self._run_decision(
                    session_id, job.job_id, event, decision, entries))
            if idempotency_key and registry is not None:
                await asyncio.to_thread(
                    registry.record, idempotency_key,
                    fingerprint=payload, response=_job_dict(job))
            return job, False

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
        try:
            final_state = await asyncio.to_thread(
                self._execute_stage1, session_id, job_id, cancel_event)
        except JobCancelled as exc:
            await asyncio.to_thread(
                self._finish_cancelled, session_id, job_id, exc.node)
        except Exception as exc:  # noqa: BLE001 - 任务失败要落成 job 状态，不能让后台任务静默死掉
            logger.exception("执行任务失败：%s / %s", session_id, job_id)
            await asyncio.to_thread(
                self._finish_failed, session_id, job_id, error_code(exc), str(exc))
        else:
            await asyncio.to_thread(
                self._finish_succeeded, session_id, job_id, final_state)
        finally:
            await asyncio.to_thread(self._cleanup, session_id, job_id)

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
        """决议的后台执行：approve → 阶段 2；revise → 从声明层次重跑阶段 1。"""
        try:
            if decision == step_machine.DECISION_APPROVE:
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
            if decision == step_machine.DECISION_APPROVE:
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
        return final_state

    def _execute_stage2(self, session_id: str, job_id: str,
                        cancel_event: threading.Event) -> dict[str, Any]:
        """线程里跑的阶段 2（approve 之后）。

        无首尾帧时的**自动降级**与向导同源（``resolve_effective_variant``）：D2 的
        帧图归 D3，所以这里永远按「没有帧」降级，并把降级事实记进会话
        （``variant_downgraded``）。``video-prompt.md`` 与向导同目录同名，写完才算成功。
        """
        from ..graph.pipeline import _STAGE2_CHAIN, run_stage2
        from ..stage2_checkpoint import load_checkpoint
        from ..ui.wizard import resolve_effective_variant

        directory = self.store.session_directory(session_id)
        session = load_session(directory)
        if session is None:
            raise RuntimeError(f"会话文件读不到：{session_id}")
        state = dict(session.stage_state)
        brief = session.brief
        original = brief.variant
        effective = resolve_effective_variant(original, False, False)
        downgraded = ""
        if effective != original:
            brief.variant = effective
            downgraded = f"{original}→{effective}"
        # 没有交回关键帧：读图结果就是空清单（向导在没有帧时同样写空列表）。
        state["fl2va_frame_descriptions"] = []
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
        (directory / "video-prompt.md").write_text(prompt, encoding="utf-8")
        return {"state": dict(final_state), "brief": brief,
                "variant_downgraded": downgraded}

    def _execute_revise(self, session_id: str, job_id: str,
                        cancel_event: threading.Event,
                        entries: list[dict[str, Any]]) -> dict[str, Any]:
        """线程里跑的修订重跑：设定级按层次截断重跑阶段 1，画面级重出关键帧提示词。

        多条修订的执行顺序＝**链上最上游的层次先跑，画面级最后**：设定级改动会换掉
        画面级节点的输入，反过来先跑画面级等于拿旧设定出词。
        """
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
        return {"state": state, "brief": brief}

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
