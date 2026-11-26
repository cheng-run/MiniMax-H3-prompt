"""#368：工作台交帧图的尺寸回执（票面 Testing Decisions 缝 3）。

测点是最外层的工作台决议缝：交帧图后**待办回执里能读到**尺寸提示，且帧照常收下、
阶段 2 照常推进——提示不改变任何通过的结论。判据本身是 #365 的
``segment_validation.frame_canvas_warning_text``（三种结论已在
``tests/test_frame_canvas_hint.py`` 钉住），这里只测「它有没有被接上工作台」以及
「接上之后流程有没有被改变」。

期望画布钉成字面量（``aspect_flow.frame_canvas`` 打桩）：用例不随
``config/image.yaml`` 的 ``frame:`` 档漂。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from minimax_h3_prompt.director import aspect_flow

from test_api_frame_submission import (
    _advance_to_gate,
    _events,
    _resolve,
    _state,
    _upload,
    _wait_job,
    workbench_client,
)
# 复用同一条会话纵切的夹具。ruff 的 F811 会把「同名导入 + 函数参数」当成重复定义，
# 所以这里用 pytest 的按名取用口包一层，参数名换成本文件的 ``frame_session``。
from test_api_frame_submission import workbench as _workbench_session  # noqa: F401


@pytest.fixture
def frame_session(request):
    return request.getfixturevalue('_workbench_session')


EXPECTED = (640, 1280)


def _png_bytes(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _broken_png_bytes() -> bytes:
    """魔数对、内容坏：上传口只认魔数，读图判据才量得出「量不出来」。"""
    return b"\x89PNG\r\n\x1a\nnot-really-a-png"


@pytest.fixture
def frame_canvas(monkeypatch):
    monkeypatch.setattr(aspect_flow, "frame_canvas", lambda canvases=None: EXPECTED)
    return EXPECTED


def _to_frame_gate(client) -> tuple[str, dict]:
    detail, review = _advance_to_gate(client, variant="FL2VA")
    session_id = detail["session_id"]
    resp = _resolve(client, session_id, review["item_id"], {"decision": "approve"})
    assert resp.status_code == 202, resp.text
    gate = client.get(f"/api/v1/sessions/{session_id}").json()
    item = [entry for entry in gate["action_items"]
            if entry["status"] == "pending"][0]
    assert item["kind"] == "frame_submission"
    return session_id, item


def _resolved_frame_item(detail: dict) -> dict:
    return [entry for entry in detail["action_items"]
            if entry["kind"] == "frame_submission"][-1]


def test_mismatch_lands_in_the_receipt_and_stage2_still_runs(frame_session, frame_canvas):
    app, _model, _root = frame_session
    with workbench_client(app) as client:
        session_id, item = _to_frame_gate(client)
        first = _upload(client, session_id, "first.png", _png_bytes((320, 240)))
        last = _upload(client, session_id, "last.png", _png_bytes((641, 1281)))

        started = _resolve(client, session_id, item["item_id"], {
            "frames": {"first": first["asset_id"], "last": last["asset_id"]}})
        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert done["status"] == "succeeded", done
    # 提示不阻断：帧照常收下、阶段 2 照常推进到完成。
    assert final["status"] == "completed"
    assert final["brief"]["variant"] == "FL2VA"
    receipt = _resolved_frame_item(final)["reason"]
    # 两张都不符：两边数值都要在回执里（「我该改哪一边」靠的就是两边数字）。
    assert "320×240" in receipt and "641×1281" in receipt, receipt
    assert f"{EXPECTED[0]}×{EXPECTED[1]}" in receipt, receipt


def test_matching_sizes_keep_the_receipt_quiet(frame_session, frame_canvas):
    app, _model, _root = frame_session
    with workbench_client(app) as client:
        session_id, item = _to_frame_gate(client)
        first = _upload(client, session_id, "first.png", _png_bytes(EXPECTED))

        started = _resolve(client, session_id, item["item_id"],
                           {"frames": {"first": first["asset_id"]}})
        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert done["status"] == "succeeded", done
    assert final["status"] == "completed"
    # 相符时一声不响：回执里不许长出噪声。
    assert _resolved_frame_item(final)["reason"] == ""


def test_unreadable_image_is_measured_not_mismatched(frame_session, frame_canvas):
    app, _model, _root = frame_session
    with workbench_client(app) as client:
        session_id, item = _to_frame_gate(client)
        first = _upload(client, session_id, "first.png", _broken_png_bytes())

        started = _resolve(client, session_id, item["item_id"],
                           {"frames": {"first": first["asset_id"]}})
        assert started.status_code == 202, started.text
        done = _wait_job(client, session_id, started.json()["job_id"])
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    assert done["status"] == "succeeded", done
    assert final["status"] == "completed"
    receipt = _resolved_frame_item(final)["reason"]
    assert "量不出" in receipt, receipt
    assert "不符" not in receipt, "读不出宽高被当成了尺寸不符"


def test_each_frame_gets_its_own_line(frame_session, frame_canvas):
    """首帧不符、尾帧相符：回执里只提不符的那一张，且点名是哪一帧。"""
    app, _model, root = frame_session
    with workbench_client(app) as client:
        session_id, item = _to_frame_gate(client)
        first = _upload(client, session_id, "first.png", _png_bytes((320, 240)))
        last = _upload(client, session_id, "last.png", _png_bytes(EXPECTED))

        started = _resolve(client, session_id, item["item_id"], {
            "frames": {"first": first["asset_id"], "last": last["asset_id"]}})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])
        final = client.get(f"/api/v1/sessions/{session_id}").json()

    receipt = _resolved_frame_item(final)["reason"]
    assert "320×240" in receipt, receipt
    assert "首帧" in receipt, "提示没说清是哪一帧不符"
    assert "尾帧" not in receipt, f"相符的那一帧不该被点名：{receipt}"
    # 帧提交交接键照旧被任务摘掉（提示不改这一层语义）。
    assert "_frame_submission" not in _state(root, session_id)["stage_state"]


def test_hint_rides_the_resolve_event_too(frame_session, frame_canvas):
    """快照落后时重放要能把回执补回去：提示随决议事件走（事件类型不新增）。"""
    app, _model, root = frame_session
    with workbench_client(app) as client:
        session_id, item = _to_frame_gate(client)
        first = _upload(client, session_id, "first.png", _png_bytes((320, 240)))

        started = _resolve(client, session_id, item["item_id"],
                           {"frames": {"first": first["asset_id"]}})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

    resolved = [event for event in _events(root, session_id)
                if event["type"] == "action_item.resolved"
                and event["payload"]["item_id"] == item["item_id"]]
    assert resolved, "决议事件没有落盘"
    assert "320×240" in resolved[-1]["payload"].get("reason", ""), resolved[-1]
    # 事件类型仍是十种闭合集合：回执只是已决议事件上的一个字段。
    from minimax_h3_prompt.workbench_store import EVENT_TYPES

    assert {event["type"] for event in _events(root, session_id)} <= set(EVENT_TYPES)
