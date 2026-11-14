"""D8-4（#264）：导演台段循环——按需词、段视频回交与数帧判定（spec #253 §8.3 / §8.4 / §10）。

四层判据：

1. **产物登记**：``director/firstframe-NN.md`` / ``director/tailframe-NN.md`` 各是一种
   kind，产物分区按 kind 读得到；
2. **判据边界**：两种导出形态都认；有引导段的容差边界；容差改 0 必须变红；
3. **段循环**：打包完成后自动进 ``segmenting``；切镜段先产首帧词并挂
   ``director_segment_image``，收图后写尾帧词并挂 ``segment_video``；``ending`` 有
   全片尾帧时跳过尾帧词；
4. **回交判定**：读不出 → 422 ``video_unreadable``；帧数判不过 → 422
   ``frame_mismatch`` 并点名「请确认贴的是本段成片」；通过后追加账本行、段进 passed、
   下一段待办自动挂上；被拒的段绝不落成 passed。
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from minimax_h3_prompt import model_factory, workbench_store
from minimax_h3_prompt.api import fake_pipeline
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.director import companion, ledger
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


# ---------------------------------------------------------------------------
# 段表：fl2v 三段（开场 / 切镜 / 收尾），帧全部落在 17k+5 网格上
# ---------------------------------------------------------------------------

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


# 107 = 17×6+5；有引导段（ending）的导出帧必须是 17 的倍数（119 = 17×7，声明 107）。
# 切镜是硬切、不开段间引导，所以它就是 107（无引导段导出＝声明）。累计 107+107+119=333。
PLAN = DirectorPlan(mode="fl2v", total_frames=333, segments=(
    _segment(1, "opening", 0, 107),
    _segment(2, "camera_cut", 107, 107),
    _segment(3, "ending", 214, 107, export=119),
))


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
    """桩造一条精确帧数的 mp4：走与真实回交同一条上传/嗅探路径。"""
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


def _segment_row(detail: dict, index: int) -> dict:
    return next(row for row in detail["segments"] if row["index"] == index)


def _resolve(client, session_id: str, item_id: str, body: dict):
    return client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve", json=body)


@pytest.fixture
def director_app(tmp_path, monkeypatch):
    """桩管线 + 固定三段表（开场/切镜/收尾）；真实模型一调就抛。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: (_ for _ in ()).throw(AssertionError("桩管线不许建真实模型")))
    monkeypatch.setattr(fake_pipeline, "director_plan",
                        lambda mode, total_s: PLAN)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=_runtime(tmp_path))
    return app, root, tmp_path


def _create(client, mode: str = "fl2v", duration: float = 14.375) -> str:
    response = client.post("/api/v1/sessions", json={
        "mode": "director", "plot": "雨夜街口的追逐", "duration": duration,
        "director_mode": mode, "director_node_id": "node-42", "canvases": {},
    })
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _run_to_first_todo(client, session_id: str, *,
                       end_asset: bool = False) -> dict:
    """阶段 1 → 审查通过 → 开场首帧 → 规划/写段 → 打包 → 第 1 段待办。"""
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    assert _wait_job(client, session_id, started.json()["job_id"])["status"] == "succeeded"

    review = _pending(_detail(client, session_id), "stage1_review")[0]
    approved = _resolve(client, session_id, review["item_id"], {"decision": "approve"})
    assert approved.status_code == 202, approved.text
    # 导演台 fl2v 通过审查只挂待办，不起任务（闸门分支）。
    assert approved.json() is None

    start_todo = _pending(_detail(client, session_id), "director_start_image")[0]
    start_asset = _upload(client, session_id, "start.png", _png(), "image/png")
    body = {
        "decision": "submit_start_image", "start_asset_id": start_asset}
    if end_asset:
        body["end_asset_id"] = _upload(
            client, session_id, "film-end.png", _png(), "image/png")
    submitted = _resolve(client, session_id, start_todo["item_id"], body)
    assert submitted.status_code == 202, submitted.text
    assert _wait_job(
        client, session_id, submitted.json()["job_id"])["status"] == "succeeded"
    detail = _wait_idle(client, session_id)
    # 打包完成后段循环自动跑第一件，停在等视频。
    assert _pending(detail, "segment_video"), detail["action_items"]
    return detail


