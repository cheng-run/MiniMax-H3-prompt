"""D6（#248）：拼接与交付纵切——末段通过自动拼成 ``final.mp4`` 并收成 completed。

钉住五件事：
1. 最后一段通过后**自动**起拼接 job（``stage=segmenting``），用户不需要点任何按钮。
2. 拼接成功产出 ``final.mp4``、登记为既有 ``video`` 产物、可按 Range 读取；会话转
   ``completed``，**没有交付确认闸门**（D4 已裁定）。
3. 缺段不拼半成品：``assemble_film`` 的 reason 原样进 ``job.failed`` 的 detail（它已点名
   第几段），会话 ``failed``、盘上没有 ``final.mp4``——不许改写成泛泛的「拼接失败」。
4. 补齐后走既有重试机制（``POST /jobs``）续拼成功，已通过段不重收、不重传。
5. 工作台不读也不写 CLI 的 ``segments/shot-ledger.jsonl``。

「缺段」怎么构造：段表来自本会话的 ``segments/plan.json``，而 CLI 与工作台共用目录
布局（spec §10「同一个会话目录若曾被 CLI 跑过」）。CLI 在同一目录写下自己的一套段表
时，plan.json 的段数会多于快照里已通过的段——这正是 ``assemble_film`` 的点名缺段分支。
拼接真跑 ffmpeg（``TRIM_FIRST_FRAME`` 重编码），造件用 ``write_h3_tier_mp4``。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts
from minimax_h3_prompt import segment_validation
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.tools import frame_auditor
from minimax_h3_prompt.workbench_store import EVENT_TYPES
from conftest import write_h3_tier_mp4
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

# 两段 6 秒（H3 6 秒档 = 158 帧）：24 秒的真实长片太重，3 段里两段就够证明「末段触发，
# 而不是第一段就拼」。
PLANS = (
    SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,),
                summary="开场", end_hook="人物停在门口"),
    SegmentPlan(index=1, start_s=6, end_s=12, shots_in_segment=(2,),
                summary="收束", end_hook="灯亮起"),
)

# 同一目录里 CLI 写下的另一套段表（3 段）：工作台快照里只有两段通过，第 3 段缺。
CLI_PLANS = PLANS + (
    SegmentPlan(index=2, start_s=12, end_s=18, shots_in_segment=(3,),
                summary="尾段", end_hook="镜头拉远"),
)


def _brief_body(duration: float = 12.0, variant: str = "T2VA",
                plot: str = "雨夜旧信") -> dict:
    return {
        "mode": "base", "variant": variant, "duration": duration,
        "style": "写实", "language": "Chinese", "plot": plot,
    }


def _wait_job(client, session_id: str, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


def _wait_until(client, session_id: str, predicate, timeout: float = 15.0) -> dict:
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


def _resolve(client, session_id: str, item_id: str, body: dict):
    return client.post(
        f"/api/v1/sessions/{session_id}/action-items/{item_id}/resolve", json=body)


def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _session_dir(root: Path, session_id: str) -> Path:
    return root / Path(session_id)


@pytest.fixture
def loop_app(tmp_path, monkeypatch):
    """临时会话根 + 确定性模型；规划/写段/语义判据都打在既有函数上。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"

    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: list(PLANS))
    monkeypatch.setattr(
        segment_prompts, "write_segment_v2",
        lambda plan, all_plans, state, llm:
            f"[Shot 1] At 00:00.000, segment {plan.index + 1} stub prompt.\n")
    # 语义闸门：尾帧读图与 end_hook 判据都放行（本票只关心拼接，不重测 #246 的三结局）。
    monkeypatch.setattr(
        frame_auditor, "describe_bridge_frame",
        lambda _path: "尾帧画面：人物停在门口，灯光昏黄")
    monkeypatch.setattr(
        segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: True)
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, root


def _to_first_segment_todo(client, session_id: str) -> dict:
    """跑完阶段 1 → 审查通过 → 阶段 2 → 规划 → 写第 1 段，停在第一段待办。"""
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    review = [item for item in client.get(
        f"/api/v1/sessions/{session_id}").json()["action_items"]
        if item["status"] == "pending"][0]
    approved = _resolve(client, session_id, review["item_id"], {"decision": "approve"})
    assert approved.status_code == 202, approved.text
    return _wait_until(
        client, session_id,
        lambda body: body["status"] != "running" and _pending_segment_todos(body))


def _submit_segment(client, session_id: str, detail: dict, number: int,
                    video: Path) -> tuple[str, dict]:
    """交第 ``number`` 段视频并等这一次校验 job 收尾；返回 (asset_id, job)。"""
    todo = [item for item in _pending_segment_todos(detail)
            if item["segment_index"] == number][0]
    asset_id = _upload(client, session_id, video)
    started = _resolve(client, session_id, todo["item_id"],
                       {"video_asset_id": asset_id})
    assert started.status_code == 202, started.text
    job = _wait_job(client, session_id, started.json()["job_id"])
    return asset_id, job


def _write_plan_file(directory: Path, plans) -> None:
    target = directory / "segments" / "plan.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([plan.to_dict() for plan in plans], ensure_ascii=False, indent=2),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# 末段通过 → 自动拼接 → final.mp4 → completed（无交付确认）
# ---------------------------------------------------------------------------

