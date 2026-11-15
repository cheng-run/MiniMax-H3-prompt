"""D6（#246/#270）：长片段循环的对外面——第三个人工待办与语义提示。

钉住四件事：
1. 规划完自动写第 1 段 → 挂出唯一的 ``segment_video`` 待办（载荷带段号），会话 await。
2. resolve 只收 ``video_asset_id``；错配/外会话素材/非 mp4/技术档不合格一律 422
   ``segment_video_invalid``，会话仍可重传。
3. 语义检查**只提示、不阻断**（#270 / ADR 0039）：有差异或读图不可用时段照样 ``passed``
   并带 ``semantic_warning``，照常写下一段，不重建待办、不发新的段待办事件。
4. 剥尾帧失败是系统故障：落 ``failed``，重试重跑同一次校验、**不要求重交素材**。

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


def _segment_todo_events(events: list[dict], number: int) -> list[dict]:
    """某一 (segment_video, 段号) 的 ``action_item.created`` 事件列表。"""
    return [event for event in events
            if event["type"] == "action_item.created"
            and event["payload"].get("kind") == "segment_video"
            and event["payload"].get("segment_index") == number]


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


def test_pass_records_segment_and_writes_next_with_tail_anchor(
        loop_app, tmp_path, monkeypatch):
    app, _model, root, captured = loop_app
    monkeypatch.setattr(
        frame_auditor, "describe_bridge_frame",
        lambda _path: "尾帧画面：人物已经停在门口，灯光昏黄")
    monkeypatch.setattr(
        segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: "")
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
    # 无差异 → 不写语义提示。
    assert segments[0]["semantic_warning"] == ""
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
    # 剥出来的尾帧是正规产物（#277）：第 2 段的起点图必须能在清单里找到，
    # 内容端点也能读出同一份字节。
    assert listed["segments/bridge_frames/shot-01-start.png"] == "bridge_frame"
    bridge = client.get(
        f"/api/v1/artifacts/{session_id}/segments/bridge_frames/shot-01-start.png/content")
    assert bridge.status_code == 200
    assert bridge.headers["content-type"] == "image/png"
    assert bridge.content.startswith(PNG_MAGIC)


# ---------------------------------------------------------------------------
# 语义差异：段照样通过、带提示、照常写下一段（不重建待办、不发新事件）
# ---------------------------------------------------------------------------

def test_semantic_difference_passes_with_warning_and_writes_next(
        loop_app, tmp_path, monkeypatch):
    app, _model, root, captured = loop_app
    monkeypatch.setattr(
        frame_auditor, "describe_bridge_frame", lambda _path: "人物还在走动")
    monkeypatch.setattr(
        segment_validation, "judge_end_hook",
        lambda _hook, _desc, _llm: "人物没有停在门口，还在向画面深处走")
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        asset_id = _upload(client, session_id, video)
        before = _segment_todo_events(_events(root, session_id), 1)

        started = _resolve(client, session_id, todo["item_id"],
                           {"video_asset_id": asset_id})
        assert started.status_code == 202, started.text
        job = _wait_job(client, session_id, started.json()["job_id"])
        # 语义差异不是系统故障：job 成功、会话继续推进（不留在等人工）。
        assert job["status"] == "succeeded"
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and any(
                seg["status"] == "waiting_video" and seg["index"] == 2
                for seg in body["segments"]))

    seg = detail["segments"][0]
    assert seg["status"] == "passed"
    assert seg["video_asset_id"] == asset_id
    warning = seg["semantic_warning"]
    assert "语义检查提示" in warning
    assert "人物没有停在门口，还在向画面深处走" in warning
    assert "计划末态：人物停在门口" in warning
    assert "尾帧画面：人物还在走动" in warning

    # 下一段写词已起：段 2 转 waiting_video，且第 2 段正文真被写过。
    assert 1 in captured
    assert [item["segment_index"] for item in _pending_segment_todos(detail)] == [2]

    # 不重建第 1 段待办、不发新的段待办事件（只有最初那一条）。
    after = _segment_todo_events(_events(root, session_id), 1)
    assert len(before) == 1 and len(after) == 1


# ---------------------------------------------------------------------------
# 读图/判据不可用：段照样通过，附「语义检查未完成」提示
# ---------------------------------------------------------------------------

def test_semantic_unavailable_passes_with_incomplete_notice(
        loop_app, tmp_path, monkeypatch):
    app, _model, _root, _captured = loop_app

    def unavailable(_path):
        raise RuntimeError("视觉服务不可用")

    monkeypatch.setattr(frame_auditor, "describe_bridge_frame", unavailable)
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
        assert job["status"] == "succeeded", job
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and any(
                seg["status"] == "waiting_video" and seg["index"] == 2
                for seg in body["segments"]))

    seg = detail["segments"][0]
    assert seg["status"] == "passed"  # 不落 failed、也不退回收视频
    assert seg["semantic_warning"].startswith("语义检查未完成：")
    assert "视觉服务不可用" in seg["semantic_warning"]


# ---------------------------------------------------------------------------
# 剥尾帧失败：落 failed，重试重跑同一次校验、不重交素材
# ---------------------------------------------------------------------------

def test_tailframe_failure_marks_failed_and_retry_needs_no_resubmit(
        loop_app, tmp_path, monkeypatch):
    app, _model, _root, _captured = loop_app
    original_extract = frame_auditor.extract_last_frame

    def broken_extract(*_args, **_kwargs):
        raise RuntimeError("剥尾帧失败：读不到视频文件")

    monkeypatch.setattr(frame_auditor, "extract_last_frame", broken_extract)
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
        assert seg["semantic_warning"] == ""  # 没有落 passed，也不写提示

        # 修好剥帧后直接重试（advance），不重新提交视频。
        monkeypatch.setattr(frame_auditor, "extract_last_frame", original_extract)
        monkeypatch.setattr(
            segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: "")
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
