"""D4-1（#228）：阶段 1 中间产物落盘为可看可复制的产物（D4-AC7 服务端一半）。

落盘判据有**两个独立来源**：接口返回的登记元数据与磁盘上的文件本身。产物名字与
``kind`` 都从 ``workbench_store`` 的登记表派生（端点不另抄白名单），所以这里既断言
HTTP 面，也断言「文件名 → kind」这一格，防止有人把新名字只加进一条读取路径。
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
from minimax_h3_prompt.workbench_store import EVENTS_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

# FL2VA 的关键帧回复必须两帧齐全（bundle 的 __post_init__ 会校验），
# 且同帧内不能重复生图模型——一行一个模型即可。
FL2VA_BUNDLE = {
    "prompt_node_id": "187",
    "scene_anchor": "雨夜老城街口，霓虹灯在积水里反光",
    "continuity_constraints": ["人物服装与道具在画面内保持一致"],
    "first": {
        "positive_prompt": "雨夜老城街口，撑黑伞的成年女性站在积水前，中景，冷蓝霓虹侧逆光",
        "composition": "中景，人物居中偏左",
        "lighting": "冷蓝霓虹侧逆光",
    },
    "last": {
        "positive_prompt": "雨夜老城街口，撑黑伞的女性走远，积水倒映霓虹，远景，冷蓝主调",
        "composition": "远景，人物偏右",
        "lighting": "冷蓝霓虹顶光",
    },
}


def _brief_body(variant: str = "T2VA") -> dict:
    return {
        "mode": "base",
        "variant": variant,
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
    }


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel(replies={
        **DEFAULT_REPLIES,
        "frame_prompt_engineer": json.dumps(FL2VA_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


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


def _advance_to_gate(client: TestClient, **kwargs) -> dict:
    session = client.post("/api/v1/sessions", json=_brief_body(**kwargs))
    assert session.status_code == 201, session.text
    session_id = session.json()["session_id"]
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["status"] == "awaiting_human"
    return detail


def test_frame_chain_session_also_writes_first_and_last_frame_prompt_artifacts(workbench):
    """FL2VA：阶段 1 出 4 个产物；两帧词同 kind，内容来自 bundle 的真实行。"""
    app, _model, root = workbench
    with workbench_client(app) as client:
        detail = _advance_to_gate(client, variant="FL2VA")
        session_id = detail["session_id"]
        by_name = {item["name"]: item for item in detail["artifacts"]}
        first = client.get(
            f"/api/v1/artifacts/{session_id}/first-frame-prompt.md/content")
        last = client.get(
            f"/api/v1/artifacts/{session_id}/last-frame-prompt.md/content")

    assert set(by_name) == {
        "script.md", "shot-table.md",
        "first-frame-prompt.md", "last-frame-prompt.md",
    }
    assert by_name["first-frame-prompt.md"]["kind"] == "frame_prompt"
    assert by_name["last-frame-prompt.md"]["kind"] == "frame_prompt"
    assert first.status_code == 200 and last.status_code == 200
    assert "首帧" in first.text
    assert FL2VA_BUNDLE["first"]["positive_prompt"] in first.text
    assert "尾帧" in last.text
    assert FL2VA_BUNDLE["last"]["positive_prompt"] in last.text


def test_revise_rerun_refreshes_the_review_artifacts(workbench):
    """spec §14 风险表那条：修订重跑后文件必须是新稿，不能停在上一轮。

    判据用**换过的模型回复**：第二稿正文与第一稿不同，文件与 content 端点都得跟上。
    """
    app, model, root = workbench
    with workbench_client(app) as client:
        detail = _advance_to_gate(client)
        session_id = detail["session_id"]
        item = [entry for entry in detail["action_items"]
                if entry["status"] == "pending"][0]
        first_script = (root / Path(session_id) / "script.md").read_text(
            encoding="utf-8")

        model.replies["screenwriter"] = "分场剧本（第二稿）：雨停后的清晨，人物收起伞。"
        started = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "revise",
                  "revisions": [{"layer": "剧情", "text": "把雨改成雨停后的清晨"}]})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        second_script = (root / Path(session_id) / "script.md").read_text(
            encoding="utf-8")
        served = client.get(
            f"/api/v1/artifacts/{session_id}/script.md/content")

    assert "第二稿" in second_script
    assert second_script != first_script
    assert served.status_code == 200 and served.text == second_script


def test_frame_layer_revise_refreshes_the_frame_prompt_artifact(workbench):
    """画面级修订只重出关键帧词——对应的两个帧词文件也必须换成新版本。"""
    app, model, root = workbench
    with workbench_client(app) as client:
        detail = _advance_to_gate(client, variant="FL2VA")
        session_id = detail["session_id"]
        item = [entry for entry in detail["action_items"]
                if entry["status"] == "pending"][0]
        before = (root / Path(session_id) / "first-frame-prompt.md").read_text(
            encoding="utf-8")

        revised = json.loads(json.dumps(FL2VA_BUNDLE, ensure_ascii=False))
        revised["first"]["positive_prompt"] = "雨夜街口，女性收起黑伞，近景，暖黄路灯"
        model.replies["frame_prompt_engineer"] = json.dumps(revised, ensure_ascii=False)
        started = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "revise",
                  "revisions": [{"layer": "画面", "text": "改成收起伞的近景"}]})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        after = (root / Path(session_id) / "first-frame-prompt.md").read_text(
            encoding="utf-8")

    assert "收起黑伞" in after
    assert after != before


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / EVENTS_FILENAME
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_text_route_writes_script_and_shot_table_artifacts(workbench):
    """T2VA：阶段 1 完成出 2 个中间产物；内容端点读到的与磁盘逐字一致。"""
    app, _model, root = workbench
    with workbench_client(app) as client:
        detail = _advance_to_gate(client)
        session_id = detail["session_id"]
        by_name = {item["name"]: item for item in detail["artifacts"]}
        script = client.get(
            f"/api/v1/artifacts/{session_id}/script.md/content")
        shot_table = client.get(
            f"/api/v1/artifacts/{session_id}/shot-table.md/content")

    assert set(by_name) == {"script.md", "shot-table.md"}
    assert by_name["script.md"]["kind"] == "script"
    assert by_name["shot-table.md"]["kind"] == "shot_table"
    for name, resp in (("script.md", script), ("shot-table.md", shot_table)):
        on_disk = (root / Path(session_id) / name).read_text(encoding="utf-8")
        assert resp.status_code == 200, (name, resp.text)
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.text == on_disk
        assert on_disk.strip()

    # 内容来自真实阶段 1 的产物（确定性桩的回复就是它写进 state 的正文）。
    assert DEFAULT_REPLIES["screenwriter"].strip() in script.text
    assert DEFAULT_REPLIES["storyboard"].strip() in shot_table.text

    # 登记事件与文件成对：先有产物，才有「阶段 1 完成」。
    events = _events(root, session_id)
    created = [event["payload"]["name"] for event in events
               if event["type"] == "artifact.created"]
    assert created == ["script.md", "shot-table.md"]
    types = [event["type"] for event in events]
    assert types.index("artifact.created") < types.index("job.succeeded")
