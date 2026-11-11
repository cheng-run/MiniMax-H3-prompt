"""D2-5（#219）：人工待办决议与修订回路的对外面（D2-AC6 / AC15）。

三件事在这里被钉住：**决议驱动真实阶段 2**、**修订跨轮累积并产生新待办**、
**同一 Idempotency-Key 不重复重跑**。错误面按 spec §7 的稳定错误码分类。
模型桩仍走 ``workbench_deterministic``——图、节点、步骤机都是真的，只换模型回复。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.workbench_store import EVENTS_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

# 阶段 2 也要跑：``max_qa_iterations=0`` 关掉质检精修轮，只验组装与落盘。
RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

# I2VA 的关键帧回复必须是合法 bundle：节点解析失败会走「转 JSON」重试并最终抛错。
# 主题「雨夜旧信」不命中地点词表，因此 scene_anchor 只需自洽。
FRAME_BUNDLE = {
    "prompt_node_id": "187",
    "scene_anchor": "雨夜老城街口，霓虹灯在积水里反光",
    "continuity_constraints": ["人物服装与道具在画面内保持一致"],
    "first": {
        "positive_prompt": (
            "雨夜老城街口，一位撑黑伞的成年女性身穿米色风衣走过积水，"
            "霓虹灯反光，中景，冷蓝主调"),
        "composition": "中景，人物居中偏左",
        "lighting": "冷蓝霓虹侧逆光",
    },
}


def _brief_body(plot: str = "雨夜旧信", variant: str = "I2VA") -> dict:
    return {
        "mode": "base",
        "variant": variant,
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": plot,
    }


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    """临时会话根 + 确定性桩模型；节点落盘也改到临时目录，不碰仓库 output/。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel(replies={
        **DEFAULT_REPLIES,
        "frame_prompt_engineer": json.dumps(FRAME_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


def _create_session(client: TestClient, **kwargs) -> dict:
    resp = client.post("/api/v1/sessions", json=_brief_body(**kwargs))
    assert resp.status_code == 201, resp.text
    return resp.json()


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


def _advance_to_gate(client: TestClient, **kwargs) -> tuple[dict, dict]:
    """建会话并推进到人工闸门，返回 ``(会话详情, 唯一那条待办)``。"""
    session = _create_session(client, **kwargs)
    resp = client.post(f"/api/v1/sessions/{session['session_id']}/jobs")
    assert resp.status_code == 202, resp.text
    _wait_job(client, session["session_id"], resp.json()["job_id"])
    detail = client.get(f"/api/v1/sessions/{session['session_id']}").json()
    assert detail["status"] == "awaiting_human"
    pending = [item for item in detail["action_items"] if item["status"] == "pending"]
    assert len(pending) == 1
    return detail, pending[0]


def _resolve(client: TestClient, session_id: str, item_id: str, body: dict,
             headers: dict[str, str] | None = None):
    return client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve",
        json=body, headers=headers or {})


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / EVENTS_FILENAME
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _state(root: Path, session_id: str) -> dict:
    path = root / Path(session_id) / SESSION_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 待办列表
# ---------------------------------------------------------------------------

def test_action_items_lists_the_pending_review(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)

        resp = client.get(
            f"/api/v1/sessions/{detail['session_id']}/action-items")

    assert resp.status_code == 200
    assert resp.json() == [item]
    assert item["kind"] == "stage1_review"
    assert item["status"] == "pending"
    assert item["decision"] is None


def test_action_items_of_an_unknown_session_is_problem_404(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        resp = client.get("/api/v1/sessions/不存在/GEN001/action-items")

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "session_not_found"


# ---------------------------------------------------------------------------
# approve：进入阶段 2 直到 completed（D2-AC6 后半）
# ---------------------------------------------------------------------------

def test_resolve_approve_runs_stage2_to_completed(workbench):
    app, model, root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]

        started = _resolve(client, session_id, item["item_id"], {"decision": "approve"})

        assert started.status_code == 202, started.text
        job = started.json()
        assert job["status"] == "running"
        done = _wait_job(client, session_id, job["job_id"])
        assert done["status"] == "succeeded"

        final = client.get(f"/api/v1/sessions/{session_id}").json()
        assert final["status"] == "completed"
        # 无首尾帧 → 按 D2 的既定边界自动降级，并把降级事实记在会话上。
        assert final["brief"]["variant"] == "T2VA"
        state = _state(root, session_id)
        assert state["variant_downgraded"] == "I2VA→T2VA"
        assert state["status"] == "completed"

        prompt_file = root / Path(session_id) / "video-prompt.md"
        assert prompt_file.is_file()
        assert prompt_file.read_text(encoding="utf-8").strip()

        resolved = [entry for entry in final["action_items"]
                    if entry["item_id"] == item["item_id"]][0]
        assert resolved["status"] == "resolved"
        assert resolved["decision"] == "approve"

        # 阶段 2 的节点真被执行过（确定性桩的角色计数是物证，不是跳过）。
        assert "unknown" in model.calls  # prompt_engineer / qa 的系统提示词没有登记首行

        types = [event["type"] for event in _events(root, session_id)]
        assert types.count("job.started") == 2  # 阶段 1 一次、阶段 2 一次
        assert "job.progress" in types
        assert "action_item.resolved" in types
        succeeded = [event for event in _events(root, session_id)
                     if event["type"] == "job.succeeded"][-1]
        assert succeeded["payload"]["next"] == "completed"


def test_stage2_writes_the_summary_artifacts_beside_the_prompt(workbench, monkeypatch):
    """D4-AC14：阶段 2 的交付三件套。

    摘要走 ``summary.summarize_prompt_zh``（向导同一条路）：模型调用换成固定返回值，
    这里验的是**阶段 2 真的产出了两件摘要并登记**，正文经 content 端点可读。
    """
    from minimax_h3_prompt import summary as summary_module
    from minimax_h3_prompt.summary import PromptSummary

    app, _model, _root = workbench
    calls: list[str] = []

    def fake_summary(prompt: str, _llm) -> PromptSummary:
        calls.append(prompt)
        return PromptSummary(
            overall="雨夜街口的收束：人物收伞回望。",
            shots=[{"shot": 1, "visual": "雨夜街口", "audio": "雨声"}],
        )

    monkeypatch.setattr(summary_module, "summarize_prompt_zh", fake_summary)
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]
        started = _resolve(client, session_id, item["item_id"], {"decision": "approve"})
        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        assert done["status"] == "succeeded"

        final = client.get(f"/api/v1/sessions/{session_id}").json()
        names = {entry["name"] for entry in final["artifacts"]}
        markdown = client.get(
            f"/api/v1/artifacts/{session_id}/summary-zh.md/content")
        payload = client.get(
            f"/api/v1/artifacts/{session_id}/summary-zh.json/content")

    # 摘要的输入是真实提示词，不是空串。
    assert calls and calls[0].strip()
    assert {"video-prompt.md", "summary-zh.md", "summary-zh.json"} <= names
    assert markdown.status_code == 200
    assert "雨夜街口的收束" in markdown.text
    assert payload.status_code == 200
    assert json.loads(payload.text) == {
        "overall": "雨夜街口的收束：人物收伞回望。",
        "shots": [{"shot": 1, "visual": "雨夜街口", "audio": "雨声"}],
    }


def test_stage2_survives_a_summary_failure(workbench, monkeypatch):
    """摘要失败只降级，不让阶段 2 的 job 失败（与向导同口径）。"""
    from minimax_h3_prompt import summary as summary_module

    app, _model, _root = workbench
    monkeypatch.setattr(
        summary_module, "summarize_prompt_zh", lambda _prompt, _llm: None)
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]
        started = _resolve(client, session_id, item["item_id"], {"decision": "approve"})
        done = _wait_job(client, session_id, started.json()["job_id"])

        final = client.get(f"/api/v1/sessions/{session_id}").json()
        prompt = client.get(
            f"/api/v1/artifacts/{session_id}/video-prompt.md/content")

    assert done["status"] == "succeeded"
    assert final["status"] == "completed"
    assert prompt.status_code == 200
    names = {entry["name"] for entry in final["artifacts"]}
    assert "video-prompt.md" in names
    assert not {"summary-zh.md", "summary-zh.json"} & names


