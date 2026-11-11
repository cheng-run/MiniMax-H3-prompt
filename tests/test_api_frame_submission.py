"""D4-2（#229）：帧提交服务端纵切（D4-AC10 / AC11 / AC12）。

三件事在这里被钉住：**T2VA 直通阶段 2 不变**、**FL2VA 通过审查后停在新的人
工待办上**、**提交的帧真的进了阶段 2 的读图描述**（spec §14 风险一：形状不齐会
静默降级，只断言 201/状态是抓不住的）。

模型桩仍走 ``workbench_deterministic``；视觉读图打桩在
``tools.reference_auditor.describe_image``（``frame_auditor`` 每次调用时现取）。
"""
from __future__ import annotations

import base64
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
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.workbench_store import EVENTS_FILENAME, EVENT_TYPES
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

# FL2VA 的关键帧回复必须两帧齐全（bundle 的 __post_init__ 会校验）。
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

# 两张最小 PNG（1x1，颜色不同）：上传按内容哈希去重，首尾帧必须是不同内容才会
# 落到两个素材上。
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


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    """临时会话根 + 确定性桩模型 + 桩读图（读图结果带帧位标签，便于断言）。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel(replies={
        **DEFAULT_REPLIES,
        "frame_prompt_engineer": json.dumps(FL2VA_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)

    def _describe(path, instruction=None):
        return f"桩读图：{Path(path).name} 的真实画面描述"

    monkeypatch.setattr(reference_auditor, "describe_image", _describe)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


def _create_session(client: TestClient, **kwargs) -> dict:
    session = client.post("/api/v1/sessions", json=_brief_body(**kwargs))
    assert session.status_code == 201, session.text
    return session.json()


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
    session = _create_session(client, **kwargs)
    session_id = session["session_id"]
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["status"] == "awaiting_human"
    pending = [item for item in detail["action_items"] if item["status"] == "pending"]
    assert len(pending) == 1
    return detail, pending[0]


def _resolve(client: TestClient, session_id: str, item_id: str, body: dict):
    return client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve",
        json=body)


def _upload(client: TestClient, session_id: str, name: str = "frame.png",
            data: bytes | None = None) -> dict:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (name, data or PNG_FIRST, "image/png")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / EVENTS_FILENAME
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _state(root: Path, session_id: str) -> dict:
    path = root / Path(session_id) / SESSION_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 分叉：T2VA 直通，FL2VA 停下来收帧（D4-AC10 / AC11）
# ---------------------------------------------------------------------------

def test_replaying_the_frame_gate_with_the_same_key_returns_null_without_a_new_item(
        workbench):
    app, _model, _root = workbench
    headers = {"Idempotency-Key": "fl2va-approve-2"}
    with workbench_client(app) as client:
        detail, review = _advance_to_gate(client, variant="FL2VA")
        session_id = detail["session_id"]

        first = client.post(
            f"/api/v1/sessions/{session_id}/action-items/"
            f"{review['item_id']}/resolve",
            json={"decision": "approve"}, headers=headers)
        second = client.post(
            f"/api/v1/sessions/{session_id}/action-items/"
            f"{review['item_id']}/resolve",
            json={"decision": "approve"}, headers=headers)
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert first.status_code == 202 and first.json() is None
    assert second.status_code == 202 and second.json() is None
    assert second.headers["idempotency-replayed"] == "true"
    assert len([entry for entry in final["action_items"]
                if entry["kind"] == "frame_submission"]) == 1


def test_t2va_approve_still_runs_stage2_directly(workbench):
    """回归：文字链不出现帧提交待办，通过后直接跑到完成。"""
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, item = _advance_to_gate(client, variant="T2VA")
        session_id = detail["session_id"]

        started = _resolve(client, session_id, item["item_id"], {"decision": "approve"})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert final["status"] == "completed"
    assert [entry["kind"] for entry in final["action_items"]] == ["stage1_review"]
    assert not any(entry["kind"] == "frame_submission"
                   for entry in final["action_items"])


def test_fl2va_approve_creates_a_frame_submission_item_and_waits(workbench):
    """帧链通过审查后不直接跑阶段 2：停在 awaiting_human，挂第二个人工待办。"""
    app, _model, root = workbench
    with workbench_client(app) as client:
        detail, review = _advance_to_gate(client, variant="FL2VA")
        session_id = detail["session_id"]

        started = _resolve(client, session_id, review["item_id"],
                           {"decision": "approve"})
        assert started.status_code == 202, started.text
        # 没有后台任务可等：这条分支只创建待办。
        assert started.json() is None

        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert final["status"] == "awaiting_human"
    resolved = [entry for entry in final["action_items"]
                if entry["item_id"] == review["item_id"]][0]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "approve"
    pending = [entry for entry in final["action_items"]
               if entry["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "frame_submission"
    # 阶段 2 还没跑：最终提示词不存在。
    assert not (root / Path(session_id) / "video-prompt.md").exists()
    # 事件类型仍十种闭合：这一分支只用了既有的两种。
    types = [event["type"] for event in _events(root, session_id)]
    assert set(types) <= set(EVENT_TYPES)
    assert "action_item.created" in types


# ---------------------------------------------------------------------------
# 交帧：读图审计在任务里，帧真的进了阶段 2（D4-AC12、spec §14 风险一）
# ---------------------------------------------------------------------------

def _fl2va_to_frame_gate(client: TestClient) -> tuple[str, dict]:
    detail, review = _advance_to_gate(client, variant="FL2VA")
    session_id = detail["session_id"]
    resp = _resolve(client, session_id, review["item_id"], {"decision": "approve"})
    assert resp.status_code == 202, resp.text
    assert resp.json() is None
    gate = client.get(f"/api/v1/sessions/{session_id}").json()
    item = [entry for entry in gate["action_items"]
            if entry["status"] == "pending"][0]
    assert item["kind"] == "frame_submission"
    return session_id, item


def test_submitted_frames_are_audited_and_reach_stage2(workbench):
    """判据不是 201，而是阶段 2 组装时**真的拿到了**这两帧的读图描述。"""
    app, _model, root = workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)
        first = _upload(client, session_id, "first.png", PNG_FIRST)
        last = _upload(client, session_id, "last.png", PNG_LAST)

        started = _resolve(client, session_id, item["item_id"], {
            "frames": {"first": first["asset_id"], "last": last["asset_id"]}})
        assert started.status_code == 202, started.text
        assert started.json() is not None
        done = _wait_job(client, session_id, started.json()["job_id"])
        assert done["status"] == "succeeded", done

        final = client.get(f"/api/v1/sessions/{session_id}").json()
        prompt = client.get(
            f"/api/v1/artifacts/{session_id}/video-prompt.md/content")

    assert final["status"] == "completed"
    assert prompt.status_code == 200 and prompt.text.strip()
    # 有首尾两帧 → 不降级。
    assert final["brief"]["variant"] == "FL2VA"
    state = _state(root, session_id)
    assert state.get("variant_downgraded", "") == ""

    descriptions = state["stage_state"]["fl2va_frame_descriptions"]
    assert [row["role"] for row in descriptions] == ["first", "last"]
    # 描述来自我们提交的那两个文件（桩读图把文件名写进结果里）。
    assert "桩读图" in descriptions[0]["description"]
    assert "桩读图" in descriptions[1]["description"]
    assert descriptions[0]["path"] != descriptions[1]["path"]

    # 帧直接引用素材文件：记录里的 target 是 assets/ 下的正本，没有 frames/ 副本。
    records = state["stage_state"]["frame_images"]
    assert {row["role"] for row in records} == {"first", "last"}
    assets_dir = root / Path(session_id) / "assets"
    for row in records:
        assert Path(row["target"]).parent == assets_dir
        assert Path(row["target"]).is_file()
    assert not (root / Path(session_id) / "frames").exists()

    # 决议事件带上用户选的帧，回放/排障看得出是哪两个素材。
    resolved = [event for event in _events(root, session_id)
                if event["type"] == "action_item.resolved"][-1]
    assert resolved["payload"]["decision"] == "submit_frames"
    assert resolved["payload"]["frames"] == {
        "first": first["asset_id"], "last": last["asset_id"]}


# ---------------------------------------------------------------------------
# 校验：外会话/不存在素材、kind 与载荷错配一律 422 frame_invalid（D4-AC12）
# ---------------------------------------------------------------------------

def _assert_frame_invalid(resp) -> None:
    assert resp.status_code == 422, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "frame_invalid"


def test_frame_from_another_session_is_rejected(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)
        # 另一个会话的素材：本会话里看不见它，但 id 是可拼的。
        other = _create_session(client, variant="T2VA")["session_id"]
        foreign = _upload(client, other, "foreign.png", PNG_LAST)

        resp = _resolve(client, session_id, item["item_id"],
                        {"frames": {"first": foreign["asset_id"]}})

        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    _assert_frame_invalid(resp)
    # 失败不吞掉待办：会话仍在原地等第二次提交。
    assert detail["status"] == "awaiting_human"
    assert [entry["status"] for entry in detail["action_items"]
            if entry["kind"] == "frame_submission"] == ["pending"]


def test_missing_asset_is_rejected_with_frame_invalid_not_404(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)

        resp = _resolve(client, session_id, item["item_id"], {
            "frames": {"first": f"{session_id}/0123456789abcdef"}})

    _assert_frame_invalid(resp)


def test_non_image_asset_is_rejected(workbench):
    """帧槽是图片位：把一段视频塞进去会让读图/组装在任务里才炸，应该当场 422。"""
    app, _model, _root = workbench
    video = Path(__file__).parent / "fixtures" / "tiny.webm"
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)
        resp = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": ("clip.webm", video.read_bytes(), "video/webm")})
        assert resp.status_code == 201, resp.text
        asset_id = resp.json()["asset_id"]

        resp = _resolve(client, session_id, item["item_id"],
                        {"frames": {"first": asset_id}})

    _assert_frame_invalid(resp)


def test_stage1_review_rejects_frames(workbench):
    """kind 与载荷错配：审查待办只认 approve/revise。"""
    app, _model, _root = workbench
    with workbench_client(app) as client:
        detail, review = _advance_to_gate(client, variant="FL2VA")
        session_id = detail["session_id"]
        asset = _upload(client, session_id, "first.png", PNG_FIRST)

        resp = _resolve(client, session_id, review["item_id"],
                        {"frames": {"first": asset["asset_id"]}})

    _assert_frame_invalid(resp)


def test_frame_submission_rejects_approve_and_revisions(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)

        approve = _resolve(client, session_id, item["item_id"],
                           {"decision": "approve"})
        revise = _resolve(client, session_id, item["item_id"], {
            "revisions": [{"layer": "画面", "text": "把雨改成雪"}],
            "frames": {}})

    _assert_frame_invalid(approve)
    _assert_frame_invalid(revise)


def test_frame_submission_without_frames_is_rejected(workbench):
    app, _model, _root = workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)

        resp = _resolve(client, session_id, item["item_id"], {})

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


# ---------------------------------------------------------------------------
# 降级矩阵：缺哪一槽就退到哪一档，全空＝显式跳过（D4-AC12）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("slots", "expected_variant", "expected_roles"),
    [
        ({"first": PNG_FIRST, "last": PNG_LAST}, "FL2VA", ["first", "last"]),
        ({"first": PNG_FIRST}, "I2VA", ["first"]),
        ({"last": PNG_LAST}, "L2VA", ["last"]),
        ({}, "T2VA", []),
    ],
)
def test_frame_combination_degrades_per_the_matrix(
        workbench, slots, expected_variant, expected_roles):
    app, _model, root = workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)
        frames = {}
        for role in ("first", "last"):
            if role in slots:
                asset = _upload(client, session_id, f"{role}.png", slots[role])
                frames[role] = asset["asset_id"]

        started = _resolve(client, session_id, item["item_id"], {"frames": frames})
        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        assert done["status"] == "succeeded", done

        final = client.get(f"/api/v1/sessions/{session_id}").json()

    state = _state(root, session_id)
    assert final["status"] == "completed"
    assert final["brief"]["variant"] == expected_variant
    assert state["variant_downgraded"] == (
        "" if expected_variant == "FL2VA"
        else f"FL2VA→{expected_variant}")
    descriptions = state["stage_state"]["fl2va_frame_descriptions"]
    assert [row["role"] for row in descriptions] == expected_roles
    # 显式跳过仍要有一条可查的决议记录，不是「什么都没发生」。
    assert final["action_items"][-1]["decision"] == "submit_frames"


# ---------------------------------------------------------------------------
# 桩管线：假读图读**用户提交的帧位**（D4 §11；真实/桩两条路径语义一致）
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_workbench(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setenv("MINIMAX_FAKE_PIPELINE", "1")
    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: (_ for _ in ()).throw(AssertionError("桩管线不许建真实模型")))
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, root


@pytest.mark.parametrize(
    ("slots", "expected_variant", "expected_roles"),
    [
        ({"first": PNG_FIRST, "last": PNG_LAST}, "FL2VA", ["first", "last"]),
        ({"first": PNG_FIRST}, "I2VA", ["first"]),
        ({}, "T2VA", []),
    ],
)
def test_fake_pipeline_reads_the_frames_the_user_submitted(
        fake_workbench, slots, expected_variant, expected_roles):
    app, root = fake_workbench
    with workbench_client(app) as client:
        session_id, item = _fl2va_to_frame_gate(client)
        frames = {}
        for role in ("first", "last"):
            if role in slots:
                asset = _upload(client, session_id, f"{role}.png", slots[role])
                frames[role] = asset["asset_id"]

        started = _resolve(client, session_id, item["item_id"], {"frames": frames})
        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        assert done["status"] == "succeeded", done
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    state = _state(root, session_id)
    assert final["status"] == "completed"
    assert final["brief"]["variant"] == expected_variant
    descriptions = state["stage_state"]["fl2va_frame_descriptions"]
    assert [row["role"] for row in descriptions] == expected_roles
    assert all("桩读图" in row["description"] for row in descriptions)


def test_contract_declares_the_frames_body_and_nullable_result(workbench):
    """契约链（D4-AC15）：body 多了 frames，decision 可省，202 可能是 null。"""
    app, _model, _root = workbench
    schema = app.openapi()
    operation = schema["paths"][
        "/api/v1/sessions/{session_id}/action-items/{item_id}/resolve"]["post"]

    body_ref = operation["requestBody"]["content"][
        "application/json"]["schema"]["$ref"]
    body = schema["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]
    assert "frames" in body["properties"]
    # 帧待办没有 approve/revise：decision 不能再是必填。
    assert "decision" not in body.get("required", [])
    frames_ref = body["properties"]["frames"]["anyOf"][0]["$ref"]
    frames = schema["components"]["schemas"][frames_ref.rsplit("/", 1)[-1]]
    assert set(frames["properties"]) == {"first", "last"}

    result = operation["responses"]["202"]["content"][
        "application/json"]["schema"]
    assert {"$ref": "#/components/schemas/JobSummary"} in result["anyOf"]
    assert {"type": "null"} in result["anyOf"]





