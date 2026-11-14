"""D8-8（#268）：桩管线下 ``r2v`` 的整条闭环——素材循环 → 规划/写作 → 打包 → 段视频 → 交付。

判据全在 HTTP 面与盘上产物，桩只替「谁写/谁判」：

- 素材循环、台账转移、待办重开、规划产物落盘、预算与子集校验、打包、段校验、交付
  归档全部走**真实代码**；
- ``build_chat_model`` 一调就炸、``describe_image`` 不被调用（``MINIMAX_FAKE_PIPELINE``）。

``r2v`` 与 ``ref`` 的差别正是本票的两条：开场图来自台账（不挂 ``director_start_image``），
素材循环跑完才进导演台阶段 2（规划），且规划前要有全片英文定义块与段内素材预算。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api import fake_pipeline
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.director.planning import (
    asset_budget_path,
    load_asset_budget,
    plan_path,
    read_plan_json,
)
from minimax_h3_prompt.director.prompts import (
    load_segment_assets,
    segment_path,
)
from minimax_h3_prompt.director.refdefs import load_ref_sections, sections_path
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.workbench_store import EVENT_TYPES
from workbench_client import workbench_client

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0,
    director_settings=lambda: __import__(
        "minimax_h3_prompt.director.pack", fromlist=["DirectorPackSettings"],
    ).DirectorPackSettings(
        width=736, height=1280, ref_max_size=864, ref_image_size="match"),
    director_comfy_output_root=lambda: Path("unused-comfy-output"),
)
ENV = "MINIMAX_FAKE_PIPELINE"
CAT = "橘猫"
LAMP = "台灯"


class _NoModel:
    def __call__(self):  # pragma: no cover - 被调到就是失败
        raise AssertionError("桩管线不许建真实模型")


def _png(seed: int = 0) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (seed % 256, 40, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def _wait_until(client, session_id: str, predicate, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        if predicate(detail):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{session_id}")


def _wait_job(client, session_id: str, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(
            f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


def _wait_idle(client, session_id: str, timeout: float = 60.0) -> dict:
    def idle(body: dict) -> bool:
        return body["status"] != "running" and not any(
            job["status"] == "running" for job in body["jobs"])
    return _wait_until(client, session_id, idle, timeout)


def _pending(detail: dict, kind: str) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == kind and item["status"] == "pending"]


def _upload(client, session_id: str, payload: bytes, filename: str,
            *, name: str | None = None, content_type: str = "image/png") -> dict:
    files = {"file": (filename, payload, content_type)}
    data = {} if name is None else {"name": name}
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets", files=files, data=data)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _resolve(client, session_id: str, item_id: str, body: dict) -> dict | None:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve", json=body)
    assert resp.status_code == 202, resp.text
    return resp.json()


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(model_factory, "build_chat_model", _NoModel())
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, root


def _drive_asset_loop_to_planning(client, session_id: str, detail: dict) -> dict:
    """素材循环：第 1 件被拒 → 同号重开 → 重交通过 → 第 2 件一次通过。"""
    todo = [item for item in _pending(detail, "asset_image")
            if item["asset_index"] == 1][0]
    first_image = _upload(client, session_id, _png(11), "cat-1.png")
    _resolve(client, session_id, todo["item_id"],
             {"asset_id": first_image["asset_id"]})

    detail = _wait_until(
        client, session_id,
        lambda body: [item for item in _pending(body, "asset_image")
                      if item["asset_index"] == 1 and item["reason"]])
    reopened = [item for item in _pending(detail, "asset_image")
                if item["asset_index"] == 1][0]
    assert "差异" in reopened["reason"]

    second_image = _upload(client, session_id, _png(12), "cat-2.png")
    _resolve(client, session_id, reopened["item_id"],
             {"asset_id": second_image["asset_id"]})
    detail = _wait_until(
        client, session_id,
        lambda body: any(item["asset_index"] == 2
                         for item in _pending(body, "asset_image")))

    third_image = _upload(client, session_id, _png(13), "lamp-1.png")
    todo2 = [item for item in _pending(detail, "asset_image")
             if item["asset_index"] == 2][0]
    _resolve(client, session_id, todo2["item_id"],
             {"asset_id": third_image["asset_id"]})
    # 素材循环收尾后自动接阶段 2（规划 + 写段）与打包；等它停稳。
    return _wait_idle(client, session_id)


def test_fake_r2v_closed_loop_plans_budgets_packs_and_delivers(
    fake_app, tmp_path,
):
    app, root = fake_app
    assert app.state.job_runner.fake_pipeline is True

    with workbench_client(app) as client:
        created = client.post("/api/v1/sessions", json={
            "mode": "director", "variant": "T2VA", "duration": 4.0,
            "style": "写实", "language": "Chinese", "plot": "雨夜旧信的 r2v 闭环",
            "director_mode": "r2v", "director_node_id": "node-r2v"})
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        registered_cat = _png(1)
        _upload(client, session_id, registered_cat, "cat.png", name=CAT)
        _upload(client, session_id, _png(2), "lamp.png", name=LAMP)

        started = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert started.status_code == 202, started.text
        detail = _wait_until(
            client, session_id, lambda body: _pending(body, "stage1_review"))
        review = _pending(detail, "stage1_review")[0]

        approved = _resolve(client, session_id, review["item_id"],
                            {"decision": "approve"})
        # 通过审查直接进素材循环（r2v 的开场图来自台账，不挂开场首帧待办）。
        assert approved["kind"] == "asset_loop"
        detail = _wait_until(
            client, session_id, lambda body: _pending(body, "asset_image"))
        assert not _pending(detail, "director_start_image")
        assert all(item["kind"] != "director_start_image"
                   for item in detail["action_items"])

        detail = _drive_asset_loop_to_planning(client, session_id, detail)

        directory = root / Path(session_id)
        # 1) 规划产物：段表、预算、英文定义块各一份，段词逐段落盘。
        plan = read_plan_json(plan_path(directory))
        assert plan.mode == "r2v"
        assert segment_path(directory, 1).is_file()
        budget = load_asset_budget(directory)
        assert asset_budget_path(directory).is_file()
        budget_entry = budget.for_segment(1)
        assert budget_entry is not None
        assert [picture.name for picture in budget_entry.pictures] == [CAT, LAMP]

        sections = load_ref_sections(directory)
        assert sections_path(directory).is_file()
        assert sections is not None
        assert sections.subject_definitions.strip()
        assert sections.retention_analysis.strip()
        assert sections.registry == ((1, CAT), (2, LAMP))
        # ``r2v`` 的段内定义必须来自这份英文块，而不是阶段 1 的中文 ``subject_defs``。
        word = segment_path(directory, 1).read_text(encoding="utf-8")
        assert "subject_definitions:" in word and "retention_analysis:" in word
        assert sections.retention_analysis.splitlines()[0] in word

        # 2) 段内素材子集 ⊆ 预算，并在段表上只读可见（重编号后的槽号 + 语义名）。
        # 桩段词只点名了 <Picture 1>，真实闭包算出「本段只用第 1 张」——少于预算
        # 是合法的（只提示不阻断），段表就照这份真实子集显示。
        scopes = load_segment_assets(directory)
        scoped = scopes[1]
        allowed = {(picture.number, picture.name)
                   for picture in budget_entry.pictures}
        assert {(entry["original"], entry["name"]) for entry in scoped} <= allowed
        assert 0 < len(scoped) < len(allowed)
        assert [(row["number"], row["name"],
                 row["original_number"]) for row in detail["segments"][0]["assets"]] == [
            (1, CAT, 1)]
        assert detail["segments"][0]["segment_type"] == "opening"

        # 3) 初始包已打好（打包照子集挂图）：挂上的是**台账里那张正本**，
        #    而不是素材循环里用户交回的校对图——开场/参考图确实来自台账。
        pack_path = directory / "director" / "initial.mmxpack.zip"
        assert pack_path.is_file()
        import zipfile

        with zipfile.ZipFile(pack_path) as archive:
            names = archive.namelist()
            assert "asset_groups/01/Picture1.png" in names
            assert "asset_groups/01/Picture2.png" not in names
            assert archive.read("asset_groups/01/Picture1.png") == registered_cat
        assert any(item["kind"] == "director_pack"
                   for item in detail["artifacts"])

        # 4) 交本段视频（桩导出帧数＝段表的导出帧）→ 挂交付待办 → 交付归档。
        export = plan.segments[0].export_frames
        video = fake_pipeline.write_director_segment_video(
            tmp_path / "seg-01.mp4", export)
        video_asset = _upload(client, session_id, video.read_bytes(),
                              video.name, content_type="video/mp4")
        todo = _pending(detail, "segment_video")[0]
        _resolve(client, session_id, todo["item_id"],
                 {"video_asset_id": video_asset["asset_id"]})
        detail = _wait_until(
            client, session_id,
            lambda body: _pending(body, "director_delivery"))

        # 最终成片 = 整片的导出帧（单段时就是这一段）。
        film = fake_pipeline.write_director_segment_video(
            tmp_path / "film.mp4", plan.total_frames)
        pack_bytes = (directory / "director" / "initial.mmxpack.zip").read_bytes()
        film_asset = _upload(client, session_id, film.read_bytes(),
                             film.name, content_type="video/mp4")
        pack_asset = _upload(client, session_id, pack_bytes, "final.zip",
                             content_type="application/zip")
        delivery = _pending(detail, "director_delivery")[0]
        _resolve(client, session_id, delivery["item_id"], {
            "decision": "submit_delivery",
            "video_asset_id": film_asset["asset_id"],
            "pack_asset_id": pack_asset["asset_id"]})
        detail = _wait_idle(client, session_id)

    assert detail["status"] == "completed"
    assert detail["segments"][0]["status"] == "passed"
    final_film = directory / "final.mp4"
    assert final_film.is_file()

    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()]
    types = [event["type"] for event in events]
    assert set(types) <= set(EVENT_TYPES)
    assert len(EVENT_TYPES) == 10
    assert "job.failed" not in types
