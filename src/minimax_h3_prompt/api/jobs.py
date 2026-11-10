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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import step_machine
from ..errors import error_code
from ..session_store import atomic_write_text, load_session
from ..workbench_store import (
    ActionItemOut,
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

            await asyncio.to_thread(self.store.mutate, session_id, _start)
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
                self._finish_failed, session_id, job_id, error_code(exc))
        else:
            await asyncio.to_thread(
                self._finish_succeeded, session_id, job_id, final_state)
        finally:
            self._cancel_events.pop(job_id, None)
            self._tasks.pop(job_id, None)
            self._started_at.pop(job_id, None)
            try:
                await asyncio.to_thread(self._remove_active_job, session_id, job_id)
            except Exception:  # noqa: BLE001 - 清理失败不该盖掉任务终态
                logger.warning("清理在跑标记失败：%s / %s", session_id, job_id)

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

        def on_step_done(node_name: str, current_state: dict) -> None:
            progress_state = dict(current_state)
            progress_state["_progress"] = {"last_completed_node": node_name}
            self._save_progress(session_id, job_id, progress_state, node_name)
            if cancel_event.is_set():
                raise JobCancelled(node_name)

        if skip:
            final_state = state
        else:
            final_state = dict(run_stage1(
                session.brief, self.config, initial_state=state,
                resume_from_node=resume_from, on_step_done=on_step_done)[0])

        # 最后一个节点跑完到收尾之间到达的取消请求也在这里生效。
        if cancel_event.is_set():
            raise JobCancelled(str(
                (final_state.get("_progress") or {}).get("last_completed_node", "")))
        return final_state

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def _job_or_running(self, job_id: str, detail: Any) -> JobSummary:
        job = _find_job(detail.jobs, job_id)
        return job or JobSummary(job_id=job_id, status="running")

    def _save_progress(self, session_id: str, job_id: str,
                       stage_state: dict[str, Any], node: str) -> None:
        def _apply(detail: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, detail)
            return {
                "status": "running",
                "stage_state": stage_state,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
            }

        # 读与写在存储层会话锁内完成：取消请求可能刚把 job 标成 cancel_requested，
        # 先读后写会把那个标记覆盖回旧值（#217 复审）。
        self.store.mutate(session_id, _apply)
        self._write_active_job(session_id, job_id, node)

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

        self.store.mutate(session_id, _apply)

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

        self.store.mutate(session_id, _apply)

    def _finish_failed(self, session_id: str, job_id: str, code: str) -> None:
        def _apply(detail: Any) -> dict[str, Any]:
            job = self._job_or_running(job_id, detail).model_copy(
                update={"status": "failed", "finished_at": _now(),
                        "error_code": code})
            return {
                "status": step_machine.STATUS_FAILED,
                "jobs": _upsert_job(detail.jobs, _job_dict(job)),
            }

        self.store.mutate(session_id, _apply)

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

                self.store.mutate(session_id, _mark)
            except (ValueError, OSError) as exc:
                logger.warning("对账失败：%s（%s）", session_id, exc)
                continue
            (directory / ACTIVE_JOB_FILENAME).unlink(missing_ok=True)
            marked.append(session_id)
        return marked


__all__ = ["ACTIVE_JOB_FILENAME", "JOB_HISTORY_LIMIT", "JobCancelled", "JobRunner"]