def _submit_video(client, session_id: str, payload: bytes, name: str):
    detail = _detail(client, session_id)
    todo = _pending(detail, "segment_video")[0]
    asset = _upload(client, session_id, name, payload, "video/mp4")
    return _resolve(client, session_id, todo["item_id"], {"video_asset_id": asset})


def _pass_segment(client, session_id: str, payload: bytes, name: str) -> dict:
    submitted = _submit_video(client, session_id, payload, name)
    assert submitted.status_code == 202, submitted.text
    assert _wait_job(
        client, session_id, submitted.json()["job_id"])["status"] == "succeeded"
    return _wait_idle(client, session_id)


# ---------------------------------------------------------------------------
# 1. 产物登记（纯登记表）：两种按需词各是一种 kind
# ---------------------------------------------------------------------------

def test_director_prompt_artifacts_are_registered_as_text():
    first = workbench_store.artifact_spec("director/firstframe-02.md")
    assert first is not None
    assert first.kind == "director_first_frame_prompt"
    assert first.media_type is None

    tail = workbench_store.artifact_spec("director/tailframe-03.md")
    assert tail is not None
    assert tail.kind == "director_tailframe_prompt"
    assert tail.media_type is None

    # 段词与简报的登记不受影响（同一个 director/ 目录，按文件名分支）。
    assert workbench_store.artifact_spec("director/segment-01.md").kind == (
        "director_segment_prompt")
    assert workbench_store.artifact_spec("director/briefing-01.md").kind == (
        "director_briefing")


# ---------------------------------------------------------------------------
# 2. 判据边界（纯函数）：两种导出形态、容差边界、容差改 0 变红
# ---------------------------------------------------------------------------

def test_judge_accepts_single_and_merged_export_forms():
    cut = PLAN.segments[1]
    # 单段导出：本段 export_frames。
    single = companion.judge_video_evidence(
        cut, actual_frames=cut.export_frames, plan=PLAN,
        tolerance=RUNTIME_TOLERANCE)
    assert single.passed and single.code == companion.CODE_FRAME_OK
    # 前 2 段合并导出：总帧 = 第 2 段 end_frame。
    merged = companion.judge_video_evidence(
        cut, actual_frames=PLAN.segments[1].end_frame, plan=PLAN,
        tolerance=RUNTIME_TOLERANCE)
    assert merged.passed
    # 整片合并导出：总帧 = 末段 end_frame。
    whole = companion.judge_video_evidence(
        cut, actual_frames=PLAN.total_frames, plan=PLAN,
        tolerance=RUNTIME_TOLERANCE)
    assert whole.passed


def test_judge_tolerance_boundary_and_zero_tolerance_turns_red():
    ending = PLAN.segments[2]
    short = ending.export_frames - RUNTIME_TOLERANCE
    assert companion.judge_video_evidence(
        ending, actual_frames=short, plan=PLAN,
        tolerance=RUNTIME_TOLERANCE).passed
    assert not companion.judge_video_evidence(
        ending, actual_frames=short, plan=PLAN, tolerance=0).passed
    # 超一位也不行：超出容差就是报红，不是「差不多」。
    assert not companion.judge_video_evidence(
        ending, actual_frames=ending.export_frames - RUNTIME_TOLERANCE - 1,
        plan=PLAN, tolerance=RUNTIME_TOLERANCE).passed


def test_default_tolerance_comes_from_the_constant():
    """容差从模块常量派生：把常量改成 0 时这条必须变红（变异守卫）。

    少 1 帧是相位对齐的合法形态（#112 现场），默认容差必须放行它；不显式传
    ``tolerance``，判据才会真去读 ``DIRECTOR_FRAME_TOLERANCE``。
    """
    ending = PLAN.segments[2]
    assert companion.judge_video_evidence(
        ending, actual_frames=ending.export_frames - 1, plan=PLAN).passed


