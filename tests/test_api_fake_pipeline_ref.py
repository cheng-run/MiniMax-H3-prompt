"""D7-6（#259）：桩管线扩到 ref —— 素材循环 + 两段闭环 + 拼接。

判据面取 HTTP（与 Playwright 走同一套服务）与盘上产物，两层：

1. **闭环**：桩模式下 ref 会话从阶段 1 一路跑到 ``completed``，两件素材（第 1 件
   不通过重开、第 2 件一次通过）、两段、拼出 ``final.mp4``；全程不建模型
   （``build_chat_model`` 一调就炸）、不读图（桩不调 ``describe_image``）。
2. **互斥**：开关关掉时没有任何一条路径会伪造结论——出词走真实取数口，取不到就
   硬失败，而不是拿桩词顶替。

桩与真实的分界仍是 ``fake_pipeline_enabled()``（环境变量，默认关、无 HTTP 入口）。
CI 里跑通的是**桩**，不能写成真实模型可用。
"""
from __future__ import annotations

import json
import threading
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
    check_segment_video,
)
from minimax_h3_prompt.workbench_store import EVENT_TYPES
from workbench_client import workbench_client

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)
ENV = "MINIMAX_FAKE_PIPELINE"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 20 秒 = 2 × 10 秒：ref 的分段门槛是 15 秒（>15 才拆段），10 秒段落在 H3 单段
# 4–15 秒的整数档内。桩造件按 24fps 折算（帧数表只校准到 6 秒，见 fake_pipeline）。
REF_SECONDS = 20.0
SEGMENT_SECONDS = 10
CAT = "橘猫"
LAMP = "台灯"


def _png(seed: int = 0) -> bytes:
    return PNG_MAGIC + bytes([seed % 256]) * 4096


class _NoModel:
    """``build_chat_model`` 的替身：桩模式下一调就炸。"""

    def __call__(self):  # pragma: no cover - 被调到就是失败
        raise AssertionError("桩管线不许建真实模型")


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


def _pending(detail: dict, kind: str) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == kind and item["status"] == "pending"]


def _upload(client, session_id: str, payload: bytes, filename: str,
            content_type: str, name: str | None = None) -> dict:
    files = {"file": (filename, payload, content_type)}
    data = {} if name is None else {"name": name}
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets", files=files, data=data)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    """开桩的真实 FastAPI + 临时会话根；``build_chat_model`` 一调就炸。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(model_factory, "build_chat_model", _NoModel())
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, root


# ---------------------------------------------------------------------------
# 造件：4–15 秒整数档的桩段视频要过真实闸门
# ---------------------------------------------------------------------------

def test_stub_ref_segment_video_passes_the_real_gate(tmp_path):
    """10 秒桩段：帧数按 24fps 折算，真实闸门判它只有「无法确证档位」warning。"""
    video = fake_pipeline.write_ref_segment_video(tmp_path / "seg-10.mp4", 10)

    meta = video_meta(video)
    assert meta["frames"] == 10 * fake_pipeline.H3_VIDEO_FPS
    assert (meta["width"], meta["height"]) == EXPECTED_VIDEO_SIZE
    issues = check_segment_video(video, expected_seconds=10)
    assert [issue for issue in issues if issue.severity == "error"] == []
    codes = {issue.code for issue in issues}
    # 7–15 秒没有实测帧数样本：只给 warning，桩不冒充「档位已核对」。
    assert "segment_video_duration_unknown_tier" in codes


def test_stub_ref_segment_video_refuses_out_of_range(tmp_path):
    """3 秒 / 16 秒都不在 H3 单段 4–15 秒档内：桩不造，也不静默截断。"""
    for seconds in (3, 16):
        with pytest.raises(ValueError, match="多参执行段"):
            fake_pipeline.write_ref_segment_video(tmp_path / f"seg-{seconds}.mp4", seconds)
        assert not (tmp_path / f"seg-{seconds}.mp4").exists()


# ---------------------------------------------------------------------------
# 素材循环：出词 → 交图 → 不通过重开 → 再交 → 通过 → 下一件
# ---------------------------------------------------------------------------

