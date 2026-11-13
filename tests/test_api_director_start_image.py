"""D8-1（#261）：导演台开场首帧待办、画幅确认与起跑闸门（spec #253 §4.4 / §7）。

三块行为：

1. ``i2v`` / ``fl2v`` 阶段 1 审查通过后挂 ``director_start_image``；``t2v`` / ``r2v``
   不挂，``base`` / ``ref`` 永不出现这个 kind；
2. 两张图都走素材上传接口；首帧落 ``director/assets/01/start.*``、全片尾帧落
   ``director/assets/film-end.*``；画幅不符收下 + 警告，但决议必须显式确认；
3. ``i2v`` / ``fl2v`` 没有开场首帧就起跑 → 409 ``start_image_required``。

桩管线只替代阶段 1 的产出，其余每一环（审查闸门、待办、素材上传、落盘、起跑闸门）
都走真实代码。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.director.film_canvas import film_frame_canvases
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.image_config import load_frame_image_model
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client

ENV = "MINIMAX_FAKE_PIPELINE"
RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

# 本文件自己定的成片画幅（竖片）。选定值随创建请求写进 ``canvases``，所以断言里的
# 736×1280 是**独立真值**，不是按实现公式重算出来的。
FILM_SIZE = (736, 1280)
# 与成片画幅比例差约 200%：CLI 验收里「锚点被裁」的那一类。
LANDSCAPE_SIZE = (1280, 736)

START_STEM = "start"
FILM_END_STEM = "film-end"


def _canvases(size: tuple[int, int] = FILM_SIZE) -> dict:
    """把成片画幅挂到当前关键帧模型家族键上（键名取自生产派生，值是本文件真值）。"""
    return film_frame_canvases(size, load_frame_image_model())


def _png(size: tuple[int, int], seed: int = 0) -> bytes:
    """造一张**真能读出宽高**的 PNG：画幅判据要拿它比，纯魔数头不够。"""
    import io

    from PIL import Image

    width, height = size
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (seed % 256, 40, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def _wait_job(client, session_id: str, job_id: str, timeout: float = 60.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(
            f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


def _detail(client, session_id: str) -> dict:
    return client.get(f"/api/v1/sessions/{session_id}").json()


def _pending(detail: dict, kind: str) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == kind and item["status"] == "pending"]


def _upload(client, session_id: str, size: tuple[int, int], *, name: str) -> str:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (name, _png(size), "image/png")})
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_id"]


def _start_stage1(client, session_id: str) -> dict:
    """起一次推进并等它停在审查闸门；返回 pending 的 stage1_review 待办。"""
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    job = _wait_job(client, session_id, started.json()["job_id"])
    assert job["status"] == "succeeded", job
    review = _pending(_detail(client, session_id), "stage1_review")
    assert len(review) == 1, review
    return review[0]


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    """开桩的真实 FastAPI + 临时会话根；``build_chat_model`` 一调就炸。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: (_ for _ in ()).throw(AssertionError("桩管线不许建真实模型")))
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, root


def _create_director(client, *, mode: str, plot: str = "雨夜街口的追逐") -> str:
    body = {
        "mode": "director",
        "plot": plot,
        "duration": 4.0,
        "director_mode": mode,
        "director_node_id": "node-42",
        "canvases": _canvases(),
    }
    resp = client.post("/api/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _director_dir(root: Path, session_id: str) -> Path:
    return root / Path(session_id) / "director" / "assets"


@pytest.mark.parametrize("mode", ["i2v", "fl2v"])
def test_i2v_and_fl2v_get_a_start_image_todo_after_review(fake_app, mode):
    app, _root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode=mode)
        review = _start_stage1(client, session_id)

        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})

        # 不产生任务：会话停在原地等开场首帧（与 base 的帧闸门同形）。
        assert resp.status_code == 202, resp.text
        assert resp.json() is None
        detail = _detail(client, session_id)
        assert detail["status"] == "awaiting_human"
        assert len(_pending(detail, "director_start_image")) == 1
        assert _pending(detail, "frame_submission") == []


@pytest.mark.parametrize("mode", ["t2v", "r2v"])
def test_t2v_and_r2v_do_not_get_a_start_image_todo(fake_app, mode):
    app, _root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode=mode)
        review = _start_stage1(client, session_id)

        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})

        assert resp.status_code == 202, resp.text
        assert resp.json() is not None, "这两种模式通过审查后应直接起阶段 2 任务"
        assert "director_start_image" not in {
            item["kind"] for item in _detail(client, session_id)["action_items"]}


