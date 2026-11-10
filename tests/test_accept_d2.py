"""#221 验收驱动器的最小单测：只打本地假 transport，不启服务、不跑模型。

为什么不用真实服务：``scripts/accept_d2.py`` 的存在意义是「真实模型的手动验收」；
如果用例也起真服务，就把真实模型调用拖进了 CI（D2 spec §2 明确不做）。这里验证的
是驱动器自己的机械部分：报告字段、AC 勾选、失败退出。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "accept_d2.py"
SPEC = importlib.util.spec_from_file_location("accept_d2", SCRIPT)
assert SPEC and SPEC.loader
accept_d2 = importlib.util.module_from_spec(SPEC)
# dataclass 定义时要按 ``__module__`` 回查 sys.modules；不登记会拿到 None。
sys.modules[SPEC.name] = accept_d2
SPEC.loader.exec_module(accept_d2)

SID = "雨夜旧信-test/GEN001"
PROMPT = "# 视频提示词\n\n雨夜街口，一位撑黑伞的女性走过积水。\n"
EVENTS = [
    {"seq": 1, "type": "session.created", "at": "t", "session_id": SID,
     "payload": {"status": "created"}},
    {"seq": 2, "type": "job.started", "at": "t", "session_id": SID, "job_id": "job1",
     "payload": {"kind": "advance"}},
    {"seq": 3, "type": "job.progress", "at": "t", "session_id": SID, "job_id": "job1",
     "payload": {"stage": "stage1", "node": "n", "status": "done"}},
    {"seq": 4, "type": "job.succeeded", "at": "t", "session_id": SID, "job_id": "job1",
     "payload": {"next": "awaiting_human"}},
    {"seq": 5, "type": "action_item.created", "at": "t", "session_id": SID,
     "job_id": "job1", "payload": {"item_id": "item1", "kind": "stage1_review"}},
    {"seq": 6, "type": "job.started", "at": "t", "session_id": SID, "job_id": "job2",
     "payload": {"kind": "advance"}},
    {"seq": 7, "type": "action_item.resolved", "at": "t", "session_id": SID,
     "job_id": "job2", "payload": {"item_id": "item1", "decision": "approve"}},
    {"seq": 8, "type": "artifact.created", "at": "t", "session_id": SID, "job_id": "job2",
     "payload": {"artifact_id": f"{SID}/video-prompt.md", "kind": "video_prompt",
                 "name": "video-prompt.md"}},
    {"seq": 9, "type": "job.succeeded", "at": "t", "session_id": SID, "job_id": "job2",
     "payload": {"next": "completed"}},
]


def _json(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    return httpx.Response(status, json=payload, headers=headers or {})


def _problem(status: int, code: str | None) -> httpx.Response:
    body: dict[str, Any] = {
        "title": "错误", "status": status, "detail": "假服务", "instance": "/x",
    }
    if code is not None:
        body["code"] = code
    return httpx.Response(status, json=body,
                          headers={"content-type": "application/problem+json"})


class FakeServer:
    """按驱动器走的路径复刻一台最小状态机；并发 advance 用锁 + 延迟模拟临界区。"""

    def __init__(self, sessions_root: Path, *, fail_create: bool = False) -> None:
        self.sessions_root = sessions_root
        self.fail_create = fail_create
        self.status = "created"
        self.session_id = SID
        self.jobs: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.create_key: str | None = None
        self.job_keys: dict[str, dict[str, Any]] = {}
        self.resolve_keys: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    # -- 资源体 ---------------------------------------------------------
    def detail(self) -> dict[str, Any]:
        return {
            "session_id": SID, "status": self.status, "legacy": False,
            "revision": 3, "created_at": "t", "updated_at": "t",
            "plot": "雨夜旧信", "mode": "base", "variant": "T2VA",
            "brief": {"mode": "base", "variant": "T2VA", "director_mode": "",
                      "duration": 4.0, "style": "写实", "language": "Chinese",
                      "plot": "雨夜旧信", "clarifications": "", "draft": "",
                      "canvases": {}, "video_output": None, "refs": []},
            "last_event_seq": 9,
            "current_job": next(
                (job for job in reversed(self.jobs) if job["status"] == "running"), None),
            "jobs": self.jobs,
            "action_items": self.items,
            "artifacts": [self.artifact()],
        }

    @staticmethod
    def artifact() -> dict[str, Any]:
        return {"artifact_id": f"{SID}/video-prompt.md", "kind": "video_prompt",
                "name": "video-prompt.md", "bytes": len(PROMPT.encode("utf-8")),
                "created_at": "t"}

    # -- 命令 -----------------------------------------------------------
    def advance(self, key: str | None) -> httpx.Response:
        with self.lock:
            if key and key in self.job_keys:
                return _json(self.job_keys[key], 202,
                             {"Idempotency-Replayed": "true"})
            if self.status == "running":
                return _problem(409, "session_busy")
            if self.status != "created":
                return _problem(409, "action_item_pending")
            self.status = "running"
        # 锁外延迟：让第二条并发请求真的落在 running 期间，而不是等第一条收尾。
        time.sleep(0.15)
        with self.lock:
            job = {"job_id": "job1", "kind": "advance", "status": "succeeded",
                   "started_at": "t", "finished_at": "t", "error_code": None,
                   "cancelled_at_node": None, "cancel_requested": False}
            self.jobs.append(job)
            self.items.append({"item_id": "item1", "kind": "stage1_review",
                               "status": "pending", "created_at": "t",
                               "resolved_at": "", "decision": None})
            self.status = "awaiting_human"
            if key:
                self.job_keys[key] = job
        return _json({**job, "status": "running", "finished_at": ""}, 202)

    def resolve(self, key: str | None) -> httpx.Response:
        with self.lock:
            if key and key in self.resolve_keys:
                return _json(self.resolve_keys[key], 202,
                             {"Idempotency-Replayed": "true"})
            if self.status != "awaiting_human":
                return _problem(409, "action_item_pending")
            self.items[0].update({"status": "resolved", "decision": "approve",
                                  "resolved_at": "t"})
            job = {"job_id": "job2", "kind": "advance", "status": "succeeded",
                   "started_at": "t", "finished_at": "t", "error_code": None,
                   "cancelled_at_node": None, "cancel_requested": False}
            self.jobs.append(job)
            self.status = "completed"
            if key:
                self.resolve_keys[key] = job
        return _json({**job, "status": "running", "finished_at": ""}, 202)

    # -- transport ------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        method = request.method
        key = request.headers.get("Idempotency-Key")
        if path == "/api/v1/health" and method == "GET":
            return _json({"status": "ok", "service": "mini", "version": "0.0.1"})
        if path == "/api/v1/sessions" and method == "POST":
            if self.fail_create:
                return _problem(500, None)
            body = json.loads(request.content or b"{}")
            if not body:
                return _problem(422, None)
            with self.lock:
                if key and key == self.create_key:
                    return _json(self.detail(), 200,
                                 {"Idempotency-Replayed": "true"})
                self.create_key = key
            return _json(self.detail(), 201)
        if path == "/api/v1/sessions" and method == "GET":
            summary = {key: self.detail()[key] for key in
                       ("session_id", "status", "legacy", "revision",
                        "created_at", "updated_at", "plot", "mode", "variant")}
            return _json({"items": [summary], "next_cursor": None})
        if path.startswith("/api/v1/artifacts/") and path.endswith("/content"):
            return httpx.Response(200, text=PROMPT,
                                  headers={"content-type": "text/plain; charset=utf-8"})
        if path.startswith("/api/v1/sessions/"):
            rest = path[len("/api/v1/sessions/"):]
            if "不存在" in rest:
                return _problem(404, "session_not_found")
            if rest.endswith("/jobs") and method == "POST":
                return self.advance(key)
            if rest.endswith("/action-items") and method == "GET":
                return _json(self.items)
            if rest.endswith("/resolve") and method == "POST":
                return self.resolve(key)
            if rest.endswith("/artifacts") and method == "GET":
                return _json([self.artifact()])
            if rest.endswith("/events") and method == "GET":
                frames = "".join(
                    f"id: {event['seq']}\nevent: {event['type']}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    for event in EVENTS[-2:])
                return httpx.Response(200, text=frames,
                                      headers={"content-type": "text/event-stream"})
            if method == "GET":
                return _json(self.detail())
        return _problem(404, "not_found")


def _prepare(tmp_path: Path, *, fail_create: bool = False) -> tuple[FakeServer, Any]:
    sessions_root = tmp_path / "sessions"
    directory = sessions_root / Path(SID)
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in EVENTS),
        encoding="utf-8")
    (directory / "session-state.json").write_text(json.dumps({
        "schema_version": "workbench.v1", "session_id": SID,
        "revision": 3, "last_event_seq": 9,
    }), encoding="utf-8")
    server = FakeServer(sessions_root, fail_create=fail_create)
    settings = accept_d2.Settings(
        base_url="http://127.0.0.1:8000", timeout=5.0, out_dir=tmp_path / "out",
        plot="雨夜旧信", duration=4.0, sessions_root=sessions_root,
        poll_seconds=0.01, run_id="test")
    client = httpx.Client(base_url=settings.base_url,
                          transport=httpx.MockTransport(server.handle))
    return server, (client, settings)


def test_acceptance_writes_report_and_ticks_automated_acs(tmp_path):
    _server, (client, settings) = _prepare(tmp_path)
    assert accept_d2.run_acceptance(client, settings) == 0

    report = json.loads((settings.out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["session_id"] == SID
    assert report["events"]["total"] == len(EVENTS)
    assert report["events"]["seq_range"] == [1, len(EVENTS)]
    checks = {check["ac"]: check for check in report["checks"]}
    for ac in ("D2-AC1", "D2-AC2", "D2-AC3", "D2-AC5", "D2-AC6",
               "D2-AC7", "D2-AC11", "D2-AC13", "D2-AC18", "D2-AC19"):
        assert checks[ac]["ok"] is True, (ac, checks[ac])
    # 不可自动核对的 AC 必须是「需人工/测试证据」，不许冒充通过。
    for ac in ("D2-AC10", "D2-AC12"):
        assert checks[ac]["ok"] is None, (ac, checks[ac])
    assert any(check["ac"] == "D2-AC4" and check["ok"] is True
               for check in report["checks"])

    assert (settings.out_dir / "video-prompt.md").read_text(encoding="utf-8") == PROMPT
    assert (settings.out_dir / "events.jsonl").is_file()
    markdown = (settings.out_dir / "report.md").read_text(encoding="utf-8")
    assert "D2-AC1" in markdown and "✅" in markdown and "⏳" in markdown


def test_acceptance_fails_fast_and_reports_failed_step(tmp_path):
    _server, (client, settings) = _prepare(tmp_path, fail_create=True)
    assert accept_d2.run_acceptance(client, settings) == 1

    report = json.loads((settings.out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["failure"]["step"] == "建会话"
    assert "201" in report["failure"]["detail"] and "500" in report["failure"]["detail"]
    assert [step["step"] for step in report["steps"]][-1] == "建会话"
    markdown = (settings.out_dir / "report.md").read_text(encoding="utf-8")
    assert "FAIL" in markdown
    # 流程没走到 completed 时 AC19 必须显式打叉，不能从表格里消失。
    assert "D2-AC19" in markdown and "❌" in markdown


def test_event_seq_problems_detects_gaps_and_duplicates():
    def events(*seqs: int) -> list[dict[str, Any]]:
        return [{"seq": seq} for seq in seqs]

    assert accept_d2.event_seq_problems(events(1, 2, 3)) == []
    assert accept_d2.event_seq_problems(events(1, 3)) == ["第 2 条 seq=3，期望 2"]
    assert accept_d2.event_seq_problems(events(1, 1)) == ["第 2 条 seq=1，期望 2"]
    assert accept_d2.event_seq_problems([{"seq": "x"}]) == ["第 1 条的 seq 不是整数：'x'"]


def test_is_loopback_only_accepts_local_hosts():
    assert accept_d2.is_loopback("http://127.0.0.1:8000")
    assert accept_d2.is_loopback("http://localhost:8000")
    assert not accept_d2.is_loopback("http://192.168.1.10:8000")