def test_resolve_on_a_session_not_awaiting_human_is_conflict(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session = _create_session(client)

        resp = _resolve(client, session["session_id"], "whatever",
                        {"decision": "approve"})

    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "action_item_pending"


def test_resolve_unknown_item_is_problem_404(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, _item = _advance_to_gate(client)

        resp = _resolve(client, detail["session_id"], "nope", {"decision": "approve"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "action_item_not_found"


def test_resolve_item_of_another_session_is_problem_404(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        _first, foreign = _advance_to_gate(client)
        second, _item = _advance_to_gate(client, plot="另一个主题")

        resp = _resolve(client, second["session_id"], foreign["item_id"],
                        {"decision": "approve"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "action_item_not_found"


def test_resolving_an_already_resolved_item_is_conflict(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]
        # 走 revise：会话跑完会回到同一个闸门，旧待办保持已决议——这正是「拿旧 id
        # 再提交一次」真正会发生的场景（approve 之后会话已完成，先撞状态闸门）。
        started = _resolve(client, session_id, item["item_id"], {
            "decision": "revise",
            "revisions": [{"layer": "画面", "text": "把雨改成雪"}],
        })
        _wait_job(client, session_id, started.json()["job_id"])

        again = _resolve(client, session_id, item["item_id"], {"decision": "approve"})

    assert again.status_code == 409
    assert again.json()["code"] == "action_item_resolved"


# ---------------------------------------------------------------------------
# revise：旧项 resolved、新项 pending、修订累积（D2-AC6 前半）
# ---------------------------------------------------------------------------

def test_resolve_revise_creates_a_new_pending_item(workbench):
    app, model, root = workbench
    model.replies["frame_prompt_engineer"] = json.dumps(FRAME_BUNDLE, ensure_ascii=False)
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]

        # 中文层名（spec §7 的写法）也要被接受并归一。
        started = _resolve(client, session_id, item["item_id"], {
            "decision": "revise",
            "revisions": [{"layer": "画面", "text": "把雨改成雪"}],
        })

        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        assert done["status"] == "succeeded"

        final = client.get(f"/api/v1/sessions/{session_id}").json()
        assert final["status"] == "awaiting_human"
        old = [entry for entry in final["action_items"]
               if entry["item_id"] == item["item_id"]][0]
        assert old["status"] == "resolved"
        assert old["decision"] == "revise"
        pending = [entry for entry in final["action_items"]
                   if entry["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["item_id"] != item["item_id"]

        revisions = _state(root, session_id)["stage_state"]["user_revisions"]
        assert [(entry["round"], entry["layer"], entry["text"])
                for entry in revisions] == [(1, "frame", "把雨改成雪")]
        assert "applied" not in revisions[0]

        events = _events(root, session_id)
        created = [event["payload"]["item_id"] for event in events
                   if event["type"] == "action_item.created"]
        assert created == [item["item_id"], pending[0]["item_id"]]
        resolutions = [event for event in events
                       if event["type"] == "action_item.resolved"]
        assert resolutions[-1]["payload"] == {
            "item_id": item["item_id"], "decision": "revise",
            # D4 起决议事件带着当时的修订载荷：快照落后时要靠它补回历史。
            "revisions": [{"layer": "frame", "text": "把雨改成雪"}]}
        succeeded = [event for event in events
                     if event["type"] == "job.succeeded"][-1]
        assert succeeded["payload"]["next"] == "awaiting_human"


def test_resolved_item_echoes_the_revisions_it_was_resolved_with(workbench):
    """D4 spec §7：决议时收到什么回显什么——层次按内部四档归一，顺序按提交顺序。

    历史从**待办列表**重建，不 dump ``user_revisions``：本用例只读详情里的待办，
    不碰 state。
    """
    app, _model, root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]

        started = _resolve(client, session_id, item["item_id"], {
            "decision": "revise",
            "revisions": [
                {"layer": "画面", "text": "把雨改成雪"},
                {"layer": "人物设定", "text": "改成短发，穿深色外套"},
            ],
        })
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        final = client.get(f"/api/v1/sessions/{session_id}").json()

    resolved = [entry for entry in final["action_items"]
                if entry["item_id"] == item["item_id"]][0]
    assert resolved["revisions"] == [
        {"layer": "frame", "text": "把雨改成雪"},
        {"layer": "character", "text": "改成短发，穿深色外套"},
    ]
    pending = [entry for entry in final["action_items"]
               if entry["status"] == "pending"][0]
    assert pending["revisions"] == []


def test_revisions_survive_snapshot_replay_from_the_event_log(workbench):
    """快照落后事件流时，修订历史由事件重放补回——不在快照里也不丢。"""
    app, _model, root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]
        started = _resolve(client, session_id, item["item_id"], {
            "decision": "revise",
            "revisions": [{"layer": "剧情", "text": "改成雨夜重逢"}],
        })
        _wait_job(client, session_id, started.json()["job_id"])

        # 手工制造「事件已写、快照没跟上」：抹掉快照里的修订，把尾号退到决议事件之前。
        snapshot = _state(root, session_id)
        resolved_event = [event for event in _events(root, session_id)
                          if event["type"] == "action_item.resolved"][-1]
        snapshot["last_event_seq"] = resolved_event["seq"] - 1
        for entry in snapshot["action_items"]:
            entry.pop("revisions", None)
        (root / Path(session_id) / SESSION_FILENAME).write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

        replayed = client.get(f"/api/v1/sessions/{session_id}").json()

    target = [entry for entry in replayed["action_items"]
              if entry["item_id"] == item["item_id"]][0]
    assert target["revisions"] == [{"layer": "story", "text": "改成雨夜重逢"}]


def test_stage_state_never_crosses_the_api_boundary(workbench):
    """D4 spec §7 的裁定：可审的是产物与待办，``stage_state`` 不是 API。"""
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, _item = _advance_to_gate(client)
        session_id = detail["session_id"]
        listed = client.get(f"/api/v1/sessions/{session_id}/action-items").json()

    assert "stage_state" not in detail
    assert all("stage_state" not in entry for entry in listed)


def test_resolve_revise_setting_layer_reruns_that_layer_and_marks_applied(workbench):
    app, model, root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]
        before = model.calls.count("character_designer")

        started = _resolve(client, session_id, item["item_id"], {
            "decision": "revise",
            # 向导屏幕上的标签也要认（与 spec §7 的中文别名同一张归一表）。
            "revisions": [{"layer": "人物设定", "text": "改成短发，穿深色外套"}],
        })
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        # 设定级＝从该层起点截断重跑：人物设计师被真的再叫了一次。
        assert model.calls.count("character_designer") == before + 1
        revisions = _state(root, session_id)["stage_state"]["user_revisions"]
        assert revisions[0]["layer"] == "character"
        # 设定级重跑成功后标已落地（此后各注入通道不再带它）。
        assert revisions[0]["applied"] is True
        assert client.get(
            f"/api/v1/sessions/{session_id}").json()["status"] == "awaiting_human"


def test_revisions_accumulate_across_rounds(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        detail, first_item = _advance_to_gate(client)
        session_id = detail["session_id"]

        first = _resolve(client, session_id, first_item["item_id"], {
            "decision": "revise",
            "revisions": [{"layer": "画面", "text": "把伞换成红色"}],
        })
        _wait_job(client, session_id, first.json()["job_id"])
        second_item = [entry for entry in client.get(
            f"/api/v1/sessions/{session_id}").json()["action_items"]
            if entry["status"] == "pending"][0]

        second = _resolve(client, session_id, second_item["item_id"], {
            "decision": "revise",
            "revisions": [{"layer": "只改画面", "text": "把雨改成雪"}],
        })
        _wait_job(client, session_id, second.json()["job_id"])

        revisions = _state(root, session_id)["stage_state"]["user_revisions"]
        assert [(entry["round"], entry["text"]) for entry in revisions] == [
            (1, "把伞换成红色"), (2, "把雨改成雪")]
        # 两条画面级意见都还在待落地清单里（画面级不标 applied，与向导语义一致）。
        assert not any(entry.get("applied") for entry in revisions)


# ---------------------------------------------------------------------------
# 幂等（D2-AC4 的同一条纪律）
# ---------------------------------------------------------------------------

def test_same_idempotency_key_replays_the_first_resolve_without_rerunning(workbench):
    app, model, _root = workbench
    # 闸门停在阶段 2 的第一次模型调用上，给第二次决议留出「任务还在跑」的窗口。
    model.gate_role = "unknown"
    headers = {"Idempotency-Key": "resolve-approve-1"}
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)
        session_id = detail["session_id"]

        first = _resolve(client, session_id, item["item_id"],
                         {"decision": "approve"}, headers)
        assert first.status_code == 202, first.text
        assert model.entered.wait(20), "第一次决议没有进入阶段 2 的模型调用"

        second = _resolve(client, session_id, item["item_id"],
                          {"decision": "approve"}, headers)

        assert second.status_code == 202
        assert second.headers["idempotency-replayed"] == "true"
        assert second.json()["job_id"] == first.json()["job_id"]
        model.release.set()
        _wait_job(client, session_id, first.json()["job_id"])

        assert model.calls.count("unknown") == 1  # prompt_engineer 只跑了一次


# ---------------------------------------------------------------------------
# 请求校验与契约
# ---------------------------------------------------------------------------

def test_revise_without_revisions_is_rejected(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)

        resp = _resolve(client, detail["session_id"], item["item_id"],
                        {"decision": "revise", "revisions": []})

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_unknown_layer_is_rejected(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client)

        resp = _resolve(client, detail["session_id"], item["item_id"], {
            "decision": "revise",
            "revisions": [{"layer": "调色", "text": "整体调暖"}],
        })

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_contract_declares_action_item_endpoints(workbench):
    app, _model, _root = workbench
    paths = app.openapi()["paths"]

    assert "get" in paths["/api/v1/sessions/{session_id}/action-items"]
    assert "post" in paths[
        "/api/v1/sessions/{session_id}/action-items/{item_id}/resolve"]
