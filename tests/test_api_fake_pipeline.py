"""D4-1（#228）：确定性桩管线（D4-AC15 的前提）。

判据是**互斥**：开了桩就一个模型都不建；没开就走真实路径（确定性桩模型照常有调用）。
桩开关只认环境变量——契约里不许出现任何入口，否则它会变成生产可开的后门。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

ENV = "MINIMAX_FAKE_PIPELINE"

# 两张 1x1 PNG（内容不同，避免被素材去重成同一个）。
PNG_FIRST = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_LAST = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _brief_body(variant: str = "FL2VA") -> dict:
    return {
        "mode": "base",
        "variant": variant,
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
    }


def _wait_job(client: TestClient, session_id: str, job_id: str,
              timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(
            f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel(replies=dict(DEFAULT_REPLIES))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    return root, model


def _openapi_mentions_fake(app) -> bool:
    return "fake" in json.dumps(app.openapi(), ensure_ascii=False).lower()


def test_fake_pipeline_is_off_by_default(workbench, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    root, model = workbench

    app = create_app(sessions_root=root, runtime_config=RUNTIME)

    assert app.state.job_runner.fake_pipeline is False
    assert not _openapi_mentions_fake(app)


def test_fake_pipeline_runs_both_stages_without_building_any_model(
        workbench, monkeypatch):
    """开桩后 FL2VA 全闭环：阶段 1 出 4 个产物、交帧后阶段 2 出提示词，全程不建模型。"""
    monkeypatch.setenv(ENV, "1")
    root, _model = workbench

    def _forbidden():
        raise AssertionError("桩管线不许建真实模型")

    monkeypatch.setattr(model_factory, "build_chat_model", _forbidden)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)

    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body())
        assert session.status_code == 201, session.text
        session_id = session.json()["session_id"]
        started = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        gate = client.get(f"/api/v1/sessions/{session_id}").json()
        assert gate["status"] == "awaiting_human"
        assert {item["name"] for item in gate["artifacts"]} == {
            "script.md", "shot-table.md",
            "first-frame-prompt.md", "last-frame-prompt.md",
        }
        item = [entry for entry in gate["action_items"]
                if entry["status"] == "pending"][0]

        approved = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "approve"})
        assert approved.status_code == 202, approved.text
        # FL2VA 通过审查后不直接跑阶段 2：先挂帧提交待办。
        assert approved.json() is None
        frame_item = [entry for entry in client.get(
            f"/api/v1/sessions/{session_id}").json()["action_items"]
            if entry["status"] == "pending"][0]
        assert frame_item["kind"] == "frame_submission"

        assets = {}
        for role, data in (("first", PNG_FIRST), ("last", PNG_LAST)):
            uploaded = client.post(
                f"/api/v1/sessions/{session_id}/assets",
                files={"file": (f"{role}.png", data, "image/png")})
            assert uploaded.status_code == 201, uploaded.text
            assets[role] = uploaded.json()["asset_id"]
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/action-items/"
            f"{frame_item['item_id']}/resolve",
            json={"frames": assets})
        assert submitted.status_code == 202, submitted.text
        _wait_job(client, session_id, submitted.json()["job_id"])

        final = client.get(f"/api/v1/sessions/{session_id}").json()
        prompt = client.get(
            f"/api/v1/artifacts/{session_id}/video-prompt.md/content")

    assert final["status"] == "completed"
    assert prompt.status_code == 200 and "桩管线" in prompt.text
    # 桩读图读的是用户提交的帧位：首尾都交了，两条描述都在。
    state = json.loads(
        (root / Path(session_id) / SESSION_FILENAME).read_text(encoding="utf-8"))
    roles = {entry["role"] for entry in state["stage_state"]["fl2va_frame_descriptions"]}
    assert roles == {"first", "last"}


def test_fake_pipeline_revise_refreshes_the_artifacts(workbench, monkeypatch):
    """桩上的修订回路也要能把「新一稿」落到审查面（E2E 要跑 revise 一轮）。"""
    monkeypatch.setenv(ENV, "1")
    root, _model = workbench
    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: (_ for _ in ()).throw(AssertionError("桩管线不许建真实模型")))
    app = create_app(sessions_root=root, runtime_config=RUNTIME)

    with workbench_client(app) as client:
        session = client.post(
            "/api/v1/sessions", json=_brief_body("T2VA")).json()
        session_id = session["session_id"]
        started = client.post(f"/api/v1/sessions/{session_id}/jobs")
        _wait_job(client, session_id, started.json()["job_id"])
        before = (root / Path(session_id) / "script.md").read_text(encoding="utf-8")
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        item = [entry for entry in detail["action_items"]
                if entry["status"] == "pending"][0]

        resolved = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "revise",
                  "revisions": [{"layer": "剧情", "text": "把雨改成雨停后的清晨"}]})
        assert resolved.status_code == 202, resolved.text
        _wait_job(client, session_id, resolved.json()["job_id"])

        after = (root / Path(session_id) / "script.md").read_text(encoding="utf-8")
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert "第 2 稿" in after
    assert "雨停后的清晨" in after
    assert after != before
    assert final["status"] == "awaiting_human"


def test_environment_off_keeps_the_real_pipeline(workbench, monkeypatch):
    """互斥的另一半：没开桩时，真实阶段 1 照旧调用模型（不是悄悄走桩）。"""
    monkeypatch.delenv(ENV, raising=False)
    root, model = workbench
    app = create_app(sessions_root=root, runtime_config=RUNTIME)

    with workbench_client(app) as client:
        session = client.post(
            "/api/v1/sessions", json=_brief_body("T2VA")).json()
        started = client.post(f"/api/v1/sessions/{session['session_id']}/jobs")
        _wait_job(client, session["session_id"], started.json()["job_id"])

    assert model.calls, "真实路径没有调用任何模型"


def test_startup_log_announces_the_switch(workbench, monkeypatch, caplog):
    root, _model = workbench
    monkeypatch.setenv(ENV, "1")
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    with caplog.at_level(logging.WARNING):
        # 日志在 lifespan 启动时打（uvicorn 的日志配置到 serve 才生效）。
        with workbench_client(app):
            pass
    assert any(ENV in record.getMessage() for record in caplog.records)
