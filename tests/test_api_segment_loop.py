"""D6（#246）：长片段循环的对外面——第三个人工待办与三种结局。

钉住四件事：
1. 规划完自动写第 1 段 → 挂出唯一的 ``segment_video`` 待办（载荷带段号），会话 await。
2. resolve 只收 ``video_asset_id``；错配/外会话素材/非 mp4/技术档不合格一律 422
   ``segment_video_invalid``，会话仍可重传。
3. 通过与拒绝严格分开：通过落 passed 并带着尾帧锚写下一段；被拒不落 passed、重建同一段
   待办、发 ``action_item.created``、**不记 failed**。
4. 技术失败落 failed，重试重跑同一次校验、**不要求重交素材**。

模型桩走 ``workbench_deterministic``；写段与语义判据分别打桩在 ``segment_prompts`` 与
``segment_validation`` 同一函数上（不另开一条路）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts
from minimax_h3_prompt import segment_validation
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.tools import frame_auditor
from conftest import write_h3_tier_mp4
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

PLANS = (
    SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,),
                summary="开场", end_hook="人物停在门口"),
    SegmentPlan(index=1, start_s=6, end_s=12, shots_in_segment=(2,),
                summary="收束", end_hook="灯亮起"),
)


def _brief_body(duration: float = 12.0, variant: str = "T2VA",
                plot: str = "雨夜旧信") -> dict:
    return {
        "mode": "base", "variant": variant, "duration": duration,
        "style": "写实", "language": "Chinese", "plot": plot,
    }


def _wait_job(client, session_id: str, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


def _wait_until(client, session_id: str, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        if predicate(detail):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{session_id}")


def _pending_segment_todos(detail: dict) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == "segment_video" and item["status"] == "pending"]


def _upload(client, session_id: str, path: Path, media: str = "video/mp4") -> str:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (path.name, path.read_bytes(), media)})
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_id"]


def _resolve(client, session_id: str, item_id: str, body: dict):
    return client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve", json=body)


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def loop_app(tmp_path, monkeypatch):
    """临时会话根 + 确定性模型；规划/写段打桩到返回固定段表与固定段正文。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    captured: dict[int, dict] = {}

    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: list(PLANS))

    def fake_write(plan, all_plans, state, llm):
        # 用**真实**请求装配器产出请求，证明尾帧锚真的进了写段请求（不靠人工看）。
        captured[plan.index] = {
            "request": segment_prompts.build_segment_v2_request(plan, all_plans, state),
            "state": dict(state),
        }
        return f"[Shot 1] At 00:00.000, segment {plan.index + 1} stub prompt.\n"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root, captured


def _to_first_segment_todo(client, session_id: str) -> dict:
    """跑完阶段 1 → 审查通过 → 阶段 2 → 规划 → 写第 1 段，停在第一段待办。"""
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    review = [item for item in client.get(
        f"/api/v1/sessions/{session_id}").json()["action_items"]
        if item["status"] == "pending"][0]
    approved = _resolve(client, session_id, review["item_id"], {"decision": "approve"})
    assert approved.status_code == 202, approved.text
    detail = _wait_until(
        client, session_id,
        lambda body: body["status"] != "running" and _pending_segment_todos(body))
    return detail


# ---------------------------------------------------------------------------
# 通过：落 passed、带尾帧锚写下一段
# ---------------------------------------------------------------------------

