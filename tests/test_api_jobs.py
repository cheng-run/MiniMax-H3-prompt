"""D2-2（#217）：执行任务——起任务、单写入者、取消、启动对账、确定性桩全链路。

HTTP 形状与状态判据是这张票的验收面（D2-AC3 / AC4 / AC5 / AC8 / AC9 / AC15）。
模型桩接在 ``model_factory.build_chat_model`` 缝上，图与节点都是真的；用例用
``model.calls`` 的角色计数证明节点真被跑过。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=1)


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


def _create_session(client, plot: str = "雨夜旧信") -> dict:
    resp = client.post("/api/v1/sessions", json=_brief_body(plot))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _job_url(session_id: str, job_id: str, suffix: str = "") -> str:
    return f"/api/v1/sessions/{session_id}/jobs/{job_id}{suffix}"


def _wait_job(client, session_id: str, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(_job_url(session_id, job_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


def _read_session(root: Path, session_id: str) -> dict:
    path = root / Path(session_id) / SESSION_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 阶段 1 跑到人工闸门（D2-AC5 / AC15）
# ---------------------------------------------------------------------------

def test_advance_runs_real_stage1_to_the_human_gate(workbench):
    app, model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)

        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs")

        assert started.status_code == 202, started.text
        job = started.json()
        assert job["kind"] == "advance"
        assert job["status"] == "running"
        done = _wait_job(client, session["session_id"], job["job_id"])

        assert done["status"] == "succeeded"
        assert done["finished_at"]
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        assert detail["status"] == "awaiting_human"
        pending = [item for item in detail["action_items"]
                   if item["kind"] == "stage1_review" and item["status"] == "pending"]
        assert len(pending) == 1
        assert detail["current_job"] is None

        # 调用的角色是「节点真被执行过」的物证：不是跳过，也不是假管线。
        for role in ("producer", "director", "screenwriter", "art_director",
                     "storyboard", "cinematographer", "sound_designer"):
            assert role in model.calls, f"{role} 节点没有调用模型：{model.calls}"
        assert len(model.calls) >= 10

        state = _read_session(root, session["session_id"])
        stage_state = state["stage_state"]
        for key in ("production_plan", "script", "character_design", "art_design",
                    "shot_table", "visual_design", "sound_design"):
            assert stage_state.get(key), f"阶段 1 的 {key} 没有落进会话文件"
        assert state["status"] == "awaiting_human"
        # 作业摘要写进会话文件，最近 20 条的口径由 store 保证。
        assert [item["job_id"] for item in state["jobs"]] == [job["job_id"]]


# ---------------------------------------------------------------------------
# 单写入者（D2-AC3）
# ---------------------------------------------------------------------------

def test_concurrent_advance_is_rejected_with_session_busy(workbench):
    app, model, _root = workbench
    model.gate_role = "producer"
    with workbench_client(app) as client:
        session = _create_session(client)
        first = client.post(f"/api/v1/sessions/{session['session_id']}/jobs")
        assert first.status_code == 202
        assert model.entered.wait(10), "第一次推进没有进入模型调用"

        second = client.post(
            f"/api/v1/sessions/{session['session_id']}/jobs",
            headers={"Idempotency-Key": "another-key"})

        assert second.status_code == 409
        assert second.headers["content-type"].startswith("application/problem+json")
        assert second.json()["code"] == "session_busy"
        model.release.set()
        _wait_job(client, session["session_id"], first.json()["job_id"])


# ---------------------------------------------------------------------------
# 幂等（D2-AC4）
# ---------------------------------------------------------------------------

def test_same_idempotency_key_replays_the_first_job_without_rerunning(workbench):
    app, model, root = workbench
    model.gate_role = "producer"
    headers = {"Idempotency-Key": "advance-1"}
    with workbench_client(app) as client:
        session = _create_session(client)
        first = client.post(f"/api/v1/sessions/{session['session_id']}/jobs", headers=headers)
        assert first.status_code == 202
        assert model.entered.wait(10)

        second = client.post(f"/api/v1/sessions/{session['session_id']}/jobs", headers=headers)

        assert second.status_code == 202
        assert second.headers["idempotency-replayed"] == "true"
        assert second.json()["job_id"] == first.json()["job_id"]
        model.release.set()
        _wait_job(client, session["session_id"], first.json()["job_id"])

        state = _read_session(root, session["session_id"])
        assert [item["job_id"] for item in state["jobs"]] == [first.json()["job_id"]]
        assert model.calls.count("producer") == 1


# ---------------------------------------------------------------------------
# 协作式取消 + 续跑（D2-AC8）
# ---------------------------------------------------------------------------

def test_cancel_stops_after_the_current_node_and_advance_resumes(workbench):
    app, model, root = workbench
    model.gate_role = "producer"
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        assert model.entered.wait(10)

        cancelled = client.post(_job_url(session["session_id"], started["job_id"], "/cancel"))

        assert cancelled.status_code == 202
        assert cancelled.json()["cancel_requested"] is True
        model.release.set()
        done = _wait_job(client, session["session_id"], started["job_id"])
        assert done["status"] == "cancelled"
        assert done["cancelled_at_node"] == "producer"
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        assert detail["status"] == "cancelled"

        state = _read_session(root, session["session_id"])
        assert state["stage_state"]["production_plan"]
        assert state["stage_state"]["_progress"]["last_completed_node"] == "producer"

        # 取消后可以再推进；起点由进度决定，已完成的 producer 不重跑。
        resumed = client.post(f"/api/v1/sessions/{session['session_id']}/jobs")
        assert resumed.status_code == 202
        again = _wait_job(client, session["session_id"], resumed.json()["job_id"])
        assert again["status"] == "succeeded"
        assert model.calls.count("producer") == 1
        assert model.calls.count("director") == 1
        assert client.get(
            f"/api/v1/sessions/{session['session_id']}").json()["status"] == "awaiting_human"


def test_cancel_on_a_finished_job_is_a_conflict(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        _wait_job(client, session["session_id"], started["job_id"])

        resp = client.post(_job_url(session["session_id"], started["job_id"], "/cancel"))

        assert resp.status_code == 409
        assert resp.json()["code"] == "job_not_running"


def test_unknown_job_is_problem_404(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)

        resp = client.get(_job_url(session["session_id"], "nope"))

        assert resp.status_code == 404
        assert resp.json()["code"] == "job_not_found"


# ---------------------------------------------------------------------------
# 启动对账（D2-AC9）
# ---------------------------------------------------------------------------

def _make_running_session(root: Path, session_id: str, *, job_id: str,
                          pid: int = 999999) -> None:
    """模拟「上一进程推进到 producer 后被强杀」：running 状态 + 在跑标记。"""
    path = root / Path(session_id) / SESSION_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "running"
    raw["stage_state"] = {
        **raw.get("stage_state", {}),
        "production_plan": "中断前已完成的制作计划",
        "_progress": {"last_completed_node": "producer"},
    }
    raw["jobs"] = [{
        "job_id": job_id, "kind": "advance", "status": "running",
        "started_at": "2026-11-09T00:00:00+00:00", "finished_at": "",
        "error_code": None, "cancelled_at_node": None,
    }]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (path.parent / "active-job.json").write_text(json.dumps({
        "pid": pid, "job_id": job_id,
        "started_at": "2026-11-09T00:00:00+00:00", "node": "producer",
    }, ensure_ascii=False), encoding="utf-8")


def test_startup_reconciliation_marks_interrupted_and_advance_resumes(workbench):
    _app, model, root = workbench
    # 先建会话目录：用一次性 app（不跑 lifespan 也行，建会话只走存储层）。
    bootstrap = create_app(sessions_root=root, runtime_config=RUNTIME)
    with workbench_client(bootstrap) as client:
        session = _create_session(client)
    _make_running_session(root, session["session_id"], job_id="dead-job")
    model.calls.clear()

    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    with workbench_client(app) as client:
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()

        assert detail["status"] == "interrupted"
        assert detail["jobs"][0]["status"] == "interrupted"
        assert not (root / Path(session["session_id"]) / "active-job.json").exists()

        resumed = client.post(f"/api/v1/sessions/{session['session_id']}/jobs")

        assert resumed.status_code == 202
        _wait_job(client, session["session_id"], resumed.json()["job_id"])
        # 从断点续跑：producer 已完成，不再重跑；它留下的产物仍在。
        assert "producer" not in model.calls
        assert model.calls.count("director") == 1
        state = _read_session(root, session["session_id"])
        assert state["stage_state"]["production_plan"] == "中断前已完成的制作计划"
        assert state["status"] == "awaiting_human"


def test_reconcile_also_clears_a_stale_marker_of_this_process(workbench):
    """标记 pid 等于本进程时也必须对账（spec §6：不是本进程**或**进程已不存在）。

    启动时刻本进程不可能已有 job 在跑，所以「pid 是自己」只可能是上一次崩溃留下的
    陈旧标记；跳过它会让会话永远停在 running，再也发不出 advance。
    """
    _app, _model, root = workbench
    bootstrap = create_app(sessions_root=root, runtime_config=RUNTIME)
    with workbench_client(bootstrap) as client:
        session = _create_session(client)
    _make_running_session(root, session["session_id"], job_id="stale-job",
                          pid=os.getpid())

    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    with workbench_client(app) as client:
        detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()

        assert detail["status"] == "interrupted"
        assert detail["jobs"][0]["status"] == "interrupted"
        assert not (root / Path(session["session_id"]) / "active-job.json").exists()


# ---------------------------------------------------------------------------
# 长任务期间查询接口仍可响应
# ---------------------------------------------------------------------------

def test_queries_stay_responsive_while_the_job_runs(workbench):
    app, model, _root = workbench
    model.gate_role = "producer"
    with workbench_client(app) as client:
        session = _create_session(client)
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs").json()
        assert model.entered.wait(10)

        began = time.monotonic()
        health = client.get("/api/v1/health")
        detail = client.get(f"/api/v1/sessions/{session['session_id']}")
        elapsed = time.monotonic() - began

        assert health.status_code == 200
        assert detail.status_code == 200
        assert detail.json()["status"] == "running"
        assert elapsed < 2.0, f"长任务运行期间查询被拖住：{elapsed:.2f}s"
        model.release.set()
        _wait_job(client, session["session_id"], started["job_id"])


def test_advance_after_stage2_interruption_runs_stage2_not_stage1(workbench):
    """中断在阶段 2 的会话再 advance：必须续跑阶段 2，不能重开人工闸门。

    #221 真实跑实测：阶段 2 被网关卡住后强杀，重启再 advance 若一律从阶段 1 进入，
    链已耗尽 → 只会挂一条新的 stage1_review，阶段 2 永远走不到。
    """
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)
        sid = session["session_id"]
        job = client.post(f"/api/v1/sessions/{sid}/jobs").json()
        _wait_job(client, sid, job["job_id"])
        path = root / Path(sid) / SESSION_FILENAME
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 模拟「approve 之后、阶段 2 途中被强杀」：待办已决议、会话 interrupted。
        for entry in raw["action_items"]:
            entry["status"] = "resolved"
            entry["decision"] = "approve"
        raw["status"] = "interrupted"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        resumed = client.post(f"/api/v1/sessions/{sid}/jobs").json()
        done = _wait_job(client, sid, resumed["job_id"])

        assert done["status"] == "succeeded"
        detail = client.get(f"/api/v1/sessions/{sid}").json()
        assert detail["status"] == "completed", "中断在阶段 2 的 advance 不该回到人工闸门"
        assert (root / Path(sid) / "video-prompt.md").is_file()
