"""D6-6（#249）：长片段的恢复与中断。

钉住五条恢复不变量（父 spec §10 / D6-AC11、AC12）：

1. 段状态是**快照事实**：每次段通过/被拒/重建待办都立刻落盘，重启不需要从事件流补。
2. 重启后从第一条未通过的段继续；已通过段不重跑写词、不重收视频。
3. 被拒的段重启后仍是「待交视频」，不被当成已通过。
4. 执行中被杀 → 走既有 ``interrupted`` 对账；用户「重试」从断点续跑（校验重跑、
   素材不重交；未写词的段重起写词 job）。

另一条边界属于 D6-AC12：工作台**不读也不写** CLI 的 ``segments/shot-ledger.jsonl``。
用例放一份**合法形状、声称全部已通过**的伪造账，再顺手把读/写口打桩成一调就炸，
两个方向一起咬：段进度只认会话快照。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts
from minimax_h3_prompt import segment_validation
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.tools import frame_auditor
from conftest import write_h3_tier_mp4
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

# 三段各 4/5/5 秒，全部落在 H3 帧数档上（技术档闸门要判得出档位）。
PLANS = (
    SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                summary="开场", end_hook="人物停在门口"),
    SegmentPlan(index=1, start_s=4, end_s=9, shots_in_segment=(2,),
                summary="推进", end_hook="灯亮起"),
    SegmentPlan(index=2, start_s=9, end_s=14, shots_in_segment=(2,),
                summary="收束", end_hook="门打开"),
)


def _brief_body(duration: float = 14.0) -> dict:
    return {
        "mode": "base", "variant": "T2VA", "duration": duration,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信",
    }


def _detail(client, session_id: str) -> dict:
    return client.get(f"/api/v1/sessions/{session_id}").json()


def _pending_segment_todos(detail: dict) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == "segment_video" and item["status"] == "pending"]


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
        detail = _detail(client, session_id)
        if predicate(detail):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{session_id}")


def _read_session(root: Path, session_id: str) -> dict:
    path = root / Path(session_id) / SESSION_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _read_events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _new_app(root: Path):
    return create_app(sessions_root=root, runtime_config=RUNTIME)


@pytest.fixture
def recovery_app(tmp_path, monkeypatch):
    """临时会话根 + 确定性模型；规划与写段打桩，写段次数记进 ``writes``。

    ``writes`` 是「某一段有没有被重新写过」的物证：恢复用例不能只看段状态——状态
    对了但提示词被重写一遍，同样是「已通过段被重跑」。
    """
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    writes: list[int] = []

    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: list(PLANS))

    def fake_write(plan, all_plans, state, llm):
        writes.append(int(plan.index))
        return f"[Shot 1] At 00:00.000, segment {plan.index + 1} stub prompt.\n"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)
    # 默认两档桩：读图给一句描述、语义判据判「达到」；被拒用例自己改回 False。
    monkeypatch.setattr(
        frame_auditor, "describe_bridge_frame",
        lambda _path: "尾帧画面：人物已经停在门口，灯光昏黄")
    monkeypatch.setattr(
        segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: True)
    return _new_app(root), root, writes


def _to_first_segment_todo(client, session_id: str) -> dict:
    """阶段 1 → 审查通过 → 阶段 2 → 规划 → 写第 1 段，停在第一段待办。"""
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])
    review = [item for item in _detail(client, session_id)["action_items"]
              if item["status"] == "pending"][0]
    approved = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
        json={"decision": "approve"})
    assert approved.status_code == 202, approved.text
    return _wait_until(
        client, session_id,
        lambda body: body["status"] != "running" and _pending_segment_todos(body))


def _pass_segment(client, session_id: str, number: int, seconds: float,
                  tmp_path: Path) -> str:
    """交一段合格视频并等它通过；返回素材 id。"""
    total = len(PLANS)
    detail = _wait_until(
        client, session_id,
        lambda body: body["status"] == "awaiting_human"
        and [item["segment_index"] for item in _pending_segment_todos(body)] == [number])
    todo = _pending_segment_todos(detail)[0]
    video = write_h3_tier_mp4(tmp_path / f"segment-{number:02d}.mp4", seconds)
    uploaded = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (video.name, video.read_bytes(), "video/mp4")})
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["asset_id"]

    started = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{todo['item_id']}/resolve",
        json={"video_asset_id": asset_id})
    assert started.status_code == 202, started.text
    _wait_job(client, session_id, started.json()["job_id"])

    def _settled(body: dict) -> bool:
        row = body["segments"][number - 1]
        if row["status"] != "passed":
            return False
        if number < total:
            return [item["segment_index"] for item in _pending_segment_todos(body)] == [number + 1]
        return not _pending_segment_todos(body)

    _wait_until(client, session_id, _settled)
    return asset_id


# ---------------------------------------------------------------------------
# 重启：从第一条未通过的段继续，已通过段不重跑不重收
# ---------------------------------------------------------------------------

def test_restart_continues_at_third_segment_without_replaying_passed(
        recovery_app, tmp_path):
    app, root, writes = recovery_app
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        _to_first_segment_todo(client, session_id)
        asset_1 = _pass_segment(client, session_id, 1, 4.0, tmp_path)
        after_first = _read_session(root, session_id)
        assert after_first["segments"][0]["status"] == "passed"
        assert after_first["segments"][0]["video_asset_id"] == asset_1
        asset_2 = _pass_segment(client, session_id, 2, 5.0, tmp_path)
        # 前两段通过后，第 3 段的写词也已落盘，会话停在它的 segment_video 待办上。
        assert writes == [0, 1, 2]
        snapshot = _read_session(root, session_id)
        assert [row["status"] for row in snapshot["segments"]] == [
            "passed", "passed", "waiting_video"]
        assert snapshot["segments"][0]["video_asset_id"] == asset_1
        assert snapshot["segments"][1]["video_asset_id"] == asset_2
        assert snapshot["segments"][2]["prompt_artifact_id"] != ""

    # 关掉进程再起一个：新 app / 新 store 只从盘上读。
    with workbench_client(_new_app(root)) as restarted:
        detail = _detail(restarted, session_id)
        assert detail["status"] == "awaiting_human"
        assert [item["segment_index"] for item in _pending_segment_todos(detail)] == [3]
        assert [row["status"] for row in detail["segments"]] == [
            "passed", "passed", "waiting_video"]
        # 已通过的段没有被重新写词。
        assert writes == [0, 1, 2]

        asset_3 = _pass_segment(restarted, session_id, 3, 5.0, tmp_path)
        final = _wait_until(
            restarted, session_id,
            lambda body: all(row["status"] == "passed" for row in body["segments"]))

    assert [row["video_asset_id"] for row in final["segments"]] == [
        asset_1, asset_2, asset_3]
    # 第 1、2 段不重跑：写词次数没有增长。
    assert writes == [0, 1, 2]
    # 事件类型仍是十种闭集合里的那些（恢复不新增事件类型）。
    from minimax_h3_prompt.workbench_store import EVENT_TYPES

    assert {event["type"] for event in _read_events(root, session_id)} <= set(EVENT_TYPES)


# ---------------------------------------------------------------------------
# 被拒：重启后仍是待交视频，不被当成已通过
# ---------------------------------------------------------------------------

def test_rejected_segment_is_still_waiting_after_restart(
        recovery_app, tmp_path, monkeypatch):
    app, root, _writes = recovery_app
    monkeypatch.setattr(
        segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: False)
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        detail = _to_first_segment_todo(client, session_id)
        todo = _pending_segment_todos(detail)[0]
        video = write_h3_tier_mp4(tmp_path / "rejected.mp4", 4.0)
        uploaded = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": (video.name, video.read_bytes(), "video/mp4")})
        asset_id = uploaded.json()["asset_id"]
        started = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{todo['item_id']}/resolve",
            json={"video_asset_id": asset_id})
        assert started.status_code == 202, started.text
        _wait_job(client, session_id, started.json()["job_id"])
        _wait_until(
            client, session_id,
            lambda body: body["status"] == "awaiting_human"
            and [item["segment_index"] for item in _pending_segment_todos(body)] == [1])
        # 被拒与重建待办在**同一次落盘**里：直接读快照，不经事件重放。
        rejected = _read_session(root, session_id)
        assert rejected["segments"][0]["status"] == "waiting_video"
        assert rejected["segments"][0]["video_asset_id"] == ""
        assert any(item["kind"] == "segment_video" and item["status"] == "pending"
                   for item in rejected["action_items"])

    with workbench_client(_new_app(root)) as restarted:
        detail = _detail(restarted, session_id)
        assert detail["status"] == "awaiting_human"
        assert detail["segments"][0]["status"] == "waiting_video"  # 不是 passed
        assert detail["segments"][0]["video_asset_id"] == ""
        assert [item["segment_index"] for item in _pending_segment_todos(detail)] == [1]
        # 待办仍在：advance 会被闸门挡下，用户必须重交视频。
        blocked = restarted.post(f"/api/v1/sessions/{session_id}/jobs")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "action_item_pending"

        # 换一段合格视频后照常通过。
        monkeypatch.setattr(
            segment_validation, "judge_end_hook", lambda _hook, _desc, _llm: True)
        _pass_segment(restarted, session_id, 1, 4.0, tmp_path)
        detail = _detail(restarted, session_id)
    assert detail["segments"][0]["status"] == "passed"
    assert [item["segment_index"] for item in _pending_segment_todos(detail)] == [2]


# ---------------------------------------------------------------------------
# 执行中被杀：既有 interrupted 对账 + 重试从断点续跑
# ---------------------------------------------------------------------------

def test_killed_validation_resumes_after_interrupted_reconcile(
        recovery_app, tmp_path, monkeypatch):
    app, root, writes = recovery_app
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        _to_first_segment_todo(client, session_id)
        _pass_segment(client, session_id, 1, 4.0, tmp_path)
        assert writes == [0, 1]

        # 让下一次剥尾帧停住：把「校验跑在中间」这个时刻钉成可复现的。
        gate = threading.Event()
        original_extract = frame_auditor.extract_last_frame
        extracts: list[int] = []

        def blocking_extract(video_path, output_path, *, sharpest=True):
            extracts.append(1)
            if len(extracts) == 1:
                assert gate.wait(30), "用例没有放行被杀掉的那次校验"
            return original_extract(video_path, output_path, sharpest=sharpest)

        monkeypatch.setattr(frame_auditor, "extract_last_frame", blocking_extract)

        todo = _pending_segment_todos(_detail(client, session_id))[0]
        video = write_h3_tier_mp4(tmp_path / "segment-02.mp4", 5.0)
        uploaded = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": (video.name, video.read_bytes(), "video/mp4")})
        asset_id = uploaded.json()["asset_id"]
        started = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{todo['item_id']}/resolve",
            json={"video_asset_id": asset_id})
        assert started.status_code == 202, started.text
        _wait_until(
            client, session_id,
            lambda body: bool(extracts) and body["status"] == "running"
            and body["segments"][1]["status"] == "validating")

        # 「被杀」＝盘上留下的就是这个时刻的状态。整份拷走，起一个新 app 读它；
        # 原进程那一份后续怎么写都影响不到副本，正好隔离两个世界。
        dead_root = tmp_path / "dead-process"
        shutil.copytree(root / Path(session_id), dead_root / Path(session_id))
        frozen = _read_session(dead_root, session_id)
        assert frozen["status"] == "running"
        assert frozen["segments"][1]["status"] == "validating"
        assert frozen["segments"][1]["video_asset_id"] == asset_id

        restarted_app = _new_app(dead_root)
        try:
            with workbench_client(restarted_app) as restarted:
                detail = _detail(restarted, session_id)
                # 既有启动对账把它收成 interrupted，不自动续跑。
                assert detail["status"] == "interrupted"
                assert any(job["status"] == "interrupted" for job in detail["jobs"])
                # 段本身仍是「已交、待校验」——重试不需要重交素材。
                assert detail["segments"][1]["status"] == "validating"
                assert detail["segments"][1]["video_asset_id"] == asset_id

                retried = restarted.post(f"/api/v1/sessions/{session_id}/jobs")
                assert retried.status_code == 202, retried.text
                _wait_job(restarted, session_id, retried.json()["job_id"])
                # 收尾与段状态在同一次落盘里写出：任务一结束，段就必须已是 passed。
                detail = _detail(restarted, session_id)
                assert detail["segments"][1]["status"] == "passed"
                assert detail["segments"][1]["video_asset_id"] == asset_id
                # 通过后照常接上第 3 段的写词——等它落成待办，才能断言「没有重写前两段」。
                detail = _wait_until(
                    restarted, session_id,
                    lambda body: [item["segment_index"]
                                  for item in _pending_segment_todos(body)] == [3])

                # 重试只重跑校验：第 1、2 段的提示词各只写了一次，第 3 段是这次
                # 校验通过后正常接上的下一次写词。
                assert writes == [0, 1, 2]
        finally:
            # 放行原进程里那次被杀住的校验，让它自己收尾（只动原来那棵树）。
            gate.set()
            _wait_job(client, session_id, started.json()["job_id"])


# ---------------------------------------------------------------------------
# 被杀在写词中途：重启后只重起那一段的写词 job（spec §10「未写词的段重新起 job」）
# ---------------------------------------------------------------------------

def test_killed_segment_write_restarts_that_write_only(
        recovery_app, tmp_path, monkeypatch):
    app, root, writes = recovery_app
    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        _to_first_segment_todo(client, session_id)
        _pass_segment(client, session_id, 1, 4.0, tmp_path)
        assert writes == [0, 1]

        # 第 2 段通过后会自动写第 3 段；让这次写词停住，制造「写词跑到一半被杀」。
        plain_write = segment_prompts.write_segment_v2
        gate = threading.Event()
        entered: list[int] = []

        def blocking_write(plan, all_plans, state, llm):
            entered.append(int(plan.index))
            if int(plan.index) == 2 and entered.count(2) == 1:
                assert gate.wait(30), "用例没有放行被杀掉的那次写词"
            return plain_write(plan, all_plans, state, llm)

        monkeypatch.setattr(segment_prompts, "write_segment_v2", blocking_write)

        todo = _pending_segment_todos(_detail(client, session_id))[0]
        video = write_h3_tier_mp4(tmp_path / "segment-02.mp4", 5.0)
        uploaded = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": (video.name, video.read_bytes(), "video/mp4")})
        started = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{todo['item_id']}/resolve",
            json={"video_asset_id": uploaded.json()["asset_id"]})
        assert started.status_code == 202, started.text
        _wait_until(
            client, session_id,
            lambda body: body["segments"][1]["status"] == "passed"
            and entered.count(2) == 1 and body["status"] == "running")

        dead_root = tmp_path / "dead-write"
        shutil.copytree(root / Path(session_id), dead_root / Path(session_id))
        frozen = _read_session(dead_root, session_id)
        assert [row["status"] for row in frozen["segments"]] == [
            "passed", "passed", "pending"]
        assert frozen["segments"][2]["prompt_artifact_id"] == ""

        try:
            with workbench_client(_new_app(dead_root)) as restarted:
                detail = _detail(restarted, session_id)
                assert detail["status"] == "interrupted"
                assert detail["segments"][2]["status"] == "pending"

                retried = restarted.post(f"/api/v1/sessions/{session_id}/jobs")
                assert retried.status_code == 202, retried.text
                _wait_job(restarted, session_id, retried.json()["job_id"])
                # 重试必须真的重起写词并落定；只收尾成「无下一步」会把它留成 pending。
                assert _detail(restarted, session_id)["segments"][2]["status"] == "waiting_video"
                detail = _wait_until(
                    restarted, session_id,
                    lambda body: [item["segment_index"]
                                  for item in _pending_segment_todos(body)] == [3])
                assert detail["segments"][2]["status"] == "waiting_video"
                assert detail["segments"][2]["prompt_artifact_id"] != ""
                # 已通过的两段没有被重写：被杀那次停在写词之前，重试只补写第 3 段。
                assert writes == [0, 1, 2]
        finally:
            gate.set()
            _wait_until(
                client, session_id,
                lambda body: [item["segment_index"]
                              for item in _pending_segment_todos(body)] == [3])


# ---------------------------------------------------------------------------
# 不读也不写 CLI 的 shot-ledger.jsonl（D6-AC12）
# ---------------------------------------------------------------------------

def test_workbench_ignores_forged_cli_shot_ledger(
        recovery_app, tmp_path, monkeypatch):
    app, root, _writes = recovery_app
    from minimax_h3_prompt import shot_ledger

    # 直接把 CLI 账的公开读写口打成一调就炸：工作台若真去读/写它，会立刻显形。
    def _boom(*_args, **_kwargs):
        raise AssertionError("工作台不许读写 CLI 的段产出账")

    for name in ("read_shot_ledger", "append_shot_output",
                 "passed_shot_rows", "read_shot_progress"):
        monkeypatch.setattr(shot_ledger, name, _boom)

    with workbench_client(app) as client:
        session_id = client.post(
            "/api/v1/sessions", json=_brief_body()).json()["session_id"]
        # 伪造一份**形状合法、声称三段全部已通过**的账（外加两条 CLI 风格的坏路径）。
        ledger = root / Path(session_id) / "segments" / shot_ledger.SHOT_LEDGER_FILENAME
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            "".join(json.dumps({
                "segment": number, "start_s": (number - 1) * 5,
                "end_s": number * 5, "video": f"D:/somewhere/seg{number}.mp4",
                "sha256": "0" * 64, "at": "2026-01-01T00:00:00+00:00",
            }, ensure_ascii=False) + "\n" for number in (1, 2, 3)),
            encoding="utf-8")
        forged = ledger.read_bytes()

        detail = _to_first_segment_todo(client, session_id)
        # 账上写着「三段都过了」，工作台仍然从第 1 段开始。
        assert [row["status"] for row in detail["segments"]] == [
            "waiting_video", "pending", "pending"]
        _pass_segment(client, session_id, 1, 4.0, tmp_path)

    # 重启后仍以自己的段状态为准（第 2 段待交，而不是「已完成」）。
    with workbench_client(_new_app(root)) as restarted:
        detail = _detail(restarted, session_id)
        assert [row["status"] for row in detail["segments"]] == [
            "passed", "waiting_video", "pending"]
        assert [item["segment_index"]
                for item in _pending_segment_todos(detail)] == [2]

    # 工作台一个字节都没有写过这本账。
    assert ledger.read_bytes() == forged
    # 它写的是自己的段表（同目录并存，互不覆盖）。
    assert (root / Path(session_id) / "segments" / "plan.json").is_file()