def test_fake_ref_closed_loop_rejects_then_settles(fake_app, tmp_path):
    """20 秒 / 2 件素材 / 2 段：桩模式跑完素材循环、段循环与拼接。

    这一条同时钉三件事：不通过时**同一件号**重开且差异清单有内容；通过后自动进下
    一件；拼接走多参的 stream-copy 而非首尾帧链的报首帧（成片帧数 = 两段帧数之和）。
    """
    app, root = fake_app
    assert app.state.job_runner.fake_pipeline is True

    with workbench_client(app) as client:
        created = client.post("/api/v1/sessions", json={
            "mode": "ref", "variant": "T2VA", "duration": REF_SECONDS,
            "style": "写实", "language": "Chinese", "plot": "雨夜旧信的多参闭环"})
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        _upload(client, session_id, _png(1), "cat.png", "image/png", name=CAT)
        _upload(client, session_id, _png(2), "lamp.png", "image/png", name=LAMP)

        started = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert started.status_code == 202, started.text
        detail = _wait_until(
            client, session_id, lambda body: _pending(body, "stage1_review"))
        review = _pending(detail, "stage1_review")[0]
        client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})

        # 第 1 件：出词 job 落 assets/picture-01.md，挂 asset_image 待办。
        detail = _wait_until(
            client, session_id,
            lambda body: _pending(body, "asset_image")
            and any(item["asset_index"] == 1 for item in _pending(body, "asset_image")))
        artifacts = {item["name"]: item for item in detail["artifacts"]}
        assert artifacts["assets/picture-01.md"]["kind"] == "asset_prompt"
        word = (root / Path(session_id) / "assets" / "picture-01.md").read_text(
            encoding="utf-8")
        assert CAT in word and "桩管线固定词" in word, "桩词仍走真实落盘路径"

        # 第 1 次交图 → 校对不通过 → 同一件号重开，差异清单提到词与图的差别。
        first_image = _upload(client, session_id, _png(11), "cat-1.png", "image/png")
        todo = _pending(detail, "asset_image")[0]
        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{todo['item_id']}/resolve",
            json={"asset_id": first_image["asset_id"]})
        assert resp.status_code == 202, resp.text
        _wait_job(client, session_id, resp.json()["job_id"])
        detail = _wait_until(
            client, session_id,
            lambda body: [item for item in _pending(body, "asset_image")
                          if item["asset_index"] == 1 and item["reason"]])
        reopened = [item for item in _pending(detail, "asset_image")
                    if item["asset_index"] == 1][0]
        assert "差异" in reopened["reason"] and "伞" in reopened["reason"]
        # 不通过不是「落定」：台账轮次 +1、状态仍未通过。
        ledger = (root / Path(session_id) / "session-state.json").read_text(
            encoding="utf-8")
        assert '"round": 1' in ledger

        # 第 2 次交图 → 通过 → 自动出第 2 件的词。
        second_image = _upload(client, session_id, _png(12), "cat-2.png", "image/png")
        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{reopened['item_id']}/resolve",
            json={"asset_id": second_image["asset_id"]})
        assert resp.status_code == 202, resp.text
        _wait_job(client, session_id, resp.json()["job_id"])
        detail = _wait_until(
            client, session_id,
            lambda body: any(item["asset_index"] == 2 for item in _pending(body, "asset_image")))
        artifacts = {item["name"]: item for item in detail["artifacts"]}
        assert "assets/picture-02.md" in artifacts

        # 第 2 件一次通过 → 素材循环收尾 → 阶段 2 → 2 段 × 10 秒。
        third_image = _upload(client, session_id, _png(13), "lamp-1.png", "image/png")
        todo2 = [item for item in _pending(detail, "asset_image")
                 if item["asset_index"] == 2][0]
        resp = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{todo2['item_id']}/resolve",
            json={"asset_id": third_image["asset_id"]})
        assert resp.status_code == 202, resp.text
        _wait_job(client, session_id, resp.json()["job_id"])
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and len(body.get("segments") or []) == 2)

        # 整片注入卡在产物分区（kind 由登记表给），段内只读子集按段落位。
        artifacts = {item["name"]: item for item in detail["artifacts"]}
        assert artifacts["injection-card.md"]["kind"] == "injection_card"
        card = (root / Path(session_id) / "injection-card.md").read_text(encoding="utf-8")
        assert CAT in card and LAMP in card, "注入卡按台账列出两件素材"
        assert [(seg["index"], seg["start_s"], seg["end_s"])
                for seg in detail["segments"]] == [(1, 0, 10), (2, 10, 20)]
        # 严格串行：第 1 段写完就停下来收视频，第 2 段还是待写（pending）。
        assert [seg["status"] for seg in detail["segments"]] == ["waiting_video", "pending"]
        assert [asset["name"] for asset in detail["segments"][0]["assets"]] == [CAT]
        assert [asset["name"] for asset in detail["segments"][1]["assets"]] == [LAMP]
        assert artifacts["segments/segment-01.md"]["kind"] == "segment_prompt"
        assert "segments/order-card.md" in artifacts
        assert "segments/ref-shot-table.md" in artifacts

        # 逐段交桩视频（4–15 整数秒档，真实技术闸门判过）→ 拼接 → completed。
        for number in (1, 2):
            todo = [item for item in _pending(detail, "segment_video")
                    if item["segment_index"] == number][0]
            video = fake_pipeline.write_ref_segment_video(
                tmp_path / f"seg-{number:02d}.mp4", SEGMENT_SECONDS)
            video_asset = _upload(
                client, session_id, video.read_bytes(), video.name, "video/mp4")
            response = client.post(
                f"/api/v1/sessions/{session_id}/action-items/{todo['item_id']}/resolve",
                json={"video_asset_id": video_asset["asset_id"]})
            assert response.status_code == 202, response.text
            job = _wait_job(client, session_id, response.json()["job_id"])
            assert job["status"] == "succeeded", job
            detail = _wait_until(
                client, session_id,
                lambda body, n=number: body["status"] != "running" and (
                    body["status"] == "completed"
                    if n == 2 else
                    any(item["segment_index"] == n + 1
                        for item in _pending(body, "segment_video"))))

        assert detail["status"] == "completed"
        film = client.get(f"/api/v1/artifacts/{session_id}/final.mp4/content")
        assert film.status_code == 200, film.text
        assert film.headers["content-type"] == "video/mp4"
        assert film.content[4:8] == b"ftyp"

    final_path = root / Path(session_id) / "final.mp4"
    assert final_path.is_file()
    # 多参拼接口径是 stream-copy（首尾帧链才裁首帧）：成片帧数＝两段之和，一帧不丢。
    assert video_meta(final_path)["frames"] == 2 * SEGMENT_SECONDS * fake_pipeline.H3_VIDEO_FPS
    assert [seg["status"] for seg in detail["segments"]] == ["passed", "passed"]

    types = [event["type"] for event in _events(root, session_id)]
    assert set(types) <= set(EVENT_TYPES)
    assert len(EVENT_TYPES) == 10
    assert "job.failed" not in types


