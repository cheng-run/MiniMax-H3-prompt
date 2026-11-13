"""D2-4（#218）：事件流与 SSE 的对外面（D2-AC10 / AC11）。

两头对账：**事件文件**证明「发生了什么、序号是否单调」，**HTTP 响应**证明客户端拿到
的帧、补发与缺口信号。SSE 是长连接，用例一律读到需要的帧就断开，不等待流自然结束；
读取放在带超时的线程里，实现坏掉时以「没等到帧」收场，而不是挂死全量。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.api.routes import sessions as sessions_route
from minimax_h3_prompt.errors import ModelOutputError
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.workbench_store import (
    EVENTS_FILENAME,
    EventDraft,
    EventLog,
)
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(roundtable_max_rounds=1, common_sense_qa=False)


def _brief_body(plot: str = "雨夜旧信") -> dict:
    return {
        "mode": "base",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": plot,
    }


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    """临时会话根 + 确定性桩模型；节点落盘也改到临时目录，不碰仓库 output/。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


def _create_session(client: TestClient, plot: str = "雨夜旧信") -> dict:
    resp = client.post("/api/v1/sessions", json=_brief_body(plot))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _events_url(session_id: str) -> str:
    return f"/api/v1/sessions/{session_id}/events"


def _events(root: Path, session_id: str) -> list[dict[str, Any]]:
    path = root / Path(session_id) / EVENTS_FILENAME
    if not path.is_file():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wait_job(client: TestClient, session_id: str, job_id: str,
              timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(
            f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


# ---------------------------------------------------------------------------
# SSE 读取工具
# ---------------------------------------------------------------------------

Until = Callable[[list[dict], list[str]], bool]


def _read_stream(client: TestClient, url: str, *, headers: dict[str, str] | None,
                 until: Until) -> tuple[list[dict], list[str], dict[str, str], int]:
    frames: list[dict] = []
    comments: list[str] = []
    with client.stream("GET", url, headers=headers or {}) as response:
        response_headers = dict(response.headers)
        status = response.status_code
        if status != 200:
            return frames, comments, response_headers, status
        current: dict[str, str] = {}
        for raw in response.iter_lines():
            line = raw.rstrip("\r")
            if not line:
                if current:
                    frames.append(current)
                    current = {}
                if until(frames, comments):
                    break
                continue
            if line.startswith(":"):
                comments.append(line)
                if until(frames, comments):
                    break
                continue
            key, _, value = line.partition(":")
            current[key.strip()] = value.lstrip()
        else:
            if current:
                frames.append(current)
    return frames, comments, response_headers, status


def _start_stream(client: TestClient, url: str, *,
                  headers: dict[str, str] | None = None, until: Until,
                  timeout: float = 20.0) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = _read_stream(client, url, headers=headers, until=until)
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, box


def _collect(client: TestClient, url: str, *, headers: dict[str, str] | None = None,
             until: Until, timeout: float = 20.0) -> tuple[list[dict], list[str], dict]:
    thread, box = _start_stream(client, url, headers=headers, until=until,
                                timeout=timeout)
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"SSE 没有在 {timeout}s 内给出期望的帧：{url}")
    if "error" in box:
        raise box["error"]
    frames, comments, headers_out, status = box["result"]
    assert status == 200, f"SSE 返回了 {status}：{frames}"
    return frames, comments, headers_out


def _enough(count: int) -> Until:
    return lambda frames, _comments: len(frames) >= count


# ---------------------------------------------------------------------------
# 建会话与任务生命周期（事件接线）
# ---------------------------------------------------------------------------

def test_create_emits_session_created(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)

    events = _events(root, session["session_id"])
    assert len(events) == 1
    assert events[0]["seq"] == 1
    assert events[0]["type"] == "session.created"
    assert events[0]["session_id"] == session["session_id"]
    assert events[0]["payload"] == {"status": "created"}
    assert session["last_event_seq"] == 1


def test_replayed_create_does_not_append_a_second_event(workbench):
    app, _model, root = workbench
    headers = {"Idempotency-Key": "create-1"}
    with workbench_client(app) as client:
        first = client.post("/api/v1/sessions", json=_brief_body(), headers=headers)
        second = client.post("/api/v1/sessions", json=_brief_body(), headers=headers)

    assert second.status_code == 200
    assert len(_events(root, first.json()["session_id"])) == 1


def test_stage1_job_emits_the_documented_events(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        _wait_job(client, session["session_id"], started["job_id"])
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()

    events = _events(root, session["session_id"])
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    types = [event["type"] for event in events]
    assert types[0] == "session.created"
    assert types[1] == "job.started"
    assert types[-1] == "action_item.created"
    assert events[1]["payload"] == {"kind": "advance"}
    assert events[1]["job_id"] == started["job_id"]

    for event in events[2:]:
        assert event["job_id"] == started["job_id"], event

    progress = [event for event in events if event["type"] == "job.progress"]
    assert {event["payload"]["stage"] for event in progress} == {"stage1"}
    assert {event["payload"]["status"] for event in progress} == {"started", "done"}
    started_nodes = [event["payload"]["node"] for event in progress
                     if event["payload"]["status"] == "started"]
    done_nodes = [event["payload"]["node"] for event in progress
                  if event["payload"]["status"] == "done"]
    # 节点级开始/结束成对出现：链上每个节点一次 started、一次 done。
    assert started_nodes == done_nodes
    assert "producer" in started_nodes and "parallel_sound" in started_nodes
    # elapsed_ms 是可选的：没有产出的节点（基础路线的 parallel_image_prompts）不回调，
    # 只能靠「图前进」收尾，没有起点可算耗时；有它的必须是非负整数。
    with_elapsed = [event["payload"]["elapsed_ms"] for event in progress
                    if "elapsed_ms" in event["payload"]]
    assert with_elapsed
    assert all(type(value) is int and value >= 0 for value in with_elapsed)

    succeeded = next(event for event in events if event["type"] == "job.succeeded")
    assert succeeded["payload"] == {"next": "awaiting_human"}

    created = next(event for event in events if event["type"] == "action_item.created")
    pending = [item for item in detail["action_items"] if item["status"] == "pending"]
    assert created["payload"] == {"item_id": pending[0]["item_id"],
                                  "kind": "stage1_review"}
    # 事件流与快照可对账（D2-AC11）：最后一条事件的号就是快照的 last_event_seq。
    assert detail["last_event_seq"] == events[-1]["seq"]


def test_cancelled_job_emits_the_node_it_stopped_after(workbench):
    app, model, root = workbench
    model.gate_role = "producer"
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        assert model.entered.wait(10)
        client.post(f"/api/v1/sessions/{session['session_id']}/jobs/"
                    f"{started['job_id']}/cancel")
        model.release.set()
        _wait_job(client, session["session_id"], started["job_id"])

    cancelled = [event for event in _events(root, session["session_id"])
                 if event["type"] == "job.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["payload"] == {"at_node": "producer"}
    assert cancelled[0]["job_id"] == started["job_id"]


def test_failed_job_emits_code_and_detail(workbench, monkeypatch):
    app, _model, root = workbench

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ModelOutputError("分镜节点吐了坏 JSON")

    monkeypatch.setattr(pipeline, "run_stage1", _boom)
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        _wait_job(client, session["session_id"], started["job_id"])

    failed = [event for event in _events(root, session["session_id"])
              if event["type"] == "job.failed"]
    assert len(failed) == 1
    assert failed[0]["payload"] == {"code": "MODEL_OUTPUT",
                                    "detail": "分镜节点吐了坏 JSON"}


def _make_running_session(root: Path, session_id: str, *, job_id: str,
                          pid: int = 999999) -> None:
    """模拟「上一进程推进到一半被强杀」：running 状态 + 在跑标记。"""
    path = root / Path(session_id) / SESSION_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "running"
    raw["stage_state"] = {**raw.get("stage_state", {}),
                          "production_plan": "中断前的制作计划",
                          "_progress": {"last_completed_node": "producer"}}
    raw["jobs"] = [{
        "job_id": job_id, "kind": "advance", "status": "running",
        "started_at": "2026-11-10T00:00:00+00:00", "finished_at": "",
        "error_code": None, "cancelled_at_node": None,
    }]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (path.parent / "active-job.json").write_text(json.dumps({
        "pid": pid, "job_id": job_id,
        "started_at": "2026-11-10T00:00:00+00:00", "node": "producer",
    }, ensure_ascii=False), encoding="utf-8")


def test_startup_reconciliation_emits_job_interrupted(workbench):
    _app, _model, root = workbench
    with workbench_client(create_app(sessions_root=root, runtime_config=RUNTIME)) as client:
        session = _create_session(client)
    _make_running_session(root, session["session_id"], job_id="dead-job")

    with workbench_client(create_app(sessions_root=root, runtime_config=RUNTIME)) as client:
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()

    assert detail["status"] == "interrupted"
    interrupted = [event for event in _events(root, session["session_id"])
                   if event["type"] == "job.interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["payload"] == {}
    assert interrupted[0]["job_id"] == "dead-job"


# ---------------------------------------------------------------------------
# SSE 帧、补发、缺口（D2-AC10）
# ---------------------------------------------------------------------------

def test_sse_frames_carry_id_type_and_json_data(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        frames, _comments, headers = _collect(
            client, _events_url(session["session_id"]), until=_enough(1))

    assert headers["content-type"].startswith("text/event-stream")
    frame = frames[0]
    assert set(frame) == {"id", "event", "data"}
    assert frame["id"] == "1"
    assert frame["event"] == "session.created"
    assert json.loads(frame["data"]) == _events(root, session["session_id"])[0]


def test_sse_replays_only_events_after_last_event_id(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        _wait_job(client, session["session_id"], started["job_id"])
        total = len(_events(root, session["session_id"]))

        frames, _comments, _headers = _collect(
            client, _events_url(session["session_id"]), headers={"Last-Event-ID": "3"},
            until=_enough(total - 3))

    ids = [int(frame["id"]) for frame in frames]
    assert ids == list(range(4, total + 1))


def test_sse_accepts_last_event_id_as_a_query_parameter(workbench):
    """浏览器 ``EventSource`` 不能自定义请求头，查询参数是重连时的第二选择。"""
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        frames, _comments, _headers = _collect(
            client, _events_url(session["session_id"]) + "?last_event_id=0",
            until=_enough(1))

    assert frames[0]["id"] == "1"


def test_bad_last_event_id_is_a_problem_422(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        # 请求头只能是 ASCII，非法值就用字母（中文在客户端编码阶段就会炸，测不到服务端）。
        resp = client.get(_events_url(session["session_id"]),
                          headers={"Last-Event-ID": "abc"})

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_sse_signals_resync_when_last_event_id_is_beyond_the_tail(workbench):
    """事件文件被清掉/换了机器：补不上缺口必须给前端一个明确信号（spec §8）。"""
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        frames, comments, headers = _collect(
            client, _events_url(session["session_id"]),
            headers={"Last-Event-ID": "99"},
            until=lambda _frames, comments: bool(comments))

    assert headers["x-resync-required"] == "true"
    assert any("resync" in comment for comment in comments), comments
    # 序号已经在尾号之后：没有「更大的事件」可补发，客户端只能重取快照。
    assert frames == []


def test_sse_heartbeat_is_a_comment_line(workbench, monkeypatch):
    monkeypatch.setattr(sessions_route, "SSE_HEARTBEAT_SECONDS", 0.05)
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        _frames, comments, _headers = _collect(
            client, _events_url(session["session_id"]),
            until=lambda _frames, comments: bool(comments))

    assert comments[0].startswith(":"), comments


def test_two_subscribers_of_the_same_session_both_get_every_event(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        _wait_job(client, session["session_id"], started["job_id"])
        total = len(_events(root, session["session_id"]))

        threads = [_start_stream(client, _events_url(session["session_id"]),
                                 until=_enough(total))
                   for _ in range(2)]
        for thread, _box in threads:
            thread.join(20)
        boxes = [box for _thread, box in threads]

    for box in boxes:
        assert "error" not in box, box
        frames, _comments, _headers, _status = box["result"]
        assert [int(frame["id"]) for frame in frames] == list(range(1, total + 1))


def test_sse_streams_while_a_long_job_is_running(workbench, monkeypatch):
    """§6：长任务期间事件循环不被拖住——任务卡在模型调用里，SSE 仍要出帧与心跳。"""
    monkeypatch.setattr(sessions_route, "SSE_HEARTBEAT_SECONDS", 0.05)
    app, model, _root = workbench
    model.gate_role = "producer"
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        assert model.entered.wait(10)

        frames, comments, _headers = _collect(
            client, _events_url(session["session_id"]),
            until=lambda frames, comments: (
                any(frame["event"] == "job.started" for frame in frames)
                and bool(comments)))
        detail = client.get(f"/api/v1/sessions/{session['session_id']}")

        assert any(frame["event"] == "job.started" for frame in frames)
        assert comments
        assert detail.status_code == 200
        assert detail.json()["status"] == "running"
        model.release.set()
        _wait_job(client, session["session_id"], started["job_id"])


def test_events_of_an_unknown_session_is_problem_404(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        resp = client.get(_events_url("不存在/GEN001"))

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "session_not_found"


def test_contract_declares_the_events_endpoint(workbench):
    app, _model, _root = workbench
    paths = app.openapi()["paths"]

    path = "/api/v1/sessions/{session_id}/events"
    assert path in paths and "get" in paths[path]


# ---------------------------------------------------------------------------
# 先事件、后快照：崩溃窗口的重放（spec §5.3）
# ---------------------------------------------------------------------------

def _append_without_snapshot(root: Path, session_id: str) -> None:
    """模拟「事件已落盘、快照还没写」就崩了。"""
    log = EventLog(root / Path(session_id), session_id)
    log.append([
        EventDraft("job.started", {"kind": "advance"}, job_id="j1"),
        EventDraft("job.succeeded", {"next": "awaiting_human"}, job_id="j1"),
        EventDraft("action_item.created",
                   {"item_id": "i1", "kind": "stage1_review"}, job_id="j1"),
    ])


def test_stale_snapshot_is_repaired_from_the_event_log(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
    _append_without_snapshot(root, session["session_id"])

    with workbench_client(app) as client:
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()

    assert detail["status"] == "awaiting_human"
    assert [item["item_id"] for item in detail["action_items"]] == ["i1"]
    assert detail["jobs"][0]["status"] == "succeeded"
    assert detail["last_event_seq"] == 4
    on_disk = json.loads(
        (root / Path(session["session_id"]) / SESSION_FILENAME).read_text(encoding="utf-8"))
    # 补齐是**落盘**的，不只在这次响应里：否则每读一次都要重放一遍。
    assert on_disk["last_event_seq"] == 4
    assert on_disk["status"] == "awaiting_human"


def test_replaying_the_same_events_twice_does_not_duplicate_anything(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
    directory = root / Path(session["session_id"])
    _append_without_snapshot(root, session["session_id"])
    (directory / "video-prompt.md").write_text("提示词正文", encoding="utf-8")
    EventLog(directory, session["session_id"]).append([
        EventDraft("artifact.created",
                   {"artifact_id": f"{session['session_id']}/video-prompt.md",
                    "kind": "video_prompt", "name": "video-prompt.md"})])

    with workbench_client(app) as client:
        first = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        second = client.get(f"/api/v1/sessions/{session['session_id']}").json()

    for detail in (first, second):
        assert [item["item_id"] for item in detail["action_items"]] == ["i1"]
        assert [item["name"] for item in detail["artifacts"]] == ["video-prompt.md"]
        assert detail["last_event_seq"] == 5
    # 第二次读没有可重放的事件：不白抬 revision（否则每次 GET 都在写盘）。
    assert first["revision"] == second["revision"]


def test_action_item_payload_survives_snapshot_replay(workbench):
    """段号/件号/被拒原因随事件重放回待办——快照没跟上也不丢（D7-1 AC1）。

    这是 ref 素材循环与 director 段待办的地基：崩溃补齐后要说得清「在等第几段视频 /
    第几件素材」，而不是一句「在等某件事」。
    """
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
    EventLog(root / Path(session["session_id"]), session["session_id"]).append([
        EventDraft("job.started", {"kind": "segment"}, job_id="j1"),
        EventDraft("job.succeeded", {"next": "awaiting_human"}, job_id="j1"),
        EventDraft("action_item.created",
                   {"item_id": "i1", "kind": "segment_video",
                    "segment_index": 2, "reason": "尾帧未达到本段末态"},
                   job_id="j1"),
        EventDraft("action_item.created",
                   {"item_id": "i2", "kind": "asset_image", "asset_index": 3}),
    ])

    with workbench_client(app) as client:
        detail = client.get(
            f"/api/v1/sessions/{session['session_id']}").json()

    items = {item["item_id"]: item for item in detail["action_items"]}
    assert items["i1"]["segment_index"] == 2
    assert items["i1"]["reason"] == "尾帧未达到本段末态"
    assert items["i2"]["asset_index"] == 3
    # 事件里没带的字段保持缺省，不编一个「第几件」出来。
    assert items["i1"]["asset_index"] is None
    assert items["i2"]["segment_index"] is None
    assert items["i2"]["reason"] == ""


def test_old_two_field_action_items_still_replay(workbench):
    """已落盘的旧会话事件只有 item_id + kind，重放仍给出可用待办（AC4）。"""
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
    EventLog(root / Path(session["session_id"]), session["session_id"]).append([
        EventDraft("job.started", {"kind": "segment"}, job_id="j1"),
        EventDraft("job.succeeded", {"next": "awaiting_human"}, job_id="j1"),
        EventDraft("action_item.created",
                   {"item_id": "i1", "kind": "segment_video"}, job_id="j1"),
    ])

    with workbench_client(app) as client:
        detail = client.get(
            f"/api/v1/sessions/{session['session_id']}").json()

    assert detail["status"] == "awaiting_human"
    item = [entry for entry in detail["action_items"] if entry["item_id"] == "i1"][0]
    assert item["status"] == "pending"
    assert item["segment_index"] is None
    assert item["asset_index"] is None
    assert item["reason"] == ""
