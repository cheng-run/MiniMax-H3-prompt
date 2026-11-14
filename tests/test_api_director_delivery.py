"""D8-6（#266）：交付归档、最终包比对与交付闸门（spec #253 §12 / §13 / §15）。

五层判据：

1. **交付触发**：全部段 ``passed`` 之后挂 ``director_delivery`` 待办（不是「悄悄等
   人工」），载荷带成片 mp4 与最终包 zip 两个素材 id；
2. **同步校验**：最终包段数与计划不一致 / 包不可解 / 缺 ``timeline.json`` → 422
   ``archive_invalid``，**不归档**（会话仍在同一待办上）；
3. **成片宽高**：判据只走 ``director.aspect_flow.final_film_verdict``（``deliver``
   不查宽高）；判不过 → 422 ``final_film_mismatch``，同样不归档；
4. **归档与比对**：三件齐备（最终包 / 校验值 / 成片记录），差异以 ``segments[].id``
   为 join 键补 ``rewrite`` 行；``final-film.json`` 带宽高；
5. **事件闭集**：十种事件类型不破。
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api import fake_pipeline
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.director.frames import (
    DirectorPlan,
    DirectorSegment,
    give_images_for,
    is_guided,
)
from minimax_h3_prompt.director.pack import DirectorPackSettings
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.workbench_store import EVENT_TYPES
from workbench_client import workbench_client

ENV = "MINIMAX_FAKE_PIPELINE"
RUNTIME_TOLERANCE = 5


def _segment(index: int, segment_type: str, start: int, declared: int,
             export: int | None = None) -> DirectorSegment:
    guided = is_guided(segment_type)
    export = export if export is not None else declared
    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=start + export,
        declared_frames=declared, export_frames=export,
        summary=f"第 {index} 段（桩）", end_hook=f"第 {index} 段落定画面（桩）",
        give_images=tuple(give_images_for(segment_type, "fl2v")),
    )


# fl2v 三段（开场 / 切镜 / 收尾），帧全部落在 17k+5 网格上；ending 有引导、导出 119。
PLAN = DirectorPlan(mode="fl2v", total_frames=333, segments=(
    _segment(1, "opening", 0, 107),
    _segment(2, "camera_cut", 107, 107),
    _segment(3, "ending", 214, 107, export=119),
))
EXPECTED_EXPORTS = [107, 107, 119]
EXPECTED_MERGED = [107, 214, 333]


def _runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0,
        director_settings=lambda: DirectorPackSettings(
            width=736, height=1280, ref_max_size=864, ref_image_size="match"),
        director_comfy_output_root=lambda: tmp_path / "comfy-output",
        director_frame_tolerance=lambda: RUNTIME_TOLERANCE,
    )


def _png(size: tuple[int, int] = (736, 1280)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 60, 100)).save(buffer, format="PNG")
    return buffer.getvalue()


def _mp4(tmp_path: Path, frames: int, tag: str) -> bytes:
    path = tmp_path / f"{tag}.mp4"
    fake_pipeline.write_director_segment_video(path, frames)
    return path.read_bytes()


def _upload(client, session_id: str, name: str, payload: bytes,
            media: str) -> str:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (name, payload, media)})
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_id"]


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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        if detail["status"] != "running" and not any(
                job["status"] == "running" for job in detail["jobs"]):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"会话没有在 {timeout}s 内停下：{session_id}")


def _detail(client, session_id: str) -> dict:
    return client.get(f"/api/v1/sessions/{session_id}").json()


def _pending(detail: dict, kind: str) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == kind and item["status"] == "pending"]


def _resolve(client, session_id: str, item_id: str, body: dict):
    return client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve", json=body)


def _build_app(tmp_path: Path, monkeypatch, *, patch_plan: bool = True):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: (_ for _ in ()).throw(AssertionError("桩管线不许建真实模型")))
    if patch_plan:
        monkeypatch.setattr(fake_pipeline, "director_plan",
                            lambda mode, total_s: PLAN)
    root = tmp_path / "sessions"
    return create_app(sessions_root=root, runtime_config=_runtime(tmp_path)), root


def _create(client, duration: float = 14.375) -> str:
    response = client.post("/api/v1/sessions", json={
        "mode": "director", "plot": "雨夜街口的追逐", "duration": duration,
        "director_mode": "fl2v", "director_node_id": "node-42", "canvases": {}})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _start_stage1(client, session_id: str) -> None:
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    assert _wait_job(
        client, session_id, started.json()["job_id"])["status"] == "succeeded"


def _submit_start_image(client, session_id: str) -> None:
    review = _pending(_detail(client, session_id), "stage1_review")[0]
    approved = _resolve(client, session_id, review["item_id"], {"decision": "approve"})
    assert approved.status_code == 202, approved.text
    start_todo = _pending(_detail(client, session_id), "director_start_image")[0]
    start = _upload(client, session_id, "start.png", _png(), "image/png")
    submitted = _resolve(client, session_id, start_todo["item_id"], {
        "decision": "submit_start_image", "start_asset_id": start})
    assert submitted.status_code == 202, submitted.text
    assert _wait_job(
        client, session_id, submitted.json()["job_id"])["status"] == "succeeded"


def _submit_video(client, session_id: str, payload: bytes, name: str) -> None:
    detail = _wait_idle(client, session_id)
    todo = _pending(detail, "segment_video")[0]
    asset = _upload(client, session_id, name, payload, "video/mp4")
    submitted = _resolve(client, session_id, todo["item_id"],
                         {"video_asset_id": asset})
    assert submitted.status_code == 202, submitted.text
    assert _wait_job(
        client, session_id, submitted.json()["job_id"])["status"] == "succeeded"


def _submit_segment_image(client, session_id: str) -> None:
    detail = _wait_idle(client, session_id)
    todo = _pending(detail, "director_segment_image")[0]
    asset = _upload(client, session_id, "cut.png", _png(), "image/png")
    submitted = _resolve(client, session_id, todo["item_id"], {
        "decision": "submit_segment_image", "asset_id": asset})
    assert submitted.status_code == 202, submitted.text
    assert _wait_job(
        client, session_id, submitted.json()["job_id"])["status"] == "succeeded"


def _run_to_all_passed(client, session_id: str, tmp_path: Path) -> dict:
    """一路跑到三段全部通过，停在交付待办上。"""
    _start_stage1(client, session_id)
    _submit_start_image(client, session_id)
    # 第 1 段（开场）：单段导出 107。
    _submit_video(client, session_id, _mp4(tmp_path, 107, "s1"), "s1.mp4")
    # 第 2 段（切镜）：先交段首图，再交**合并导出**（前 2 段累计 214）。
    _submit_segment_image(client, session_id)
    _submit_video(client, session_id, _mp4(tmp_path, 214, "s2"), "s2.mp4")
    # 第 3 段（收尾）：单段导出 119。
    _submit_video(client, session_id, _mp4(tmp_path, 119, "s3"), "s3.mp4")
    return _wait_idle(client, session_id)


def _initial_pack(root: Path, session_id: str) -> Path:
    return root / Path(session_id) / "director" / "initial.mmxpack.zip"


def _make_final_pack(source: Path, target: Path, *, change_segment: int = 2,
                     segment_count: int | None = None) -> None:
    """从初始包复制一份最终包：改第 ``change_segment`` 段的 prompt。

    ``segment_count`` 给定时按它增删段（用来造「段数不符」的坏包）。
    """
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    timeline = json.loads(members["timeline.json"].decode("utf-8"))
    rows = timeline["segments"]
    if segment_count is not None:
        rows = rows[:segment_count]
        timeline["segments"] = rows
    if 1 <= change_segment <= len(rows):
        rows[change_segment - 1]["prompt"] = (
            str(rows[change_segment - 1].get("prompt") or "")
            + " 用户在导演台里补的一句：雨更大了。")
    # fl2v 的 shots 是 UI 那份词；这里也改一处，证明比对会落到 shots 上。
    for shot in timeline.get("shots") or []:
        if shot.get("id") == rows[0].get("id"):
            shot["prompt"] = str(shot.get("prompt") or "") + " shots 侧改动。"
    members["timeline.json"] = json.dumps(
        timeline, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _submit_delivery(client, session_id: str, film: bytes, pack: Path):
    detail = _wait_idle(client, session_id)
    todo = _pending(detail, "director_delivery")[0]
    film_asset = _upload(client, session_id, "final.mp4", film, "video/mp4")
    pack_asset = _upload(client, session_id, "final.mmxpack.zip",
                         pack.read_bytes(), "application/zip")
    return todo, _resolve(client, session_id, todo["item_id"], {
        "decision": "submit_delivery",
        "video_asset_id": film_asset,
        "pack_asset_id": pack_asset,
    })


@pytest.fixture
def director_app(tmp_path, monkeypatch):
    app, root = _build_app(tmp_path, monkeypatch)
    return app, root, tmp_path


def test_default_stub_plan_reaches_the_delivery_todo(tmp_path, monkeypatch):
    """不替换桩计划：fl2v 长片默认就该给出开场/切镜/收尾三段（E2E 走的是这一份）。

    这一条守住 E2E 的前提——若桩计划退回单段，浏览器侧就再也走不到切镜段的图槽与
    两种按需词，而 E2E 仍会「全绿」（它只是少覆盖了几条分支）。
    """
    app, _root = _build_app(tmp_path, monkeypatch, patch_plan=False)
    plan = fake_pipeline.director_plan("fl2v", 14.0)
    assert [segment.type for segment in plan.segments] == [
        "opening", "camera_cut", "ending"]
    assert [segment.export_frames for segment in plan.segments] == EXPECTED_EXPORTS
    assert plan.total_frames == sum(EXPECTED_EXPORTS)

    with workbench_client(app) as client:
        session_id = _create(client)
        detail = _run_to_all_passed(client, session_id, tmp_path)

    assert detail["status"] == "awaiting_human"
    assert len(_pending(detail, "director_delivery")) == 1


def test_reopen_after_all_passed_drops_the_delivery_todo(director_app):
    """换一段会收掉交付待办：它建立在「全部段通过」上，前提没了就不能再挂着。

    漏收的后果不是报错——最后一段重跑完会再挂一个，界面出现两个交付入口，
    用户交一个之后另一个永远悬着。
    """
    app, _root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_all_passed(client, session_id, tmp_path)
        assert len(_pending(_detail(client, session_id), "director_delivery")) == 1

        reopened = client.post(f"/api/v1/sessions/{session_id}/segments/3/reopen")
        assert reopened.status_code == 200, reopened.text
        detail = _wait_idle(client, session_id)

    assert detail["status"] == "awaiting_human"
    assert _pending(detail, "director_delivery") == []


def test_last_segment_pass_hangs_the_delivery_todo(director_app):
    app, _root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        detail = _run_to_all_passed(client, session_id, tmp_path)

    assert detail["status"] == "awaiting_human"
    todo = _pending(detail, "director_delivery")
    assert len(todo) == 1
    # 交付待办不是段待办：它没有段号，两个素材槽在决议后才回显。
    assert todo[0]["segment_index"] is None
    assert todo[0]["video_asset_id"] is None
    assert todo[0]["pack_asset_id"] is None


def test_delivery_archives_pack_film_and_rewrites(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_all_passed(client, session_id, tmp_path)

        pack = tmp_path / "final.mmxpack.zip"
        _make_final_pack(_initial_pack(root, session_id), pack)
        # 成片只要是一段可读 mp4：交付不数帧（数帧是段级证据的事）。
        todo, submitted = _submit_delivery(
            client, session_id, _mp4(tmp_path, 333, "film"), pack)
        assert submitted.status_code == 202, submitted.text
        job = _wait_job(client, session_id, submitted.json()["job_id"])
        detail = _wait_idle(client, session_id)
        directory = root / Path(session_id)

    assert job["status"] == "succeeded", job
    assert detail["status"] == "completed"

    # 三件齐备：最终包、校验值、成片记录；``final.mp4`` 是既有 video 产物。
    assert (directory / "director" / "final.mmxpack.zip").is_file()
    sha = (directory / "director" / "final.sha256").read_text(encoding="utf-8")
    assert sha.strip().endswith("final.mmxpack.zip")
    record = json.loads(
        (directory / "director" / "final-film.json").read_text(encoding="utf-8"))
    assert record["width"] == 736 and record["height"] == 1280
    assert (directory / "final.mp4").is_file()

    kinds = {item["name"]: item["kind"] for item in detail["artifacts"]}
    assert kinds["director/final.mmxpack.zip"] == "director_final_pack"
    assert kinds["director/final-film.json"] == "director_final_film"
    assert kinds["final.mp4"] == "video"
    # 校验值不单独登记成产物。
    names = set(kinds)
    assert "director/final.sha256" not in names

    # 差异补记成 rewrite 行：join 键是 segments[].id，不比帧数。
    ledger = [
        json.loads(line)
        for line in (directory / "segments" / "segment-ledger.jsonl")
        .read_text(encoding="utf-8").splitlines() if line.strip()]
    rewrites = [row for row in ledger if row.get("event") == "rewrite"]
    assert len(rewrites) == 2, rewrites  # segments 侧一段 + shots 侧一段
    assert all(row["feedback"] == "导演台 UI 内修改" for row in rewrites)
    assert {row["segment"] for row in rewrites} == {1, 2}

    # 改写条数可在页面读到（job detail）。
    delivery_job = next(job for job in detail["jobs"]
                        if job["kind"] == "delivery")
    assert "改写账" in delivery_job["detail"]
    assert "2" in delivery_job["detail"]

    # 十种事件闭集不破。
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(encoding="utf-8")
        .splitlines() if line.strip()]
    assert len(EVENT_TYPES) == 10
    assert {event["type"] for event in events} <= set(EVENT_TYPES)


def test_pack_segment_count_mismatch_is_422_and_not_archived(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_all_passed(client, session_id, tmp_path)

        pack = tmp_path / "final-bad-count.mmxpack.zip"
        _make_final_pack(_initial_pack(root, session_id), pack, segment_count=2)
        _todo, submitted = _submit_delivery(
            client, session_id, _mp4(tmp_path, 333, "film2"), pack)

        assert submitted.status_code == 422, submitted.text
        assert submitted.json()["code"] == "archive_invalid"
        # 不归档、待办仍在：用户换一份包就能重交。
        directory = root / Path(session_id)
        assert not (directory / "director" / "final.mmxpack.zip").exists()
        detail = _detail(client, session_id)
        assert _pending(detail, "director_delivery")


def test_corrupt_pack_is_422_archive_invalid(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_all_passed(client, session_id, tmp_path)

        bad = tmp_path / "broken.mmxpack.zip"
        bad.write_bytes(b"PK\x03\x04not really a zip")
        _todo, submitted = _submit_delivery(
            client, session_id, _mp4(tmp_path, 333, "film3"), bad)

        assert submitted.status_code == 422, submitted.text
        assert submitted.json()["code"] == "archive_invalid"
        assert not (root / Path(session_id) / "director" / "final.mmxpack.zip").exists()


def test_missing_timeline_is_422_archive_invalid(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_all_passed(client, session_id, tmp_path)

        pack = tmp_path / "no-timeline.mmxpack.zip"
        with zipfile.ZipFile(pack, "w") as archive:
            archive.writestr("other.txt", "no timeline")
        _todo, submitted = _submit_delivery(
            client, session_id, _mp4(tmp_path, 333, "film4"), pack)

        assert submitted.status_code == 422, submitted.text
        assert submitted.json()["code"] == "archive_invalid"
        assert not (root / Path(session_id) / "director" / "final.mmxpack.zip").exists()


def _aspect_current(mode: str, expected: tuple[int, int]):
    """造一个只带画幅判据所需字段的「详情」：宽高判据只读 brief/canvases/时间。"""
    from minimax_h3_prompt.director.film_canvas import film_frame_canvases
    from minimax_h3_prompt.image_config import load_frame_image_model

    brief = SimpleNamespace(
        director_mode=mode,
        canvases=film_frame_canvases(expected, load_frame_image_model()))
    return SimpleNamespace(
        brief=brief, created_at="2026-11-14T00:00:00+00:00",
        stage_state_snapshot=lambda: {})


def test_final_film_mismatch_uses_the_shared_verdict(director_app):
    """宽高判据只走 ``final_film_verdict``：r2v 的画幅超差要在归档前被拦下。

    ``deliver`` 不查宽高（票面现状事实），所以这里钉的是**工作台自己那一次调用**：
    把 ``final_film_verdict`` 换成恒放行时这条必须变红（变异检验）。
    """
    from minimax_h3_prompt.workbench_store import FinalFilmMismatch

    app, _root, tmp_path = director_app
    runner = app.state.job_runner
    film = tmp_path / "landscape.mp4"
    fake_pipeline.write_director_segment_video(film, 24)  # 736×1280

    # r2v 的期望画幅是横的：比例超差 → 停。
    with pytest.raises(FinalFilmMismatch) as excinfo:
        runner._validate_final_film(
            _aspect_current("r2v", (1920, 1088)), film)
    assert "1920×1088" in str(excinfo.value)

    # t2v 没有画布判据：同一份成片直接放行。
    runner._validate_final_film(_aspect_current("t2v", (1920, 1088)), film)