# ---------------------------------------------------------------------------
# 桩与真实互斥：开关关掉时没有一条路径伪造结论
# ---------------------------------------------------------------------------

def test_fake_switch_off_never_fabricates_conclusions(tmp_path, monkeypatch):
    """env 关掉 → runner 不是桩；出词走真实取数口，取不到就硬失败。

    这一条不跑真实阶段 1（那要模型）：只确认**桩的词没有被拿来顶替**——真实取数口
    ``asset_prompt_for`` 找不到语义名时抛 ``AssetPromptMissing``，而且盘上不会出现
    任何 ``assets/picture-NN.md``。
    """
    from minimax_h3_prompt.api.jobs import JobRunner
    from minimax_h3_prompt.generation import AssetPromptMissing

    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    runner = app.state.job_runner
    assert isinstance(runner, JobRunner)
    assert runner.fake_pipeline is False
    assert fake_pipeline.fake_pipeline_enabled() is False

    with workbench_client(app) as client:
        created = client.post("/api/v1/sessions", json={
            "mode": "ref", "variant": "T2VA", "duration": REF_SECONDS,
            "style": "写实", "language": "Chinese", "plot": "点开关关掉"})
        session_id = created.json()["session_id"]
        _upload(client, session_id, _png(1), "cat.png", "image/png", name=CAT)

        with pytest.raises(AssetPromptMissing):
            runner._execute_asset_write(
                session_id, "job", threading.Event(), 1)

    assert not (root / Path(session_id) / "assets" / "picture-01.md").exists()
