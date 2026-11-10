"""D2-6（#220）：阶段 2 交付与文本产物读取的对外面（D2-AC7 / AC13）。

模型桩走 ``workbench_deterministic``：图、节点、步骤机都是真的，只有模型回复是预置的。
产物的判据有两个独立来源——接口返回的元数据与**磁盘上的文件本身**，两者逐字对照，
不让接口自己证明自己。
"""
from __future__ import annotations

import json
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


def _brief_body() -> dict:
    return {
        "mode": "base",
        "variant": "I2VA",
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
        "frame_prompt_engineer": json.dumps(FRAME_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


def _run_to_completed(client: TestClient) -> str:
    """建会话 → 推进到人工闸门 → approve → 阶段 2 收尾；返回 session_id。"""
    session = client.post("/api/v1/sessions", json=_brief_body()).json()
    session_id = session["session_id"]
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    pending = [item for item in detail["action_items"] if item["status"] == "pending"]
    assert len(pending) == 1
    resolved = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{pending[0]['item_id']}/resolve",
        json={"decision": "approve"})
    assert resolved.status_code == 202, resolved.text
    _wait_job(client, session_id, resolved.json()["job_id"])
    final = client.get(f"/api/v1/sessions/{session_id}").json()
    assert final["status"] == "completed"
    return session_id


def _wait_job(client: TestClient, session_id: str, job_id: str,
              timeout: float = 60.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(
            f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


# ---------------------------------------------------------------------------
# 阶段 2 交付：产物登记与文本内容读取（D2-AC7）
# ---------------------------------------------------------------------------

def test_completed_session_registers_video_prompt_and_content_matches_disk(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session_id = _run_to_completed(client)

        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        artifact = {item["name"]: item for item in detail["artifacts"]}["video-prompt.md"]
        on_disk = (root / Path(session_id) / "video-prompt.md").read_text(encoding="utf-8")

        resp = client.get(
            f"/api/v1/artifacts/{artifact['artifact_id']}/content")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == on_disk
    assert artifact["artifact_id"] == f"{session_id}/video-prompt.md"
    assert artifact["kind"] == "video_prompt"
    assert artifact["bytes"] == len(on_disk.encode("utf-8"))
    assert artifact["created_at"]
    assert on_disk.strip()


def test_artifact_created_event_is_recorded_with_the_registered_id(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session_id = _run_to_completed(client)
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    lines = (root / Path(session_id) / EVENTS_FILENAME).read_text(
        encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    created = [event for event in events if event["type"] == "artifact.created"]

    assert len(created) == 1
    assert created[0]["session_id"] == session_id
    assert created[0]["seq"] <= detail["last_event_seq"]
    assert created[0]["payload"] == {
        "artifact_id": f"{session_id}/video-prompt.md",
        "kind": "video_prompt",
        "name": "video-prompt.md",
    }
    # 产物事件在阶段 2 的 job.succeeded 之前：先有交付物，才有「完成」。
    types = [event["type"] for event in events]
    assert types.index("artifact.created") < len(types) - 1
    assert types[-1] == "job.succeeded"


def test_text_artifact_over_one_mib_is_413(workbench):
    """上限是 spec §10 的 1 MiB；边界值按字面取，不跟实现常量走。"""
    app, _model, root = workbench
    limit = 1024 * 1024
    with workbench_client(app) as client:
        session_id = _run_to_completed(client)
        path = root / Path(session_id) / "video-prompt.md"

        path.write_text("a" * (limit + 1), encoding="utf-8")
        too_large = client.get(
            f"/api/v1/artifacts/{session_id}/video-prompt.md/content")

        path.write_text("a" * limit, encoding="utf-8")
        at_limit = client.get(
            f"/api/v1/artifacts/{session_id}/video-prompt.md/content")

    assert too_large.status_code == 413
    assert too_large.headers["content-type"].startswith("application/problem+json")
    assert too_large.json()["code"] == "artifact_too_large"
    assert at_limit.status_code == 200
    assert len(at_limit.text) == limit


def test_video_artifact_is_listed_and_readable(workbench):
    """成片（#225 起）走 content 端点给字节 + Range；清单里的元数据照旧。"""
    app, _model, root = workbench
    payload = bytes(range(256)) * 8
    with workbench_client(app) as client:
        session_id = _run_to_completed(client)
        (root / Path(session_id) / "final.mp4").write_bytes(payload)

        listed = client.get(f"/api/v1/sessions/{session_id}/artifacts")
        detail = client.get(f"/api/v1/sessions/{session_id}")
        content = client.get(
            f"/api/v1/artifacts/{session_id}/final.mp4/content")
        chunk = client.get(
            f"/api/v1/artifacts/{session_id}/final.mp4/content",
            headers={"Range": "bytes=8-15"})

    assert listed.status_code == 200, listed.text
    by_name = {item["name"]: item for item in listed.json()}
    assert set(by_name) == {"video-prompt.md", "final.mp4"}
    assert by_name["final.mp4"]["kind"] == "video"
    assert by_name["final.mp4"]["bytes"] == len(payload)
    assert by_name["final.mp4"]["artifact_id"] == f"{session_id}/final.mp4"
    assert set(by_name) == {item["name"] for item in detail.json()["artifacts"]}

    assert content.status_code == 200
    assert content.headers["content-type"] == "video/mp4"
    assert content.headers["accept-ranges"] == "bytes"
    assert content.headers["etag"]
    assert content.headers["last-modified"]
    assert content.content == payload
    assert chunk.status_code == 206
    assert chunk.headers["content-range"] == f"bytes 8-15/{len(payload)}"
    assert chunk.content == payload[8:16]


# ---------------------------------------------------------------------------
# 越界与不存在：一律 404 artifact_not_found（spec §10 / §12）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("artifact_id", [
    "../secret/video-prompt.md",
    "a/../../secret/video-prompt.md",
    "/etc/video-prompt.md",
    "C:/x/video-prompt.md",
    "主题/GEN001\\video-prompt.md",
    "video-prompt.md",
    "{session}/notes.txt",
    "不存在/GEN001/video-prompt.md",
])
def test_escaping_and_unknown_artifact_ids_are_problem_404(workbench, artifact_id):
    app, _model, root = workbench
    outside = root.parent / "secret"
    outside.mkdir(parents=True)
    (outside / "video-prompt.md").write_text("根外机密", encoding="utf-8")
    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body()).json()
        target = artifact_id.format(session=session["session_id"])
        # 点号要百分号编码：客户端（httpx）会在发请求前把字面 ``..`` 规范化掉，
        # 那样测的就不是服务端的越界判定，而是客户端自己的路径清洗。
        encoded = target.replace(".", "%2E")
        resp = client.get(f"/api/v1/artifacts/{encoded}/content")

    assert resp.status_code == 404, target
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "artifact_not_found"
    assert "根外机密" not in resp.text


def test_artifact_symlinked_outside_the_root_is_404(workbench):
    app, _model, root = workbench
    outside = root.parent / "secret.md"
    outside.write_text("根外机密", encoding="utf-8")
    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body()).json()
        session_id = session["session_id"]
        link = root / Path(session_id) / "video-prompt.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"本机建不了符号链接：{exc}")

        resp = client.get(f"/api/v1/artifacts/{session_id}/video-prompt.md/content")
        listed = client.get(f"/api/v1/sessions/{session_id}/artifacts")

    assert resp.status_code == 404
    assert resp.json()["code"] == "artifact_not_found"
    assert "根外机密" not in resp.text
    # 元数据同样不许泄漏根外文件：清单里不出现这条。
    assert listed.json() == []


# ---------------------------------------------------------------------------
# 已知文件名的其余文本产物（spec §10）
# ---------------------------------------------------------------------------

def test_summary_artifacts_are_listed_and_readable(workbench):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body()).json()
        session_id = session["session_id"]
        directory = root / Path(session_id)
        (directory / "summary-zh.md").write_text("# 摘要\n雨夜故事。", encoding="utf-8")
        (directory / "summary-zh.json").write_text(
            json.dumps({"plot": "雨夜旧信", "shots": 2}, ensure_ascii=False),
            encoding="utf-8")

        listed = {item["name"]: item for item in
                  client.get(f"/api/v1/sessions/{session_id}/artifacts").json()}
        markdown = client.get(
            f"/api/v1/artifacts/{session_id}/summary-zh.md/content")
        payload = client.get(
            f"/api/v1/artifacts/{session_id}/summary-zh.json/content")

    assert set(listed) == {"summary-zh.md", "summary-zh.json"}
    assert listed["summary-zh.md"]["kind"] == "summary_markdown"
    assert listed["summary-zh.json"]["kind"] == "summary_json"
    assert markdown.status_code == 200 and markdown.text == "# 摘要\n雨夜故事。"
    assert payload.status_code == 200
    assert json.loads(payload.text) == {"plot": "雨夜旧信", "shots": 2}


# ---------------------------------------------------------------------------
# 契约（D2-AC13 / AC14）
# ---------------------------------------------------------------------------
def test_contract_declares_artifact_content_and_its_413(workbench):
    app, _model, _root = workbench
    paths = app.openapi()["paths"]

    assert "/api/v1/sessions/{session_id}/artifacts" in paths
    content_path = "/api/v1/artifacts/{artifact_id}/content"
    assert content_path in paths
    # GET 与 HEAD 成对：#222 §7 让成片走 §6.2 的 Range 矩阵，那一格里就有 HEAD。
    assert {"get", "head"} <= set(paths[content_path])
    responses = paths[content_path]["get"]["responses"]
    assert "200" in responses and "404" in responses and "413" in responses
    assert "206" in responses
    # 形态由登记表派生：三个文本产物是 text/plain，成片是 video/mp4。
    assert "text/plain" in responses["200"]["content"]
    assert "video/mp4" in responses["200"]["content"]
    assert "video/mp4" in responses["206"]["content"]
    # 媒体响应头与媒体端点同一份声明（含 Content-Length）：一处有一处没有就是两套口径。
    for header in ("Accept-Ranges", "ETag", "Last-Modified", "Content-Length"):
        assert header in responses["206"]["headers"], header
    assert "application/problem+json" in responses["413"]["content"]
    assert "application/problem+json" in responses["404"]["content"]
    assert "application/problem+json" in responses["416"]["content"]