def test_pass_records_segment_and_writes_next_with_tail_anchor(
        loop_app, tmp_path, monkeypatch):
    app, _model, root, captured = loop_app
    monkeypatch.setattr(
        frame_auditor, "describe_bridge_frame",
        lambda _path: "尾帧画面：人物已经停在门口，灯光昏黄")
    monkeypatch.setattr(
        segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: True)
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        assert todo["segment_index"] == 1
        asset_id = _upload(client, session_id, video)

        started = _resolve(client, session_id, todo["item_id"],
                           {"video_asset_id": asset_id})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and any(
                seg["status"] == "waiting_video" and seg["index"] == 2
                for seg in body["segments"]))

    segments = detail["segments"]
    assert segments[0]["status"] == "passed"
    assert segments[0]["video_asset_id"] == asset_id
    # 通过后不残留上一轮被拒的原因。
    assert segments[0]["rejection_reason"] == ""
    assert segments[1]["status"] == "waiting_video"
    assert [item["segment_index"] for item in _pending_segment_todos(detail)] == [2]

    # 第 2 段写词请求里必须带第 1 段视频的尾帧锚（D6-AC4）。
    assert 1 in captured, "第 2 段没有被写过"
    assert "人物已经停在门口" in captured[1]["request"]
    # 尾帧锚也真写进了 state（下一段要按它取）。
    anchors = captured[1]["state"].get("bridge_frame_descriptions") or []
    assert {"segment": 1, "description": "尾帧画面：人物已经停在门口，灯光昏黄"} in anchors

    # 两段提示词都是可读产物，kind=segment_prompt。
    for number in (1, 2):
        artifact = client.get(
            f"/api/v1/artifacts/{session_id}/segments/shot-{number:02d}.md/content")
        assert artifact.status_code == 200
    listed = {entry["name"]: entry["kind"] for entry in detail["artifacts"]}
    assert listed["segments/shot-01.md"] == "segment_prompt"
    assert listed["segments/shot-02.md"] == "segment_prompt"


# ---------------------------------------------------------------------------
# 被拒：不落 passed、重建同一段待办、不记 failed
# ---------------------------------------------------------------------------

def test_reject_rebuilds_the_same_segment_todo_without_passing(
        loop_app, tmp_path, monkeypatch):
    app, _model, root, _captured = loop_app
    monkeypatch.setattr(
        frame_auditor, "describe_bridge_frame",
        lambda _path: "尾帧画面：人物还在走动，没有停下")
    monkeypatch.setattr(
        segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: False)
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        asset_id = _upload(client, session_id, video)

        started = _resolve(client, session_id, todo["item_id"],
                           {"video_asset_id": asset_id})
        assert started.status_code == 202, started.text
        job = _wait_job(client, session_id, started.json()["job_id"])
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] == "awaiting_human"
            and _pending_segment_todos(body))

    # 用户的素材不合格不是系统故障：job 成功（不 failed）、会话回到等人工。
    assert job["status"] == "succeeded"
    segments = detail["segments"]
    assert segments[0]["status"] == "waiting_video"  # 被拒段不落 passed
    assert segments[0]["video_asset_id"] == ""
    pending = _pending_segment_todos(detail)
    assert [item["segment_index"] for item in pending] == [1]
    assert pending[0]["item_id"] != todo["item_id"]  # 重建，不是复用旧待办
    # 被拒要有**具体**原因（D6 §5.4）：尾帧实际画面 + 本段末态要求，缺一不可。
    reason = segments[0]["rejection_reason"]
    assert "尾帧未达到本段末态" in reason
    assert "人物还在走动" in reason
    assert "人物停在门口" in reason
    # 不许越过这一段去写第 2 段。
    assert not (root / Path(session_id) / "segments" / "shot-02.md").exists()

    events = _events(root, session_id)
    types = [event["type"] for event in events]
    assert types.count("action_item.created") >= 2  # 首次挂 + 重建
    assert "job.failed" not in types
    # 原因也随重建的待办事件发出（事件类型仍闭合，不新增类型）。
    rebuilt = [event for event in events
               if event["type"] == "action_item.created"][-1]
    assert "尾帧未达到本段末态" in rebuilt["payload"].get("reason", "")
    # 重建的待办事件同时带着段号：崩溃补齐后重放要说得清「在等第几段」（D7-1）。
    assert rebuilt["payload"].get("segment_index") == 1


def test_segment_todo_event_carries_the_segment_number(loop_app):
    """首次挂出的段待办事件带段号——不只快照里有（D7-1 AC1）。"""
    app, _model, root, _captured = loop_app

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        _to_first_segment_todo(client, session_id)

    created = [event for event in _events(root, session_id)
               if event["type"] == "action_item.created"]
    assert created[-1]["payload"]["kind"] == "segment_video"
    assert created[-1]["payload"]["segment_index"] == 1


# ---------------------------------------------------------------------------
# 失败：落 failed，重试不重交素材
# ---------------------------------------------------------------------------

