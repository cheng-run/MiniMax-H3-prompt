"""D6-1（#244）：长片在阶段 2 之后自动分段规划。

判据分两层：**接线**（长片自动起 job、stage=segmenting、plan.json 落本会话目录）
与**复用**（真的走 ``segment_planner.plan_segments`` 的 base 档，而不是另写一套拆段）。
短视频那条路径必须一字不变：不出现分段、不产生规划任务、详情里 ``segments`` 为空。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, segment_planner
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=1)

ENV = "MINIMAX_FAKE_PIPELINE"


def _brief_body(duration: float, variant: str = "T2VA") -> dict:
    return {
        "mode": "base",
        "variant": variant,
        "duration": duration,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
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
    """轮询详情直到条件成立（自动接续的规划任务没有 POST 返回值可等）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        if predicate(detail):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{session_id}")


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    return root, model


def _run_to_stage2(client, session_id: str) -> None:
    """T2VA：阶段 1 → 审查通过 → 阶段 2 跑完（不涉及帧待办）。"""
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    item = [entry for entry in detail["action_items"] if entry["status"] == "pending"][0]
    approved = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item['item_id']}/resolve",
        json={"decision": "approve"})
    assert approved.status_code == 202, approved.text
    _wait_job(client, session_id, approved.json()["job_id"])


def _read_session(root: Path, session_id: str) -> dict:
    return json.loads(
        (root / Path(session_id) / SESSION_FILENAME).read_text(encoding="utf-8"))


def _read_events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fake_workbench(monkeypatch, root: Path) -> None:
    """桩管线：不建任何模型，规划也走确定性桩（与 D4 的桩口径一致）。"""
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: (_ for _ in ()).throw(AssertionError("桩管线不许建真实模型")))


def test_long_form_auto_plans_segments_after_stage2(workbench, monkeypatch):
    root, _model = workbench
    _fake_workbench(monkeypatch, root)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)

    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body(12.0)).json()
        session_id = session["session_id"]
        _run_to_stage2(client, session_id)

        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and len(body.get("segments") or []) > 0)

    segments = detail["segments"]
    # 12 秒按 4–10 秒拆：ceil(12/10)=2 段，末段吸收余数。
    assert [segment["index"] for segment in segments] == [1, 2]
    assert segments[0]["start_s"] == 0
    assert segments[-1]["end_s"] >= 12
    for segment in segments:
        assert 4 <= segment["end_s"] - segment["start_s"] <= 10
        assert segment["status"] == "pending"
        assert segment["end_hook"]
        assert segment["summary"]
        assert segment["prompt_artifact_id"] == ""
        assert segment["video_asset_id"] == ""
    # 规划完不要求人工确认：没有未决待办，会话停在等待人工（D6-2 接第 1 段写词）。
    assert detail["status"] == "awaiting_human"
    assert [item for item in detail["action_items"] if item["status"] == "pending"] == []

    # plan.json 落在**本会话目录**里，形状与 CLI 的 segments/plan.json 一致。
    plan_path = root / Path(session_id) / "segments" / "plan.json"
    plans = json.loads(plan_path.read_text(encoding="utf-8"))
    assert [plan["start_s"] for plan in plans] == [segment["start_s"] for segment in segments]
    assert plans[-1]["end_hook"] == segments[-1]["end_hook"]

    # 进度事件的 stage 取新值；任务记录里能看到规划 job（kind=segment）。
    progress = [event["payload"] for event in _read_events(root, session_id)
                if event["type"] == "job.progress"]
    assert any(payload["stage"] == "segmenting" for payload in progress)
    assert any(job["kind"] == "segment" for job in detail["jobs"])


def test_short_form_is_untouched(workbench, monkeypatch):
    root, _model = workbench
    _fake_workbench(monkeypatch, root)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)

    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body(8.0)).json()
        session_id = session["session_id"]
        _run_to_stage2(client, session_id)
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert detail["status"] == "completed"
    assert detail["segments"] == []
    assert all(job["kind"] != "segment" for job in detail["jobs"])
    assert not (root / Path(session_id) / "segments" / "plan.json").exists()
    stages = {event["payload"]["stage"] for event in _read_events(root, session_id)
              if event["type"] == "job.progress"}
    assert "segmenting" not in stages


def test_planning_reuses_plan_segments_with_base_mode(workbench, monkeypatch):
    """复用而非另写：入参是阶段 1 的 shot_table，mode=base，页面上用的是真实段表。"""
    root, _model = workbench
    monkeypatch.delenv(ENV, raising=False)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    calls: list[dict] = []

    def fake_planner(shot_table, total_s, llm, **kwargs):
        calls.append({"shot_table": shot_table, "total_s": total_s, **kwargs})
        return [
            SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,),
                        summary="开场", end_hook="人物停在门口"),
            SegmentPlan(index=1, start_s=6, end_s=12, shots_in_segment=(2,),
                        summary="收束", end_hook="灯亮起"),
        ]

    monkeypatch.setattr(segment_planner, "plan_segments", fake_planner)

    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body(12.0)).json()
        session_id = session["session_id"]
        _run_to_stage2(client, session_id)
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and len(body.get("segments") or []) > 0)

    assert len(calls) == 1
    assert "[Shot 1]" in calls[0]["shot_table"]
    assert calls[0]["total_s"] == 12.0
    assert calls[0]["mode"] == "base"
    # 覆盖镜头原样带进详情（规划器给什么就是什么，不重算）。
    assert [segment["shots_in_segment"] for segment in detail["segments"]] == [[1], [2]]


def test_unusable_plan_fails_and_retry_reruns_only_planning(workbench, monkeypatch):
    """规划回复不合格 → failed；重试只重跑规划，不重跑阶段 2。"""
    root, _model = workbench
    monkeypatch.delenv(ENV, raising=False)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    stage2_calls: list[int] = []
    real_run_stage2 = pipeline.run_stage2

    def counting_run_stage2(*args, **kwargs):
        stage2_calls.append(1)
        return real_run_stage2(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_stage2", counting_run_stage2)
    answers = [None, [
        SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,),
                    summary="开场", end_hook="人物停在门口"),
        SegmentPlan(index=1, start_s=6, end_s=12, shots_in_segment=(2,),
                    summary="收束", end_hook="灯亮起"),
    ]]
    monkeypatch.setattr(
        segment_planner, "plan_segments",
        lambda *args, **kwargs: answers.pop(0))

    with workbench_client(app) as client:
        session = client.post("/api/v1/sessions", json=_brief_body(12.0)).json()
        session_id = session["session_id"]
        _run_to_stage2(client, session_id)
        failed = _wait_until(
            client, session_id, lambda body: body["status"] == "failed")

        retried = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert retried.status_code == 202, retried.text
        _wait_job(client, session_id, retried.json()["job_id"])
        done = client.get(f"/api/v1/sessions/{session_id}").json()

    assert failed["segments"] == []
    assert len(stage2_calls) == 1, "重试把阶段 2 又跑了一遍"
    assert len(done["segments"]) == 2
    assert done["status"] == "awaiting_human"
