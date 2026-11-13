"""D6-7（#250）：桩管线造合规段视频 + 三段 CI 全闭环。

两层判据：

1. **造件**：桩用 ffmpeg 现造的 6 秒档视频必须是 H3 实测档位（158 帧 / 736×1280），
   并且过得了**真实的** ``preflight.check_segment_video``——为了「让桩过」去放宽闸门
   等于让 CI 假绿。
2. **闭环**：18 秒 / 3 段走完规划 → 写词 → 回交 → 校验 → 拼接，``final.mp4`` 存在
   且可读；全程不建模型、不碰 ComfyUI、不读 ``output/accept-*``（D6-AC13/AC15）。

段长必须是 H3 真能生成的档位（4/5/6 秒，2026-11-12 用户裁定）：桩规划不再按 10 秒
上限等分，否则 8/9 秒的段在真实闸门上只能拿到「无法确证档位」的 warning。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api import fake_pipeline
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.tools.preflight import (
    EXPECTED_VIDEO_SIZE,
    H3_TIER_FRAMES,
    check_segment_video,
)
from minimax_h3_prompt.workbench_store import EVENT_TYPES
from workbench_client import workbench_client

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)
ENV = "MINIMAX_FAKE_PIPELINE"

# 18 秒 = 3 段 × 6 秒（H3 的 4/5/6 秒档里最大的那一档），正好覆盖「三段闭环」。
LONGFORM_SECONDS = 18.0


def _brief_body(duration: float = LONGFORM_SECONDS) -> dict:
    return {
        "mode": "base", "variant": "T2VA", "duration": duration,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信的雨夜街口",
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


def _wait_until(client, session_id: str, predicate, timeout: float = 60.0) -> dict:
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


def _upload(client, session_id: str, path: Path) -> str:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (path.name, path.read_bytes(), "video/mp4")})
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_id"]


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_stub_segment_video_passes_the_real_gate(tmp_path):
    """6 秒档造件：帧数/尺寸从仓内常量派生，真实闸门判它没有 error。

    这一条直接拿 ``check_segment_video``（生产闸门，不是测试重写的副本）判桩产物：
    桩造的视频若不合规，CI 里的「段校验通过」就是假的。
    """
    video = fake_pipeline.write_segment_video(tmp_path / "seg-06.mp4", 6)

    meta = video_meta(video)
    assert meta["frames"] == H3_TIER_FRAMES[6]
    assert (meta["width"], meta["height"]) == EXPECTED_VIDEO_SIZE
    issues = check_segment_video(video, expected_seconds=6)
    assert [issue for issue in issues if issue.severity == "error"] == []


def test_stub_segment_plan_uses_only_h3_tier_lengths():
    """桩规划的每段时长都落在 H3 实测档位（4/5/6），且无缝覆盖总时长。

    判据不是「拆成几段」而是「拆出来的段 H3 真能生成」：按 10 秒上限等分会拆出
    8/9 秒的段，真实闸门只能给「无法确证档位」的 warning，CI 就证不了档位合规。
    """
    for total in (11, 12, 13, 18, 23, 24, 60):
        plans = fake_pipeline.segment_plan(total)
        durations = [plan.duration_s for plan in plans]
        assert sum(durations) == total, (total, durations)
        assert set(durations) <= set(H3_TIER_FRAMES), (total, durations)
        assert [plan.start_s for plan in plans] == [0, *[p.end_s for p in plans[:-1]]]


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


def test_fake_pipeline_runs_the_three_segment_closed_loop(fake_app, tmp_path):
    """18 秒 / 3 段：规划 → 写词 ×3 → 回交 ×3 → 校验 ×3 → 拼接，final.mp4 可读。

    「用户去 ComfyUI 跑」这一步由桩管线的 ffmpeg 造件替身，其余每一环走真实代码：
    上传素材、resolve 闸门（真实 ``check_segment_video``）、剥尾帧、段状态推进、
    ``assemble_film`` 拼接。全程不建模型、不调 ComfyUI。
    """
    app, root = fake_app
    assert app.state.job_runner.fake_pipeline is True

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        started = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])

        review = [item for item in client.get(
            f"/api/v1/sessions/{session_id}").json()["action_items"]
            if item["status"] == "pending"][0]
        approved = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        assert approved.status_code == 202, approved.text

        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and _pending_segment_todos(body))
        # 规划 → 写第 1 段：段表 3 段 × 6 秒（H3 可生成档位）。
        assert [(seg["index"], seg["start_s"], seg["end_s"])
                for seg in detail["segments"]] == [(1, 0, 6), (2, 6, 12), (3, 12, 18)]

        submitted: list[str] = []
        for number in (1, 2, 3):
            todo = [item for item in _pending_segment_todos(detail)
                    if item["segment_index"] == number][0]
            segment = [seg for seg in detail["segments"]
                       if seg["index"] == number][0]
            seconds = float(segment["end_s"] - segment["start_s"])
            # 桩自己现造本段视频：参数从段表与仓内常量派生，交回时走真实闸门。
            video = fake_pipeline.write_segment_video(
                tmp_path / f"seg-{number:02d}.mp4", seconds)
            assert not [issue for issue in check_segment_video(video, seconds)
                        if issue.severity == "error"], "桩产物过不了真实段闸门"
            asset_id = _upload(client, session_id, video)
            response = client.post(
                f"/api/v1/sessions/{session_id}/action-items/"
                f"{todo['item_id']}/resolve",
                json={"video_asset_id": asset_id})
            assert response.status_code == 202, response.text
            job = _wait_job(client, session_id, response.json()["job_id"])
            assert job["status"] == "succeeded", job
            submitted.append(asset_id)
            if number < 3:
                detail = _wait_until(
                    client, session_id,
                    lambda body, n=number: body["status"] != "running" and any(
                        item["segment_index"] == n + 1
                        for item in _pending_segment_todos(body)))

        detail = _wait_until(
            client, session_id, lambda body: body["status"] == "completed")
        film = client.get(f"/api/v1/artifacts/{session_id}/final.mp4/content")

    # 成片存在、已登记为既有 video 产物、容器可读。
    assert film.status_code == 200, film.text
    assert film.headers["content-type"] == "video/mp4"
    assert film.content[4:8] == b"ftyp"
    final_path = root / Path(session_id) / "final.mp4"
    assert final_path.is_file()
    assert video_meta(final_path)["frames"] > 0, "final.mp4 读不出一帧"
    assert {entry["name"]: entry["kind"] for entry in detail["artifacts"]}["final.mp4"] == "video"

    assert [seg["status"] for seg in detail["segments"]] == ["passed"] * 3
    assert [seg["video_asset_id"] for seg in detail["segments"]] == submitted

    types = [event["type"] for event in _events(root, session_id)]
    # 十种事件闭合：段循环挤进既有事件，不新增类型（D6 §11）。
    assert set(types) <= set(EVENT_TYPES)
    assert len(EVENT_TYPES) == 10
    assert "job.failed" not in types