def test_base_frame_chain_keeps_frame_submission_and_no_director_kind(fake_app):
    """base 的 FL2VA 仍挂 ``frame_submission``：判据修补不许改变 base 的行为。"""
    app, _root = fake_app
    with workbench_client(app) as client:
        resp = client.post("/api/v1/sessions", json={
            "mode": "base", "variant": "FL2VA", "duration": 4.0,
            "plot": "雨夜旧信"})
        session_id = resp.json()["session_id"]
        review = _start_stage1(client, session_id)

        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})

        detail = _detail(client, session_id)
        assert len(_pending(detail, "frame_submission")) == 1
        assert _pending(detail, "director_start_image") == []


def test_submit_start_and_film_end_images_land_in_the_director_package(fake_app):
    app, root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="fl2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, session_id), "director_start_image")[0]
        start = _upload(client, session_id, FILM_SIZE, name="start.png")
        end = _upload(client, session_id, FILM_SIZE, name="end.png")

        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image",
                  "start_asset_id": start, "end_asset_id": end})

        assert resp.status_code == 202, resp.text
        _wait_job(client, session_id, resp.json()["job_id"])
        assets = _director_dir(root, session_id)
        assert (assets / "01" / f"{START_STEM}.png").is_file()
        assert (assets / f"{FILM_END_STEM}.png").is_file()

        resolved = next(
            row for row in _detail(client, session_id)["action_items"]
            if row["item_id"] == item["item_id"])
        assert resolved["status"] == "resolved"
        assert resolved["decision"] == "submit_start_image"
        # 两个图槽都回显：一个待办收两张图，少一项就说不清用户交的是哪一对。
        assert resolved["start_asset_id"] == start
        assert resolved["end_asset_id"] == end
        # 画幅相符：没有警告。
        assert resolved["reason"] == ""


def test_film_end_is_optional(fake_app):
    app, root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, session_id), "director_start_image")[0]
        start = _upload(client, session_id, FILM_SIZE, name="start.png")

        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image", "start_asset_id": start})

        assert resp.status_code == 202, resp.text
        _wait_job(client, session_id, resp.json()["job_id"])
        assets = _director_dir(root, session_id)
        assert (assets / "01" / f"{START_STEM}.png").is_file()
        assert not (assets / f"{FILM_END_STEM}.png").exists()


def test_aspect_mismatch_is_rejected_until_explicitly_confirmed(fake_app):
    app, root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, session_id), "director_start_image")[0]
        start = _upload(client, session_id, LANDSCAPE_SIZE, name="landscape.png")
        url = (f"/api/v1/sessions/{session_id}/action-items/"
               f"{item['item_id']}/resolve")

        refused = client.post(url, json={
            "decision": "submit_start_image", "start_asset_id": start})
        assert refused.status_code == 409, refused.text
        assert refused.headers["content-type"].startswith("application/problem+json")
        assert refused.json()["code"] == "aspect_mismatch_unconfirmed"
        # 待办仍在：拒绝不许把它悄悄决议掉。
        assert _pending(_detail(client, session_id), "director_start_image")
        assert not _director_dir(root, session_id).exists()

        accepted = client.post(url, json={
            "decision": "submit_start_image", "start_asset_id": start,
            "confirm_aspect_mismatch": True})
        assert accepted.status_code == 202, accepted.text
        _wait_job(client, session_id, accepted.json()["job_id"])

        detail = _detail(client, session_id)
        resolved = next(
            row for row in detail["action_items"] if row["item_id"] == item["item_id"])
        # 警告要有地方可读：落在待办 reason 上（与实际比例、画布比例两个数一起）。
        assert "1280×736" in resolved["reason"]
        assert "736×1280" in resolved["reason"]
        assert (Path(_director_dir(root, session_id)) / "01"
                / f"{START_STEM}.png").is_file()


def test_submit_start_image_requires_an_uploaded_asset_id(fake_app):
    app, _root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, session_id), "director_start_image")[0]
        url = (f"/api/v1/sessions/{session_id}/action-items/"
               f"{item['item_id']}/resolve")

        missing = client.post(url, json={"decision": "submit_start_image"})
        assert missing.status_code == 422, missing.text

        # 本地路径不是素材标识：工作台只认上传接口给的那一串。
        path = client.post(url, json={
            "decision": "submit_start_image",
            "start_asset_id": "D:/tmp/start.png"})
        assert path.status_code == 422, path.text