def test_last_segment_auto_assembles_final_film_and_completes(
        loop_app, tmp_path):
    app, root = loop_app
    video_a = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    video_b = write_h3_tier_mp4(tmp_path / "seg02.mp4", 6)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        first_asset, _ = _submit_segment(client, session_id, detail, 1, video_a)
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and any(
                seg["status"] == "waiting_video" and seg["index"] == 2
                for seg in body["segments"]))

        # 第二段（末段）通过后不需要任何用户动作：拼接由服务端自己接着跑。
        second_asset, validation_job = _submit_segment(
            client, session_id, detail, 2, video_b)
        assert validation_job["status"] == "succeeded"
        detail = _wait_until(
            client, session_id, lambda body: body["status"] == "completed")

        # final.mp4 是既有 video 产物，可按 Range 读。
        content = client.get(
            f"/api/v1/artifacts/{session_id}/final.mp4/content")
        assert content.status_code == 200, content.text
        assert content.headers["content-type"] == "video/mp4"
        assert content.content[4:8] == b"ftyp"
        ranged = client.get(
            f"/api/v1/artifacts/{session_id}/final.mp4/content",
            headers={"Range": "bytes=0-99"})
        assert ranged.status_code == 206
        assert ranged.headers["content-range"].startswith("bytes 0-99/")
        assert len(ranged.content) == 100

        listed = {entry["name"]: entry["kind"] for entry in detail["artifacts"]}
        assert listed["final.mp4"] == "video"

    directory = _session_dir(root, session_id)
    assert (directory / "final.mp4").is_file()
    # 终态就是 completed：没有交付确认待办，也没有待办被挂出来。
    assert all(item["status"] != "pending" for item in detail["action_items"])
    assert [seg["video_asset_id"] for seg in detail["segments"]] == [
        first_asset, second_asset]
    assert all(seg["status"] == "passed" for seg in detail["segments"])

    events = _events(root, session_id)
    types = [event["type"] for event in events]
    # 十种闭合不变：拼接只复用既有事件类型。
    assert set(types) <= set(EVENT_TYPES)
    assert len(EVENT_TYPES) == 10
    assert "artifact.created" in types
    # 拼接是 segmenting 阶段的一个 job，不是新阶段、也不是新会话状态。
    stages = {(event["payload"].get("stage"), event["payload"].get("node"))
              for event in events if event["type"] == "job.progress"}
    assert ("segmenting", "segment_assemble") in stages


# ---------------------------------------------------------------------------
# 缺段：failed + 点名段号；补齐后重试成功，已通过段不重收
# ---------------------------------------------------------------------------

def test_missing_segment_fails_with_named_segment_and_retry_after_fix(
        loop_app, tmp_path):
    app, root = loop_app
    video_a = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    video_b = write_h3_tier_mp4(tmp_path / "seg02.mp4", 6)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        first_asset, _ = _submit_segment(client, session_id, detail, 1, video_a)
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and any(
                seg["status"] == "waiting_video" and seg["index"] == 2
                for seg in body["segments"]))

        # 同一会话目录里出现了 CLI 自己的一套段表（3 段）：工作台只有两段已通过。
        _write_plan_file(_session_dir(root, session_id), CLI_PLANS)
        second_asset, validation_job = _submit_segment(
            client, session_id, detail, 2, video_b)
        assert validation_job["status"] == "succeeded"
        failed = _wait_until(
            client, session_id, lambda body: body["status"] == "failed")

    directory = _session_dir(root, session_id)
    # 缺一段的成片是伪产物：盘上不许出现半成品。
    assert not (directory / "final.mp4").exists()
    assert [seg["status"] for seg in failed["segments"]] == ["passed", "passed"]

    failure = [event for event in _events(root, session_id)
               if event["type"] == "job.failed"][-1]
    # reason 原样保留：点名缺的是哪一段，而不是泛泛的「拼接失败」。
    assert "第 3 段" in failure["payload"]["detail"]
    assert failure["payload"]["detail"] != "拼接失败"
    assert failure["payload"]["code"] == "segment_assembly_refused"

    # 补齐（把段表恢复成本会话那一套）后用既有重试机制续拼。
    _write_plan_file(directory, PLANS)
    with workbench_client(app) as client:
        retried = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert retried.status_code == 202, retried.text
        _wait_job(client, session_id, retried.json()["job_id"])
        detail = _wait_until(
            client, session_id, lambda body: body["status"] == "completed")

    assert (directory / "final.mp4").is_file()
    # 已通过的段不重收、不重传：素材还是原来两份，也没有新的段待办。
    assert [seg["video_asset_id"] for seg in detail["segments"]] == [
        first_asset, second_asset]
    assert _pending_segment_todos(detail) == []


# ---------------------------------------------------------------------------
# 不读也不写 CLI 的段产出账
# ---------------------------------------------------------------------------

def test_forged_shot_ledger_is_neither_read_nor_written(loop_app, tmp_path):
    app, root = loop_app
    video_a = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    video_b = write_h3_tier_mp4(tmp_path / "seg02.mp4", 6)
    # 一份「看起来完整但全是假的」CLI 账：真去读它就会拿这些行去拼接（路径根本不存在）。
    forged = json.dumps({
        "event": "output", "segment": 1,
        "video": str(tmp_path / "does-not-exist.mp4"), "sha256": "0" * 64,
    }, ensure_ascii=False) + "\n"

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        directory = _session_dir(root, session_id)
        (directory / "segments" / "shot-ledger.jsonl").write_text(
            forged * 2, encoding="utf-8")

        _submit_segment(client, session_id, detail, 1, video_a)
        detail = _wait_until(
            client, session_id,
            lambda body: body["status"] != "running" and any(
                seg["status"] == "waiting_video" and seg["index"] == 2
                for seg in body["segments"]))
        _submit_segment(client, session_id, detail, 2, video_b)
        _wait_until(client, session_id, lambda body: body["status"] == "completed")

    # 读它会把假路径拼进来（必然失败）；写它会篡改用户目录里的 CLI 账。
    assert (directory / "segments" / "shot-ledger.jsonl").read_text(
        encoding="utf-8") == forged * 2
    assert (directory / "final.mp4").is_file()
