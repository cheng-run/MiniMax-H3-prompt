"""D8-8（#268）：导演台 ``r2v`` 复用 D7 的素材台账与素材循环。

判据分两层，全部走 HTTP 面（与浏览器同一套服务）：

1. **台账复用**：语义名、编号连续、容量闸、起跑锁、删除只撤登记——两条路线
   （``ref`` 与 ``director/r2v``）走同一份实现，base 与 t2v/i2v/fl2v 仍拒语义名；
2. **素材循环接线**：r2v 通过审查后先跑素材循环（``asset_image`` 待办），跑完才
   进导演台阶段 2——不挂开场首帧待办（参考素材是它的开场图来源）。

桩管线全闭环（规划/预算/子集/打包/交付）在 ``test_api_fake_pipeline_director_r2v``
里；本文件只钉路由判据与循环入口，模型一次都不建。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"
WAV_MAGIC = b"RIFF\x00\x00\x00\x00WAVEfmt "
CAT = "橘猫"
LAMP = "台灯"


def _png(seed: int = 0) -> bytes:
    return PNG_MAGIC + bytes([seed % 256]) * 4096


def _mp4(seed: int = 0) -> bytes:
    return MP4_MAGIC + bytes([seed % 256]) * 4096


def _wav(seed: int = 0) -> bytes:
    return WAV_MAGIC + bytes([seed % 256]) * 4096


def _client(tmp_path: Path) -> TestClient:
    return workbench_client(create_app(sessions_root=tmp_path))


def _create(client: TestClient, **overrides) -> dict:
    body = {
        "mode": "director",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信的 r2v 闭环",
        "director_mode": "r2v",
        "director_node_id": "node-r2v",
        **overrides,
    }
    resp = client.post("/api/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload(client: TestClient, session_id: str, payload: bytes, filename: str,
            *, name: str | None = None, content_type: str = "image/png"):
    files = {"file": (filename, payload, content_type)}
    data = {} if name is None else {"name": name}
    return client.post(f"/api/v1/sessions/{session_id}/assets",
                       files=files, data=data)


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == code
    return body


def _refs(client: TestClient, session_id: str) -> list[tuple[int, str]]:
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    return [(ref["number"], ref["name"]) for ref in detail["brief"]["refs"]]


def _session_file(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / Path(session_id) / SESSION_FILENAME


# ---------------------------------------------------------------------------
# 台账复用：上传、编号、起跑锁、删除
# ---------------------------------------------------------------------------

def test_r2v_registers_named_pictures_exactly_like_ref(tmp_path):
    """语义名必填规则 + 同类编号从 1 起连续：与 ref 同一份实现。"""
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]

    first = _upload(client, session_id, _png(1), "cat.png", name=CAT)
    assert first.status_code == 201, first.text
    assert first.json()["number"] == 1
    second = _upload(client, session_id, _png(2), "lamp.png", name=LAMP)
    assert second.status_code == 201, second.text
    assert second.json()["number"] == 2

    assert _refs(client, session_id) == [(1, CAT), (2, LAMP)]

    # 同名再传：语义名会话内唯一。
    duplicate = _upload(client, session_id, _png(3), "cat-2.png", name=CAT)
    _assert_problem(duplicate, 422, "asset_name_invalid")


def test_r2v_delete_only_revokes_the_registration_and_renumbers(tmp_path):
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]
    first = _upload(client, session_id, _png(1), "cat.png", name=CAT).json()
    _upload(client, session_id, _png(2), "lamp.png", name=LAMP)

    deleted = client.delete(
        f"/api/v1/sessions/{session_id}/assets/{first['asset_id']}")
    assert deleted.status_code == 204, deleted.text
    # 删掉第 1 件之后同类编号压回连续（从 1 起）。
    assert _refs(client, session_id) == [(1, LAMP)]

    missing = client.delete(
        f"/api/v1/sessions/{session_id}/assets/{first['asset_id']}")
    _assert_problem(missing, 404, "asset_not_found")


def test_r2v_capacity_and_type_sniffing_match_ref(tmp_path):
    """9/3/3/12 与类型嗅探都是 D7 那一份：r2v 不另立一套判据。"""
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]

    picture = _upload(client, session_id, _png(1), "a.png", name="图1")
    video = _upload(client, session_id, _mp4(1), "b.mp4", name="视频1",
                    content_type="video/mp4")
    audio = _upload(client, session_id, _wav(1), "c.wav", name="音频1",
                    content_type="audio/wav")
    assert picture.status_code == video.status_code == audio.status_code == 201
    # 编号按**同类**从 1 起，不是全局计数。
    assert (picture.json()["number"], video.json()["number"],
            audio.json()["number"]) == (1, 1, 1)
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    assert [(ref["kind"], ref["number"], ref["name"])
            for ref in detail["brief"]["refs"]] == [
        ("picture", 1, "图1"), ("video", 1, "视频1"), ("audio", 1, "音频1")]

    # 9 图上限：第 10 张同步 422（与 ref 同一份 ``check_reference_capacity``）。
    for index in range(2, 10):
        resp = _upload(client, session_id, _png(index + 10), f"p{index}.png",
                       name=f"图{index}")
        assert resp.status_code == 201, resp.text
    tenth = _upload(client, session_id, _png(99), "tenth.png", name="图10")
    _assert_problem(tenth, 422, "ref_capacity_exceeded")
    assert len([ref for ref in client.get(
        f"/api/v1/sessions/{session_id}").json()["brief"]["refs"]
        if ref["kind"] == "picture"]) == 9


def test_semantic_name_is_still_rejected_off_the_ledger_routes(tmp_path):
    """base 与导演台 t2v/i2v/fl2v 没有参考素材：带语义名一律 422。"""
    client = _client(tmp_path)
    cases = [
        {"mode": "base", "variant": "T2VA"},
        {"mode": "director", "director_mode": "t2v", "director_node_id": "n"},
        {"mode": "director", "director_mode": "i2v", "director_node_id": "n"},
    ]
    for body in cases:
        session_id = client.post("/api/v1/sessions", json={
            "duration": 4.0, "plot": "雨夜旧信", "style": "写实",
            "language": "Chinese", **body}).json()["session_id"]
        resp = _upload(client, session_id, _png(1), "cat.png", name=CAT)
        _assert_problem(resp, 422, "asset_name_invalid")


def test_r2v_requires_one_picture_before_starting(tmp_path):
    """零素材起跑被挡在 409：r2v 的开场图来自台账，没有图就没有可写的闭包。"""
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]

    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    _assert_problem(started, 409, "asset_required")


def test_r2v_ledger_locks_once_the_session_starts(tmp_path):
    """起跑即锁：已写好的资产图词基于当时的台账，事后加删会让词与图对不上。"""
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]
    first = _upload(client, session_id, _png(1), "cat.png", name=CAT).json()

    session_file = _session_file(tmp_path, session_id)
    raw = json.loads(session_file.read_text(encoding="utf-8"))
    raw["status"] = "running"
    session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    locked_upload = _upload(client, session_id, _png(2), "lamp.png", name=LAMP)
    _assert_problem(locked_upload, 409, "asset_locked")
    locked_delete = client.delete(
        f"/api/v1/sessions/{session_id}/assets/{first['asset_id']}")
    _assert_problem(locked_delete, 409, "asset_locked")


@pytest.mark.parametrize("mode", ["t2v", "i2v", "fl2v"])
def test_director_non_r2v_sessions_have_no_asset_ledger(tmp_path, mode):
    """其余导演台模式删素材回 route_mismatch：不是「不许删」，是「没有这本台账」。"""
    client = _client(tmp_path)
    session_id = _create(client, director_mode=mode)["session_id"]
    resp = client.delete(f"/api/v1/sessions/{session_id}/assets/{session_id}/deadbeef")
    _assert_problem(resp, 409, "route_mismatch")


# ---------------------------------------------------------------------------
# 全片英文定义块：只生成一次，登记表一致就复用
# ---------------------------------------------------------------------------

@pytest.fixture
def deterministic(tmp_path, monkeypatch):
    """真实 app + 确定性模型桩：定义块走真实 ``build_ref_sections``（只换谁写）。"""
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.api import jobs as jobs_module
    from minimax_h3_prompt.graph import pipeline
    from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel(replies=dict(DEFAULT_REPLIES))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    monkeypatch.setattr(jobs_module, "build_chat_model", lambda: model)
    app = create_app(sessions_root=tmp_path)
    return app, model, tmp_path


def test_ref_sections_are_generated_once_and_cached_until_registry_changes(
    deterministic,
):
    from minimax_h3_prompt.api.jobs import JobRunner
    from minimax_h3_prompt.director.refdefs import load_ref_sections, sections_path
    from minimax_h3_prompt.session_store import load_session

    app, model, root = deterministic
    runner = app.state.job_runner
    assert isinstance(runner, JobRunner)

    with workbench_client(app) as client:
        session_id = _create(client)["session_id"]
        first = _upload(client, session_id, _png(1), "cat.png", name=CAT).json()
        _upload(client, session_id, _png(2), "lamp.png", name=LAMP)
        directory = root / Path(session_id)
        session = load_session(directory)

        registry, sections = runner._director_ref_context(
            session, session.stage_state, directory, "r2v")
        assert registry == ((1, CAT), (2, LAMP))
        assert model.calls.count("director_ref_sections") == 1
        assert sections.subject_definitions.strip()
        # 定义块是英文改写结果，不是阶段 1 的中文 ``subject_defs``。
        assert "一致性核对" not in sections.subject_definitions
        assert sections.retention_analysis.strip()

        # 同一份登记表再进一次（续跑/重规划）：读盘复用，不再付一次调用。
        again_registry, again = runner._director_ref_context(
            session, session.stage_state, directory, "r2v")
        assert again_registry == registry
        assert again == sections
        assert model.calls.count("director_ref_sections") == 1
        assert load_ref_sections(directory) == sections
        assert sections_path(directory).is_file()

        # 台账变了 → 旧定义的编号/语义名对不上，必须重写一遍。
        client.delete(
            f"/api/v1/sessions/{session_id}/assets/{first['asset_id']}")
        refreshed = load_session(directory)
        new_registry, new_sections = runner._director_ref_context(
            refreshed, refreshed.stage_state, directory, "r2v")
        assert new_registry == ((1, LAMP),)
        assert new_sections.registry == ((1, LAMP),)
        assert model.calls.count("director_ref_sections") == 2


def test_planner_request_sees_the_ledger_and_the_english_definitions(
    deterministic,
):
    """规划请求里必须有素材清单与英文定义块（#186 的接线，r2v 由 #268 接上）。

    判据取**出站请求正文**：只断言返回对象，漏了「真喂进模型」也看不出来
    （本仓「规则在、接线不在」的老形态）。
    """
    import threading

    from minimax_h3_prompt.api.jobs import JobRunner
    from minimax_h3_prompt.session_store import load_session

    app, model, root = deterministic
    runner = app.state.job_runner
    assert isinstance(runner, JobRunner)

    with workbench_client(app) as client:
        session_id = _create(client)["session_id"]
        _upload(client, session_id, _png(1), "cat.png", name=CAT)
        _upload(client, session_id, _png(2), "lamp.png", name=LAMP)
        directory = root / Path(session_id)
        session = load_session(directory)
        shot_table = (
            "[Shot 1] At 00:00.000, a medium shot of 橘猫 <Picture 1> and "
            "台灯 <Picture 2> in the rainy street.")

        plan, context = runner._plan_director_project(
            session_id, "job-plan", threading.Event(), session,
            dict(session.stage_state), shot_table, "r2v", directory)

    assert plan.mode == "r2v"
    assert context.registry == ((1, CAT), (2, LAMP))
    assert context.sections is not None
    assert context.budget is not None
    # 定义块是规划**之前**由真实构建口产出的（模型角色可数）。
    assert "director_ref_sections" in model.calls
    planner_request = next(
        body for body, role in zip(model.requests, model.calls)
        if role == "director_planner")
    assert "<Picture 1> = 橘猫" in planner_request
    assert "<Picture 2> = 台灯" in planner_request