def test_advance_without_the_start_image_is_409_start_image_required(fake_app):
    """起跑闸门：阶段 1 已过、会话可推进，但开场首帧没就位 → 409。

    真实路径是「审查通过 → 挂待办 → 中断 → 重开会话再点开始」。这里用存储层的
    ``mutate`` 把会话摆到同一个事实状态上（待办挂着、状态 interrupted），而不是
    伪造一个不存在的 API。
    """
    app, _root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        store = app.state.session_store
        store.mutate(session_id, lambda _detail: {"status": "interrupted"})

        resp = client.post(f"/api/v1/sessions/{session_id}/jobs")

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "start_image_required"


def test_advance_passes_once_the_start_image_is_on_disk(fake_app):
    """同一道闸门在首帧就位后放行：它拦的是「没图起跑」，不是这条路线本身。"""
    app, root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, session_id), "director_start_image")[0]
        start = _upload(client, session_id, FILM_SIZE, name="start.png")
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image", "start_asset_id": start})
        _wait_job(client, session_id, accepted.json()["job_id"])
        # 把会话摆回可推进状态，再点一次开始：图在盘上，闸门放行。
        app.state.session_store.mutate(
            session_id, lambda _detail: {"status": "interrupted"})

        resp = client.post(f"/api/v1/sessions/{session_id}/jobs")

        assert resp.status_code == 202, resp.text
        _wait_job(client, session_id, resp.json()["job_id"])


def test_start_image_files_are_not_artifacts(fake_app):
    """``director/assets`` 是打包落点，不是产物登记表里的一件（不串线）。"""
    app, root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        review = _start_stage1(client, session_id)
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, session_id), "director_start_image")[0]
        start = _upload(client, session_id, FILM_SIZE, name="start.png")
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image", "start_asset_id": start})
        _wait_job(client, session_id, accepted.json()["job_id"])

        names = {row["name"] for row in _detail(client, session_id)["artifacts"]}
        # 首帧图落 ``director/assets``（打包落点），不是产物；交图后接着跑的是导演台
        # 阶段 2（#262），它写的 ``director/segment-NN.md`` 才是登记进产物的那一件。
        assert not any(name.startswith("director/assets/") for name in names)
        assert "director/segment-01.md" in names
        assert json.loads(
            (root / Path(session_id) / SESSION_FILENAME).read_text(encoding="utf-8")
        )["brief"]["director_mode"] == "i2v"


def test_both_image_slots_survive_snapshot_replay(fake_app):
    """一个待办收两张图：崩溃补齐后两个 id 都要在（D7-1 的载荷纪律，D8 §7.1）。

    快照落后时按事件重放；决议事件里带的是一对 ``start_asset_id`` / ``end_asset_id``。
    漏一个的后果不是报错，而是重放后待办只剩「交过图」，说不清交的是哪两张。
    """
    from minimax_h3_prompt.workbench_store import EventDraft, EventLog

    app, root = fake_app
    start_id = "雨夜街口的追逐/GEN001/0123456789abcdef"
    end_id = "雨夜街口的追逐/GEN001/fedcba9876543210"
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="fl2v")
        log = EventLog(root / Path(session_id), session_id)
        log.append([
            EventDraft("job.started", {"kind": "advance"}, job_id="j1"),
            EventDraft("job.succeeded", {"next": "awaiting_human"}, job_id="j1"),
            EventDraft("action_item.created",
                       {"item_id": "i1", "kind": "director_start_image"}),
        ])
        log.append([EventDraft("action_item.resolved", {
            "item_id": "i1", "decision": "submit_start_image",
            "start_asset_id": start_id, "end_asset_id": end_id,
        })])

        detail = _detail(client, session_id)

    item = next(row for row in detail["action_items"] if row["item_id"] == "i1")
    assert item["status"] == "resolved"
    assert item["start_asset_id"] == start_id
    assert item["end_asset_id"] == end_id


def test_replayed_start_image_item_keeps_the_empty_end_slot_empty(fake_app):
    """只交首帧时重放不许编一个尾帧 id 出来。"""
    from minimax_h3_prompt.workbench_store import EventDraft, EventLog

    app, root = fake_app
    with workbench_client(app) as client:
        session_id = _create_director(client, mode="i2v")
        log = EventLog(root / Path(session_id), session_id)
        log.append([
            EventDraft("job.started", {"kind": "advance"}, job_id="j1"),
            EventDraft("job.succeeded", {"next": "awaiting_human"}, job_id="j1"),
            EventDraft("action_item.created",
                       {"item_id": "i1", "kind": "director_start_image"}),
        ])
        log.append([EventDraft("action_item.resolved", {
            "item_id": "i1", "decision": "submit_start_image",
            "start_asset_id": f"{session_id}/0123456789abcdef",
        })])

        item = next(
            row for row in _detail(client, session_id)["action_items"]
            if row["item_id"] == "i1")

    assert item["end_asset_id"] is None
