"""#324：工作台三处调用点把开场首帧图的画面事实带进规划与写段请求。

判据分五层：

1. **规划**：``_plan_director_project`` 的规划请求里出现起点画面块（planning 口径）；
2. **当轮构造点**：同一次规划后的首写请求也带同一份描述（写段口径）；
3. **续跑重建**：删段后续写，出站请求里的描述与首写那次逐字相同（sidecar 命中、
   不重读）；
4. **不串话**：A 规划 → B 规划 → A 续写，A 的请求只含 A 的描述。runner 是进程级
   单实例，这条用例就是用来证伪「把描述挂到 runner 实例属性上」的：那样 A 会读到
   B 的残留值；
5. **失败与无槽**：读图失败时 job 明确失败、病因点名是哪张图；无图或模式无首帧槽
   （t2v／r2v）时请求与不给描述时逐字节相同。

视觉调用一律桩在 ``reference_auditor.describe_image`` 上：接线变更曾让旧用例真发
网络请求（#56 实测），这里的默认桩是一调用就报红，用例想读图必须显式给桩。
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
from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
from minimax_h3_prompt.director.pack import DirectorPackSettings, START_STEM
from minimax_h3_prompt.director.planning import plan_path
from minimax_h3_prompt.director.prompts import segment_path
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.writing_disciplines import FRAME_CONSISTENCY_DISCIPLINE
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=1,
    director_settings=lambda: DirectorPackSettings(
        width=736, height=1280, ref_max_size=864, ref_image_size="match"),
    director_comfy_output_root=lambda: Path("unused-comfy-output"),
)

# 规划与写段共用的块标题（``director.opening_still.OPENING_STILL_BLOCK_HEAD``）：
# 断言用字面量，改措辞时这条会跟着报红，不会静默变成「块还在、只是没人看」。
BLOCK_HEAD = "【本段起点画面（用户提供的开场首帧图）】"
PLANNER_MARK = "Segment Planner（导演台分段规划师）"
WRITER_MARK = "本段编号：Segment "

# i2v／fl2v 的阶段 1 要出关键帧词：交完整两帧 bundle，i2v 侧由节点剥掉多余的尾帧。
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
    """临时会话根 + 确定性桩模型；视觉调用默认一调就报红（不许出网）。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setattr(
        reference_auditor, "describe_image",
        lambda path: (_ for _ in ()).throw(
            AssertionError(f"读图没有被桩住，真的要出网了：{path}")))
    model = DeterministicChatModel(replies={
        **DEFAULT_REPLIES,
        "frame_prompt_engineer": json.dumps(FL2VA_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    monkeypatch.setattr(jobs_module, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, model, root


def _png(size: tuple[int, int] = (736, 1280)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 40, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def _create(client, mode: str = "i2v", plot: str = "雨夜街口的追逐") -> str:
    resp = client.post("/api/v1/sessions", json={
        "mode": "director", "plot": plot, "duration": 4.0,
        "director_mode": mode, "director_node_id": "node-42", "canvases": {},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _detail(client, session_id: str) -> dict:
    return client.get(f"/api/v1/sessions/{session_id}").json()


def _pending(detail: dict, kind: str) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == kind and item["status"] == "pending"]


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
        detail = _detail(client, session_id)
        if detail["status"] != "running" and not any(
                job["status"] == "running" for job in detail["jobs"]):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"会话没有在 {timeout}s 内停下：{session_id}")


def _run_to_review(client, session_id: str) -> dict:
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    job = _wait_job(client, session_id, started.json()["job_id"])
    assert job["status"] == "succeeded", job
    review = _pending(_detail(client, session_id), "stage1_review")
    assert len(review) == 1
    return review[0]


def _upload(client, session_id: str, name: str = "start.png", *,
            end: bool = False) -> dict:
    ids = {}
    for kind, filename in (("start", name), ("end", f"end-{name}")):
        if kind == "end" and not end:
            continue
        resp = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": (filename, _png(), "image/png")})
        assert resp.status_code == 201, resp.text
        ids[kind] = resp.json()["asset_id"]
    return ids


def _submit_start_image(client, session_id: str, *, end: bool = False) -> dict:
    """跑到「交开场首帧」这一步并交图，等阶段 2 与自动打包停稳。"""
    review = _run_to_review(client, session_id)
    gate = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
        json={"decision": "approve"})
    assert gate.status_code == 202 and gate.json() is None
    item = _pending(_detail(client, session_id), "director_start_image")[0]
    assets = _upload(client, session_id, end=end)
    body = {"decision": "submit_start_image", "start_asset_id": assets["start"]}
    if "end" in assets:
        body["end_asset_id"] = assets["end"]
    accepted = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
        json=body)
    assert accepted.status_code == 202, accepted.text
    job = _wait_job(client, session_id, accepted.json()["job_id"])
    if job["status"] == "succeeded":
        _wait_idle(client, session_id)
    return job


def _interrupt_after_deleting_segment(root: Path, session_id: str) -> None:
    """制造「续写缺段」现场：删掉段词，把会话摆回可推进状态。"""
    directory = root / Path(session_id)
    segment_path(directory, 1).unlink()
    session_file = directory / SESSION_FILENAME
    raw = json.loads(session_file.read_text(encoding="utf-8"))
    raw["status"] = "interrupted"
    session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def _requests(model, kind: str, start: int = 0) -> list[str]:
    """按角色从出站请求里挑：规划与写段是两条不同的直调，各看各的。"""
    mark = PLANNER_MARK if kind == "planner" else WRITER_MARK
    return [body for body in model.requests[start:] if mark in body]


def _description_line(request: str) -> str:
    return next(line for line in request.splitlines()
                if line.startswith("画面描述："))


# ---------------------------------------------------------------------------
# 1. 规划 + 当轮构造点：两处请求都带起点画面事实
# ---------------------------------------------------------------------------

def test_planner_and_first_write_receive_the_opening_still_fact(workbench, monkeypatch):
    opening = "起点画面：窗在人物右后方，桌上一只白瓷杯，窗帘半掩"
    seen: list[Path] = []

    def fake_describe(image):
        seen.append(Path(image))
        return opening

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "i2v")
        job = _submit_start_image(client, session_id)
        assert job["status"] == "succeeded", job
        directory = root / Path(session_id)
        start_image = directory / "director" / "assets" / "01" / f"{START_STEM}.png"
        assert seen == [start_image], "读图应恰好落在这张开场首帧图上"

        planners = _requests(model, "planner")
        writers = _requests(model, "writer")
        assert planners and writers
        # 规划期：块 + 描述 + 纪律句 + planning 那半句（拿它来规划本段边界与落定画面）。
        # 三条同源事实一起断言（#326）：只查描述会漏掉「标题或纪律句丢了、模型不知道
        # 这堆字是画面事实」这种半接线。
        assert planners[-1].count(BLOCK_HEAD) == 1
        assert opening in planners[-1]
        assert FRAME_CONSISTENCY_DISCIPLINE in planners[-1]
        assert "规划本段的边界、summary 与 end_hook 时以它为准" in planners[-1]
        # 当轮构造点：首写请求带同一份描述、同一句纪律，块措辞切到「写本段正文时」。
        assert writers[-1].count(BLOCK_HEAD) == 1
        assert opening in writers[-1]
        assert FRAME_CONSISTENCY_DISCIPLINE in writers[-1]
        assert "写本段正文时以它为准" in writers[-1]


# ---------------------------------------------------------------------------
# 2. 续跑重建：续写看到的描述与首写逐字相同（且不重读）
# ---------------------------------------------------------------------------

def test_resume_write_uses_the_same_description_as_the_first_write(
        workbench, monkeypatch):
    opening = "起点画面：窗在人物身后，桌上没有杯子"
    calls: list[Path] = []

    def fake_describe(image):
        calls.append(Path(image))
        return opening

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "i2v")
        assert _submit_start_image(client, session_id)["status"] == "succeeded"

        first_write = _requests(model, "writer")[-1]
        assert opening in first_write
        _interrupt_after_deleting_segment(root, session_id)

        before = len(model.requests)
        resumed = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert resumed.status_code == 202, resumed.text
        assert _wait_job(
            client, session_id, resumed.json()["job_id"])["status"] == "succeeded"

    resumes = _requests(model, "writer", start=before)
    assert resumes, "续写缺段应真的调用一次写段"
    # 逐字同一句：续写的段词不许悄悄换口径（sidecar 命中，描述原样取回）。
    assert _description_line(resumes[-1]) == _description_line(first_write)
    # 续跑不再读图：同一张图只付一次视觉调用。
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 3. 不串话：A 规划 → B 规划 → A 续写，A 只看到自己的描述
# ---------------------------------------------------------------------------

def test_two_sessions_do_not_cross_contaminate(workbench, monkeypatch):
    """runner 是进程级单实例：A 续写不许读到 B 留下的描述。

    顺序就是这条用例的判据：A 先规划（若实现把描述挂到实例上，A 的值在此写入）、
    B 再规划（覆盖成 B 的值）、A 才续写缺段。正确实现按**本会话盘面**现算，A 拿到
    自己的那份；挂实例属性的实现会拿到 B 的残留值。
    """
    def fake_describe(image):
        # 会话目录名＝``sessions/<会话 id 首段>/GENxxx``：取 GENxxx 的上一层让两张图
        # 各自说不同的话（GENxxx 每个会话都叫这个名字，区分不了会话）。
        session_name = Path(image).parents[4].name
        return f"起点画面（{session_name}）：窗在人物身后的墙侧，窗帘半掩"

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    app, model, root = workbench
    with workbench_client(app) as client:
        # A：规划 + 写段，然后删段、摆回可推进，等 B 规划之后再续写。
        a = _create(client, "i2v", plot="甲会话雨夜街口")
        before_a = len(model.requests)
        assert _submit_start_image(client, a)["status"] == "succeeded"
        a_writes = [body for body in model.requests[before_a:]
                    if WRITER_MARK in body]
        _interrupt_after_deleting_segment(root, a)

        # B：另一个会话（同一张 runner），描述与 A 不同。
        b = _create(client, "i2v", plot="乙会话清晨车站")
        before_b = len(model.requests)
        assert _submit_start_image(client, b)["status"] == "succeeded"
        b_writes = [body for body in model.requests[before_b:]
                    if WRITER_MARK in body]

        a_name, b_name = Path(a).parts[0], Path(b).parts[0]
        assert a_writes and b_writes
        assert a_name in _description_line(a_writes[-1])
        assert b_name not in _description_line(a_writes[-1])
        assert b_name in _description_line(b_writes[-1])
        assert a_name not in _description_line(b_writes[-1])

        before = len(model.requests)
        resumed = client.post(f"/api/v1/sessions/{a}/jobs")
        assert resumed.status_code == 202, resumed.text
        assert _wait_job(
            client, a, resumed.json()["job_id"])["status"] == "succeeded"
        a_resume = _requests(model, "writer", start=before)

    assert a_resume, "A 的续写应真的调用一次写段"
    line = _description_line(a_resume[-1])
    assert a_name in line, "A 的续写没有按本会话盘面取回自己的描述"
    assert b_name not in line, "A 的续写读到了 B 会话的描述（实例属性串话）"


# ---------------------------------------------------------------------------
# 4. 失败：job 明确失败、病因点名是哪张图
# ---------------------------------------------------------------------------

def test_read_failure_fails_the_job_and_names_the_opening_image(
        workbench, monkeypatch):
    monkeypatch.setattr(
        reference_auditor, "describe_image",
        lambda _path: (_ for _ in ()).throw(RuntimeError("视觉服务不可用")))
    app, _model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "i2v")
        job = _submit_start_image(client, session_id)

        assert job["status"] == "failed", job
        directory = root / Path(session_id)
        failures = [
            json.loads(line) for line in
            (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("type") == "job.failed"
        ]
        detail = failures[-1]["payload"]["detail"]
        # 点名是哪张图 + 原样带回病因：只说「读图失败」用户不知道换哪一张。
        assert "开场首帧图读图失败" in detail
        assert "视觉服务不可用" in detail
        # 不静默降级成「无描述」：段表与段词都不落。
        assert not plan_path(directory).is_file()
        assert not segment_path(directory, 1).is_file()


def test_failed_image_names_which_picture(tmp_path, monkeypatch):
    """``DirectorStillFacts.failed_image``（#324 加性字段）：分出「哪张图」失败。"""
    from minimax_h3_prompt.ui.wizard_director import prepare_director_still_facts

    monkeypatch.setattr(
        reference_auditor, "describe_image",
        lambda _path: (_ for _ in ()).throw(RuntimeError("视觉服务不可用")))
    assets = tmp_path / "director" / "assets"
    (assets / "01").mkdir(parents=True)
    (assets / "01" / f"{START_STEM}.png").write_bytes(b"start")
    (assets / "film-end.png").write_bytes(b"film-end")

    # 两张都在、两处都读不出来：按取数顺序先报全片尾帧（与改前行为一致）。
    facts = prepare_director_still_facts(tmp_path, "fl2v")
    assert facts.ok is False and facts.failed_image == "全片尾帧"
    assert "视觉服务不可用" in facts.failure_reason
    # 只有开场首帧时点名它。
    (assets / "film-end.png").unlink()
    facts = prepare_director_still_facts(tmp_path, "fl2v")
    assert facts.ok is False and facts.failed_image == "开场首帧"
    assert "视觉服务不可用" in facts.failure_reason


# ---------------------------------------------------------------------------
# 5. 无图 / 模式无首帧槽：请求与不给描述时逐字节相同
# ---------------------------------------------------------------------------

def test_t2v_flow_never_reads_or_mentions_a_start_image(workbench):
    """t2v 没有首帧槽：不读图、请求里不出现起点块。"""
    app, model, root = workbench
    with workbench_client(app) as client:
        session_id = _create(client, "t2v")
        review = _run_to_review(client, session_id)
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        assert _wait_job(
            client, session_id, accepted.json()["job_id"])["status"] == "succeeded"
        _wait_idle(client, session_id)

    assert _requests(model, "planner") and _requests(model, "writer")
    assert all(BLOCK_HEAD not in body for body in model.requests)


def test_description_is_ignored_where_the_mode_has_no_start_slot():
    """规划与写段两条组装口：模式没有首帧槽时，带不带描述出站请求逐字节相同。"""
    from minimax_h3_prompt.director.prompts import build_segment_request

    model = DeterministicChatModel()
    from minimax_h3_prompt.director.planning import plan_director_segments

    plan_director_segments("[Shot 1] At 00:00.000，人物走进门廊", 4.0, model,
                           mode="t2v")
    baseline = model.requests[-1]
    plan_director_segments("[Shot 1] At 00:00.000，人物走进门廊", 4.0, model,
                           mode="t2v",
                           start_frame_description="起点画面：窗在人物身后")
    assert model.requests[-1] == baseline, "t2v 的规划请求被起点描述改动了"

    plan = DirectorPlan(mode="t2v", total_frames=96, segments=(
        DirectorSegment(
            index=1, type="opening", guided=False, start_frame=0, end_frame=96,
            declared_frames=96, export_frames=96,
            summary="开场", end_hook="人物走进门廊", give_images=()),
    ))
    segment = plan.segments[0]
    base_kwargs = {"shot_table": "[Shot 1] At 00:00.000，人物走进门廊"}
    baseline_request = build_segment_request(plan, segment, **base_kwargs)
    assert build_segment_request(
        plan, segment, **base_kwargs,
        start_frame=False, start_frame_description="") == baseline_request
    # 盘上有图、也有描述，但 t2v 的开场段没有首帧槽：判据问给图契约，不写模式字面量。
    assert build_segment_request(
        plan, segment, **base_kwargs,
        start_frame=True,
        start_frame_description="起点画面：窗在人物身后") == baseline_request