def test_judge_rejects_videos_that_are_not_this_segment():
    cut = PLAN.segments[1]
    verdict = companion.judge_video_evidence(
        cut, actual_frames=PLAN.total_frames + 500, plan=PLAN,
        tolerance=RUNTIME_TOLERANCE)
    assert not verdict.passed
    assert "请确认贴的是本段成片" in verdict.message


# ---------------------------------------------------------------------------
# 3. 段循环：按需词、段首图待办、账本与段状态
# ---------------------------------------------------------------------------

def test_director_segment_loop_runs_all_three_segments(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        detail = _run_to_first_todo(client, session_id)
        directory = root / Path(session_id)

        # 第 1 段（开场）只要视频，没有按需词。
        opening = _segment_row(detail, 1)
        assert opening["status"] == "waiting_video"
        assert opening["declared_frames"] == 107
        assert opening["export_frames"] == 107
        assert not (directory / "director" / "firstframe-01.md").exists()

        detail = _pass_segment(
            client, session_id, _mp4(tmp_path, 107, "opening"), "opening.mp4")
        assert _segment_row(detail, 1)["status"] == "passed"

        # 第 2 段（切镜）：首帧词自动产出，等用户挂这一段的图。
        assert (directory / "director" / "firstframe-02.md").is_file()
        image_todo = _pending(detail, "director_segment_image")
        assert len(image_todo) == 1 and image_todo[0]["segment_index"] == 2
        assert _segment_row(detail, 2)["status"] == "pending"

        image_asset = _upload(client, session_id, "cut.png", _png(), "image/png")
        submitted = _resolve(client, session_id, image_todo[0]["item_id"], {
            "decision": "submit_segment_image", "asset_id": image_asset})
        assert submitted.status_code == 202, submitted.text
        assert _wait_job(
            client, session_id, submitted.json()["job_id"])["status"] == "succeeded"
        detail = _wait_idle(client, session_id)

        # 收图后自动写尾帧词，再挂本段视频待办。
        assert (directory / "director" / "tailframe-02.md").is_file()
        assert _segment_row(detail, 2)["status"] == "waiting_video"
        assert _pending(detail, "segment_video")

        detail = _pass_segment(
            client, session_id, _mp4(tmp_path, 214, "cut"), "cut-merged.mp4")
        assert _segment_row(detail, 2)["status"] == "passed"

        # 第 3 段（收尾）：尾帧词以第 2 段成片末帧为起点，自动产出。
        assert (directory / "director" / "tailframe-03.md").is_file()
        assert _segment_row(detail, 3)["status"] == "waiting_video"
        detail = _pass_segment(
            client, session_id, _mp4(tmp_path, 119, "ending"), "ending.mp4")
        assert _segment_row(detail, 3)["status"] == "passed"
        assert detail["status"] == "awaiting_human"

        # 段产出账与 CLI 同一本：三段各一行 output，实际帧数对得上。
        rows = ledger.read_director_rows(directory)
        outputs = [row for row in rows if row["event"] == "output"]
        assert [row["segment"] for row in outputs] == [1, 2, 3]
        assert [row["actual_frames"] for row in outputs] == [107, 214, 119]
        assert all(row["video_sha256"] for row in outputs)

        # 两种按需词都登记成产物，按 kind 读得到。
        kinds = {item["kind"] for item in detail["artifacts"]}
        assert "director_first_frame_prompt" in kinds
        assert "director_tailframe_prompt" in kinds

        # 十种事件类型闭集不变。
        events = [
            json.loads(line)
            for line in (directory / "events.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
        assert len(EVENT_TYPES) == 10
        assert {event["type"] for event in events} <= set(EVENT_TYPES)


def test_finalize_retry_does_not_duplicate_the_ledger_row(director_app):
    """崩溃重试同段收尾：账本行不重复（追加式账不会自己去重），段仍能通过。"""
    from minimax_h3_prompt.session_store import SESSION_FILENAME

    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_first_todo(client, session_id)
        directory = root / Path(session_id)
        _pass_segment(client, session_id, _mp4(tmp_path, 107, "o"), "o.mp4")
        assert len(ledger.read_director_rows(directory)) == 1

        # 模拟「同步判帧已过、慢收尾前崩溃」：段停在 validating，会话可再 advance。
        session_file = directory / SESSION_FILENAME
        raw = json.loads(session_file.read_text(encoding="utf-8"))
        raw["status"] = "interrupted"
        for row in raw["segments"]:
            if row["index"] == 1:
                row["status"] = "validating"
        session_file.write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        resumed = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert resumed.status_code == 202, resumed.text
        assert _wait_job(
            client, session_id, resumed.json()["job_id"])["status"] == "succeeded"
        detail = _wait_idle(client, session_id)

        assert _segment_row(detail, 1)["status"] == "passed"
        outputs = [
            row for row in ledger.read_director_rows(directory)
            if row["event"] == "output"]
        assert [row["segment"] for row in outputs] == [1]


def test_ending_skips_tailframe_when_film_end_is_present(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_first_todo(client, session_id, end_asset=True)
        directory = root / Path(session_id)

        _pass_segment(client, session_id, _mp4(tmp_path, 107, "o"), "o.mp4")
        detail = _detail(client, session_id)
        image_todo = _pending(detail, "director_segment_image")[0]
        image_asset = _upload(client, session_id, "cut.png", _png(), "image/png")
        submitted = _resolve(client, session_id, image_todo["item_id"], {
            "decision": "submit_segment_image", "asset_id": image_asset})
        assert submitted.status_code == 202, submitted.text
        assert _wait_job(
            client, session_id, submitted.json()["job_id"])["status"] == "succeeded"
        _wait_idle(client, session_id)

        detail = _pass_segment(
            client, session_id, _mp4(tmp_path, 214, "c"), "c.mp4")
        assert _segment_row(detail, 3)["status"] == "waiting_video"
        # 作者给的全片尾帧存在时，ending 不写尾帧词、不花模型调用。
        assert not (directory / "director" / "tailframe-03.md").exists()
        detail = _pass_segment(
            client, session_id, _mp4(tmp_path, 119, "e"), "e.mp4")
        assert _segment_row(detail, 3)["status"] == "passed"


# ---------------------------------------------------------------------------
# 4. 回交判定：读不出 / 帧数不符 / 被拒的段绝不落 passed
# ---------------------------------------------------------------------------

def test_unreadable_video_is_422_video_unreadable(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_first_todo(client, session_id)
        # 扩展名与嗅探都对得上 mp4，但内容不是可解码视频。
        bad = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        submitted = _submit_video(client, session_id, bad, "bad.mp4")
        assert submitted.status_code == 422, submitted.text
        assert submitted.json()["code"] == "video_unreadable"
        detail = _detail(client, session_id)
        assert _segment_row(detail, 1)["status"] == "waiting_video"
        assert not ledger.read_director_rows(root / Path(session_id))


def test_frame_mismatch_is_422_and_segment_never_passes(director_app):
    app, root, tmp_path = director_app
    with workbench_client(app) as client:
        session_id = _create(client)
        _run_to_first_todo(client, session_id)
        submitted = _submit_video(
            client, session_id, _mp4(tmp_path, 50, "wrong"), "wrong.mp4")
        assert submitted.status_code == 422, submitted.text
        problem = submitted.json()
        assert problem["code"] == "frame_mismatch"
        assert "请确认贴的是本段成片" in problem["detail"]
        detail = _detail(client, session_id)
        assert _segment_row(detail, 1)["status"] == "waiting_video"
        # 同一待办仍在：用户可直接换一条视频重交。
        assert _pending(detail, "segment_video")[0]["segment_index"] == 1
        assert not ledger.read_director_rows(root / Path(session_id))
        # 重交一条正确帧数的视频，这一段仍能通过。
        detail = _pass_segment(
            client, session_id, _mp4(tmp_path, 107, "ok"), "ok.mp4")
        assert _segment_row(detail, 1)["status"] == "passed"
