"""D8-2（#262）：导演台阶段 2 ＝ 规划 ＋ 写段（spec #253 §8.1 / §8.2）。

判据分四层：

1. **接线**：导演台会话通过审查（或交完开场首帧）后走导演台执行器，落
   ``director/plan.json`` 与逐段 ``director/segment-NN.md``；规划失败即 job 失败；
2. **段表进快照**：``SegmentOut`` 带可选帧字段，``start_s`` / ``end_s`` 由帧向下截断；
3. **严格读回**：续跑只认 ``read_plan_json``，坏表（未知类型/帧数不自洽）抛错，
   已在盘上的段词不重写、段表不重规划；
4. **只读子集**：r2v 的段内素材子集从 ``director/assets-in-segment.json`` 读回，
   重编号后的 Picture 号 + 语义名一起进快照，不做第二套模型。

模型桩接在 ``api.jobs.build_chat_model`` 缝上（导演台规划/写段）与
``model_factory.build_chat_model`` 上（阶段 1）；真实 LangGraph、真实落盘、
真实校验器都照跑。
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api import jobs as jobs_module
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.director.frames import FPS, DirectorPlan, DirectorSegment
from minimax_h3_prompt.director.planning import plan_path, read_plan_json
from minimax_h3_prompt.director.prompts import segment_path
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=1)

# i2v 的阶段 1 要出首帧词；I2VA 只收 first 一份。取 FL2VA 的同一份内容去掉尾帧，
# 形状与真实节点一致（``fl2va_bundle_from_dict`` 会校验字段齐全）。
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


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    """临时会话根 + 确定性桩模型（导演台两条直调也在同一份桩上认角色）。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    # i2v/fl2v 的阶段 1 要出关键帧词：交完整的两帧 bundle，i2v 侧由节点把多余的
    # 尾帧剥掉（``strip_foreign_frames``），两种变体共用一份桩回复。
    model = DeterministicChatModel(replies={
        **DEFAULT_REPLIES,
        "frame_prompt_engineer": json.dumps(FL2VA_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    monkeypatch.setattr(jobs_module, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


def _director_body(mode: str = "t2v", duration: float = 4.0) -> dict:
    return {
        "mode": "director",
        "plot": "雨夜街口的追逐",
        "duration": duration,
        "director_mode": mode,
        "director_node_id": "node-42",
        "canvases": {},
    }


def _wait_job(client, session_id: str, job_id: str, timeout: float = 60.0) -> dict:
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


def _run_to_review(client, session_id: str) -> dict:
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    job = _wait_job(client, session_id, started.json()["job_id"])
    assert job["status"] == "succeeded", job
    review = _pending(_detail(client, session_id), "stage1_review")
    assert len(review) == 1
    return review[0]


def _create(client, mode: str = "t2v") -> str:
    resp = client.post("/api/v1/sessions", json=_director_body(mode))
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _plan_and_write(client, session_id: str, item: dict) -> dict:
    approved = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
        json={"decision": "approve"})
    assert approved.status_code == 202, approved.text
    job = approved.json()
    assert job is not None, "t2v 通过审查应直接起阶段 2 任务"
    body = _wait_job(client, session_id, job["job_id"])
    assert body["status"] == "succeeded", body
    return body


def test_t2v_plans_and_writes_segments_with_frame_grid(workbench):
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "t2v")
        review = _run_to_review(client, session_id)
        _plan_and_write(client, session_id, review)

        directory = root / Path(session_id)
        plan_file = plan_path(directory)
        assert plan_file.is_file(), "导演台段表应落在 director/plan.json"
        plan = read_plan_json(plan_file)
        assert plan.mode == "t2v"
        assert sum(segment.export_frames for segment in plan.segments) == plan.total_frames
        assert segment_path(directory, 1).is_file()

        detail = _detail(client, session_id)
        # 过渡态：本票没有打包（#263）+ 段循环（#264），写完后停在 awaiting_human，
        # 不假装进 segmenting、不走 base 的拼接。
        assert detail["status"] == "awaiting_human"
        assert len(detail["segments"]) == 1
        segment = detail["segments"][0]
        assert segment["segment_type"] == "opening"
        assert segment["guided"] is False
        assert segment["give_images"] == []
        assert segment["start_frame"] == 0
        assert segment["end_frame"] == segment["declared_frames"]
        assert segment["export_frames"] == segment["declared_frames"]
        assert segment["start_s"] == 0
        assert segment["end_s"] == int(segment["end_frame"] / FPS)
        assert segment["prompt_artifact_id"].endswith("director/segment-01.md")

        # 段词进产物登记；plan.json 不是产物。
        assert any(item["kind"] == "director_segment_prompt"
                   for item in detail["artifacts"])
        assert not any(item["name"] == "director/plan.json"
                       for item in detail["artifacts"])
        content = client.get(
            f"/api/v1/artifacts/{segment['prompt_artifact_id']}/content")
        assert content.status_code == 200, content.text
        assert "integrated_multimodal_description:" in content.text

        notes = "".join(detail["director_notes"])
        assert "目标" in notes and "实际" in notes and "差" in notes

        assert "director_planner" in model.calls
        assert "director_segment_writer" in model.calls


def test_written_segments_are_not_replanned_or_rewritten_on_advance(workbench):
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "t2v")
        review = _run_to_review(client, session_id)
        _plan_and_write(client, session_id, review)

        directory = root / Path(session_id)
        segment_text = segment_path(directory, 1).read_text(encoding="utf-8")
        plan_text = plan_path(directory).read_text(encoding="utf-8")
        planned_calls = model.calls.count("director_planner")
        written_calls = model.calls.count("director_segment_writer")

        # 模拟「上一轮被中断」：把状态改成可 advance 的 interrupted，再推进一次。
        session_file = directory / SESSION_FILENAME
        raw = json.loads(session_file.read_text(encoding="utf-8"))
        raw["status"] = "interrupted"
        session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        resumed = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert resumed.status_code == 202, resumed.text
        body = _wait_job(client, session_id, resumed.json()["job_id"])
        assert body["status"] == "succeeded", body

        # 判据落磁盘事实：段表与段词都还在，模型一次都没有再被调用。
        assert model.calls.count("director_planner") == planned_calls
        assert model.calls.count("director_segment_writer") == written_calls
        assert plan_path(directory).read_text(encoding="utf-8") == plan_text
        assert segment_path(directory, 1).read_text(encoding="utf-8") == segment_text
        assert _detail(client, session_id)["segments"][0]["segment_type"] == "opening"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        # 未知段类型 / 非法帧数 / Σ 导出 ≠ 总帧：三种坏表都必须停在读回那一层。
        (lambda plan: plan["segments"][0].update(type="not_a_segment_type"), "未知段类型"),
        (lambda plan: plan["segments"][0].update(declared_frames=0), "帧数非法"),
        (lambda plan: plan.update(total_frames=plan["total_frames"] + 17), "导出合计"),
    ],
)
def test_corrupt_plan_stops_the_resume_instead_of_reusing_a_wrong_table(
    workbench, mutate, expected,
):
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "t2v")
        review = _run_to_review(client, session_id)
        _plan_and_write(client, session_id, review)

        directory = root / Path(session_id)
        bad = json.loads(plan_path(directory).read_text(encoding="utf-8"))
        mutate(bad)
        plan_path(directory).write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        session_file = directory / SESSION_FILENAME
        raw = json.loads(session_file.read_text(encoding="utf-8"))
        raw["status"] = "interrupted"
        session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        resumed = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert resumed.status_code == 202, resumed.text
        body = _wait_job(client, session_id, resumed.json()["job_id"])
        assert body["status"] == "failed"
        failures = [
            json.loads(line) for line in
            (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("type") == "job.failed"
        ]
        assert failures and expected in failures[-1]["payload"]["detail"]
        # 坏表没有被「归一」成一张能跑的表：段表原样留在盘上等人处理。
        assert json.loads(
            plan_path(directory).read_text(encoding="utf-8")
        ) == bad


def test_planner_failure_fails_the_job_and_names_the_reason(workbench):
    app, model, root = workbench
    # 规划回复不是 JSON：``parse_director_plan`` 抛 ValueError，job 必须失败并点名。
    model.replies["director_planner"] = "这不是 JSON，也没有段表"
    with workbench_client(app) as client:
        session_id = _create(client, "t2v")
        review = _run_to_review(client, session_id)
        approved = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        assert approved.status_code == 202, approved.text
        body = _wait_job(client, session_id, approved.json()["job_id"])
        assert body["status"] == "failed"

        directory = root / Path(session_id)
        failures = [
            json.loads(line) for line in
            (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("type") == "job.failed"
        ]
        assert failures and "JSON" in failures[-1]["payload"]["detail"]
        # 没有机械回退：规划失败不落段表，也不写段词。
        assert not plan_path(directory).is_file()
        assert not segment_path(directory, 1).is_file()


def _director_rows(plan: DirectorPlan, directory: Path,
                   existing: list[dict] | None = None) -> list[dict]:
    from minimax_h3_prompt.api.jobs import _director_segment_rows

    return _director_segment_rows("s/GEN001", plan, directory, existing or [])


def test_segment_rows_derive_seconds_from_frames_and_read_the_r2v_subset(tmp_path):
    plan = DirectorPlan(mode="r2v", total_frames=204, segments=(
        DirectorSegment(
            index=1, type="opening", guided=False, start_frame=0, end_frame=204,
            declared_frames=192, export_frames=204,
            summary="开场", end_hook="人物入画", give_images=("assets",)),
    ))
    (tmp_path / "director").mkdir()
    (tmp_path / "director" / "assets-in-segment.json").write_text(
        json.dumps({"1": [
            {"new": 1, "original": 3, "name": "黑伞"},
            {"new": 2, "original": 5, "name": "旧信"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )

    rows = _director_rows(plan, tmp_path)
    row = rows[0]
    assert row["start_s"] == 0
    assert row["end_s"] == int(204 / FPS)
    assert row["segment_type"] == "opening"
    assert row["declared_frames"] == 192
    assert row["export_frames"] == 204
    assert row["give_images"] == ["assets"]
    assert [(asset["number"], asset["name"], asset["original_number"])
            for asset in row["assets"]] == [(1, "黑伞", 3), (2, "旧信", 5)]


def test_segment_rows_keep_existing_status_when_rebuilt(tmp_path):
    plan = DirectorPlan(mode="t2v", total_frames=192, segments=(
        DirectorSegment(
            index=1, type="opening", guided=False, start_frame=0, end_frame=192,
            declared_frames=192, export_frames=192,
            summary="开场", end_hook="人物入画", give_images=()),
    ))
    existing = [{"index": 1, "status": "waiting_video",
                 "prompt_artifact_id": "s/director/segment-01.md",
                 "video_asset_id": "s/abc", "rejection_reason": "未过"}]
    row = _director_rows(plan, tmp_path, existing)[0]
    assert row["status"] == "waiting_video"
    assert row["prompt_artifact_id"] == "s/director/segment-01.md"
    assert row["video_asset_id"] == "s/abc"
    assert row["rejection_reason"] == "未过"
    assert row["end_frame"] == 192


def _png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 40, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_start_image_submit_enters_planning_instead_of_revising_stage1(workbench):
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "i2v")
        review = _run_to_review(client, session_id)
        gate = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        # i2v 通过审查只挂开场首帧待办，不起任务。
        assert gate.status_code == 202 and gate.json() is None
        item = _pending(_detail(client, session_id), "director_start_image")[0]

        upload = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": ("start.png", _png((736, 1280)), "image/png")})
        assert upload.status_code == 201, upload.text
        start_asset = upload.json()["asset_id"]

        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image",
                  "start_asset_id": start_asset,
                  "confirm_aspect_mismatch": True})
        assert resp.status_code == 202, resp.text
        body = _wait_job(client, session_id, resp.json()["job_id"])
        assert body["status"] == "succeeded", body

        directory = root / Path(session_id)
        assert plan_path(directory).is_file()
        assert segment_path(directory, 1).is_file()
        detail = _detail(client, session_id)
        # 交完首帧后没有新挂的审查待办：走的是导演台规划，不是修订重跑阶段 1。
        assert _pending(detail, "stage1_review") == []
        assert detail["segments"][0]["give_images"] == ["first"]
        assert "director_planner" in model.calls


def test_only_missing_skips_existing_segment_files(tmp_path):
    """续跑只补缺的段：已在盘上的段词不重写，模型只为缺的那段付一次调用。"""
    from minimax_h3_prompt.director.prompts import write_director_segments

    plan = DirectorPlan(mode="t2v", total_frames=180, segments=(
        DirectorSegment(
            index=1, type="opening", guided=False, start_frame=0, end_frame=90,
            declared_frames=90, export_frames=90,
            summary="开场", end_hook="人物走进门廊", give_images=()),
        DirectorSegment(
            index=2, type="opening", guided=False, start_frame=90, end_frame=180,
            declared_frames=90, export_frames=90,
            summary="收束", end_hook="门在身后合上", give_images=()),
    ))
    first = segment_path(tmp_path, 1)
    first.parent.mkdir(parents=True)
    first.write_text("EXISTING SEGMENT ONE\n", encoding="utf-8")

    model = DeterministicChatModel()
    written = write_director_segments(
        plan, llm=model, session_dir=tmp_path, shot_table="[Shot 1] ...",
        only_missing=True)

    assert [item.index for item in written] == [1, 2]
    assert first.read_text(encoding="utf-8") == "EXISTING SEGMENT ONE\n"
    assert segment_path(tmp_path, 2).is_file()
    assert model.calls == ["director_segment_writer"]


def test_film_end_description_does_not_leak_across_sessions(workbench, monkeypatch):
    """JobRunner 是进程级单实例：A 会话的尾帧描述不能被 B 的续写请求读走。"""
    from minimax_h3_prompt.tools import reference_auditor

    monkeypatch.setattr(
        reference_auditor, "describe_image",
        lambda _path: "A 会话的终点画面：隧道深处，车灯由远及近")

    app, model, root = workbench
    with workbench_client(app) as client:
        # A：fl2v + 全片尾帧，正常规划与写段（会用到尾帧描述）。
        a = _create(client, "fl2v")
        review = _run_to_review(client, a)
        gate = client.post(
            f"/api/v1/sessions/{a}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        assert gate.status_code == 202 and gate.json() is None
        item = _pending(_detail(client, a), "director_start_image")[0]
        start = client.post(
            f"/api/v1/sessions/{a}/assets",
            files={"file": ("start.png", _png((736, 1280)), "image/png")}
        ).json()["asset_id"]
        end = client.post(
            f"/api/v1/sessions/{a}/assets",
            files={"file": ("end.png", _png((736, 1280)), "image/png")}
        ).json()["asset_id"]
        accepted = client.post(
            f"/api/v1/sessions/{a}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image", "start_asset_id": start,
                  "end_asset_id": end, "confirm_aspect_mismatch": True})
        assert accepted.status_code == 202, accepted.text
        assert _wait_job(client, a, accepted.json()["job_id"])["status"] == "succeeded"

        # B：i2v 没有尾帧，跑到写段完成后**删掉段词**，制造「续写缺段」。
        b = _create(client, "i2v")
        review = _run_to_review(client, b)
        client.post(
            f"/api/v1/sessions/{b}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        item = _pending(_detail(client, b), "director_start_image")[0]
        start = client.post(
            f"/api/v1/sessions/{b}/assets",
            files={"file": ("start.png", _png((736, 1280)), "image/png")}
        ).json()["asset_id"]
        accepted = client.post(
            f"/api/v1/sessions/{b}/action-items/{item['item_id']}/resolve",
            json={"decision": "submit_start_image", "start_asset_id": start,
                  "confirm_aspect_mismatch": True})
        assert _wait_job(client, b, accepted.json()["job_id"])["status"] == "succeeded"

        directory = root / Path(b)
        segment_path(directory, 1).unlink()
        session_file = directory / SESSION_FILENAME
        raw = json.loads(session_file.read_text(encoding="utf-8"))
        raw["status"] = "interrupted"
        session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        before = len(model.requests)
        resumed = client.post(f"/api/v1/sessions/{b}/jobs")
        assert _wait_job(client, b, resumed.json()["job_id"])["status"] == "succeeded"

    writer_requests = [
        request for request in model.requests[before:]
        if "本段编号：Segment " in request
    ]
    assert writer_requests, "续写缺段应真的调用一次写段"
    # 旧实现会让 B 读到 A 留在 runner 上的尾帧标志与描述——i2v 写段请求里不该出现它。
    assert "【全片终点画面" not in writer_requests[-1]
    assert "隧道深处" not in writer_requests[-1]


def test_director_notes_uses_the_same_wording_as_the_planner(tmp_path):
    """详情页的总长说明与规划器 ``land_exports`` 的 note 必须逐字同源。"""
    from minimax_h3_prompt.director.frames import land_exports
    from minimax_h3_prompt.director.planning import write_plan_json
    from minimax_h3_prompt.workbench_store import _director_notes

    exports, note = land_exports(["opening"], [96], 96, mode="t2v")
    assert note is not None and "差" in note and "目标" in note
    plan = DirectorPlan(mode="t2v", total_frames=sum(exports), segments=(
        DirectorSegment(
            index=1, type="opening", guided=False, start_frame=0,
            end_frame=sum(exports), declared_frames=exports[0],
            export_frames=exports[0], summary="", end_hook="人物走进门廊",
            give_images=()),
    ))
    write_plan_json(plan_path(tmp_path), plan)

    assert _director_notes(tmp_path, 4.0) == [note]