def test_technical_failure_marks_failed_and_retry_needs_no_resubmit(
        loop_app, tmp_path, monkeypatch):
    app, _model, root, _captured = loop_app
    # 读图服务不可用 = 测不出事实 = 系统故障（failed），不是「用户的视频不合格」。
    monkeypatch.setattr(frame_auditor, "describe_bridge_frame", lambda _path: None)
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        asset_id = _upload(client, session_id, video)

        started = _resolve(client, session_id, todo["item_id"],
                           {"video_asset_id": asset_id})
        assert started.status_code == 202, started.text
        job = _wait_job(client, session_id, started.json()["job_id"])
        assert job["status"] == "failed"

        failed = client.get(f"/api/v1/sessions/{session_id}").json()
        assert failed["status"] == "failed"
        seg = failed["segments"][0]
        # 素材 id 留在段上：重试重跑同一次校验，不要求重交。
        assert seg["status"] == "validating"
        assert seg["video_asset_id"] == asset_id

        # 修好读图后直接重试（advance），不重新提交视频。
        monkeypatch.setattr(
            frame_auditor, "describe_bridge_frame",
            lambda _path: "尾帧画面：人物停在门口")
        monkeypatch.setattr(
            segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: True)
        retried = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert retried.status_code == 202, retried.text
        _wait_job(client, session_id, retried.json()["job_id"])
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running"
            and body["segments"][0]["status"] == "passed")

    assert detail["segments"][0]["video_asset_id"] == asset_id
    assert [item["segment_index"] for item in _pending_segment_todos(detail)] == [2]


# ---------------------------------------------------------------------------
# resolve 载荷与素材闸门（422 segment_video_invalid）
# ---------------------------------------------------------------------------

def test_segment_video_rejects_other_payload_kinds(loop_app, tmp_path):
    app, _model, _root, _captured = loop_app
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        asset_id = _upload(client, session_id, video)

        bodies = [
            {"decision": "approve"},                                     # 审查载荷错配
            {"frames": {"first": None, "last": None}},                   # 帧载荷错配
            {"video_asset_id": asset_id,                                 # 段视频不收修订
             "revisions": [{"layer": "画面", "text": "换个角度"}]},
        ]
        for body in bodies:
            resp = _resolve(client, session_id, todo["item_id"], body)
            assert resp.status_code == 422, (body, resp.text)
            assert resp.json()["code"] == "segment_video_invalid", (body, resp.json())

        # 每次都是 422、会话仍留 awaiting_human，同一待办还能重传。
        after = client.get(f"/api/v1/sessions/{session_id}").json()
        assert after["status"] == "awaiting_human"
        assert _pending_segment_todos(after)


def test_segment_video_rejects_foreign_asset(loop_app, tmp_path):
    app, _model, _root, _captured = loop_app
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        other_id = client.post(
            "/api/v1/sessions", json=_brief_body(plot="另一个主题")).json()["session_id"]
        foreign_asset = _upload(client, other_id, video)

        resp = _resolve(client, session_id, todo["item_id"],
                        {"video_asset_id": foreign_asset})

        assert resp.status_code == 422
        assert resp.json()["code"] == "segment_video_invalid"
        assert client.get(
            f"/api/v1/sessions/{session_id}").json()["status"] == "awaiting_human"


def test_segment_video_rejects_non_mp4(loop_app, tmp_path):
    app, _model, _root, _captured = loop_app
    png = tmp_path / "frame.png"
    png.write_bytes(PNG_MAGIC + b"\x00" * 4096)
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        image_asset = _upload(client, session_id, png, media="image/png")

        resp = _resolve(client, session_id, todo["item_id"],
                        {"video_asset_id": image_asset})

        assert resp.status_code == 422
        assert resp.json()["code"] == "segment_video_invalid"
        assert "mp4" in resp.json()["detail"]


def test_segment_video_rejects_wrong_tier(loop_app, tmp_path):
    app, _model, _root, _captured = loop_app
    # 本段计划 6 秒；交一段 4 秒（107 帧）的视频 → check_segment_video 报错。
    wrong = write_h3_tier_mp4(tmp_path / "seg04.mp4", 4)
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        asset_id = _upload(client, session_id, wrong)

        resp = _resolve(client, session_id, todo["item_id"],
                        {"video_asset_id": asset_id})

        assert resp.status_code == 422
        assert resp.json()["code"] == "segment_video_invalid"
        assert "segment_video_duration_mismatch" in resp.json()["detail"]
