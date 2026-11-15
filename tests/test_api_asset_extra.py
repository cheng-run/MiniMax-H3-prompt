"""D7 修订 5/6（#284）：素材循环之后的视频/音频补交待办（``asset_extra``）。

覆盖票面 AC 的四条外部行为：

- 循环走完 / 零资产时挂补交待办，未解进不了阶段 2；
- 「没有，就这样」能点下去、放行，而且不再重复问；
- 补交视频/音频按用户起的语义名登记，编号走独立的 kind 号段（不占 Picture）；
- 落定后容量按当时台账现算并点名超限档，此后不再接受增补。

判据尽量读**会话文件**与事件流（``events.jsonl``），不用接口自证；阶段 1 走真实执行
路径，只把 ``graph.pipeline.run_stage1`` 换成固定 state 的桩。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.tools import reference_auditor
from workbench_client import workbench_client

from test_api_asset_loop import (
    CAT,
    LAMP,
    Harness,
    _RewriteStub,
    _mp4,
    _png,
    _stage1_state,
)

# RIFF/WAVE 头：存储层按魔数认音频（WAV 在 8..12 字节上写 WAVE），不解析内容。
WAV_MAGIC = b"RIFF" + (8192).to_bytes(4, "little") + b"WAVE"


def _wav(seed: int = 0) -> bytes:
    return WAV_MAGIC + bytes([seed % 256]) * 4096


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """ref 会话 + 固定两件资产 + 记录型阶段 2；读图/改写不真发网络请求。"""
    root = tmp_path / "sessions"
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: (
            _stage1_state({
                CAT: "一只橘色短毛猫蹲在木质窗台，尾巴绕住前爪。",
                LAMP: "一盏黄铜台灯立在书桌右角，灯罩是墨绿色。",
            }), None, None))

    stage2_calls: list[object] = []

    def fake_stage2(stage_state, brief, config, **kwargs):
        stage2_calls.append(brief)
        # 与真实 run_stage2 同一份契约：回填 final_prompt，否则续跑会认成没跑过。
        updated = dict(stage_state)
        updated["final_prompt"] = "固定视频提示词"
        return updated, "固定视频提示词"

    monkeypatch.setattr(pipeline, "run_stage2", fake_stage2)
    rewrite = _RewriteStub()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: rewrite)

    def fake_describe(path, instruction=None):
        state.read_calls.append((str(path), str(instruction or "")))
        if not state.readings:
            raise AssertionError("用例没有给这条读图准备结论")
        return state.readings.pop(0)

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    with workbench_client(create_app(sessions_root=root)) as client:
        state = Harness(client, root, rewrite, [])
        state.stage2_calls = stage2_calls
        yield state


def _zero_asset_stage1(monkeypatch) -> None:
    """阶段 1 一件资产都没产（纯风景片）：派生 0 行、素材循环空转、直接到补交口。"""
    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: ({
            "script": "分场剧本：雨落在空码头上。",
            "shot_table": "[Shot 1] At 00:00.000, an empty pier in the rain.",
            "character_image_prompts": [],
        }, None, None))


def _to_extras_gate(harness) -> dict:
    """零资产 ref 会话走到补交口，返回当时快照。"""
    harness.create()
    harness.start_stage1()
    harness.approve_stage1()
    return harness.wait_until(
        lambda snapshot: any(item["kind"] == "asset_extra"
                             and item["status"] == "pending"
                             for item in snapshot["action_items"]))


def _extra_item(snapshot: dict) -> dict:
    return next(item for item in snapshot["action_items"]
                if item["kind"] == "asset_extra" and item["status"] == "pending")


def _resolve_extra(harness, item: dict, body: dict):
    return harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/action-items/"
        f"{item['item_id']}/resolve", json=body)


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status and body["code"] == code
    return body


def _events(harness) -> list[dict]:
    path = harness.root / Path(harness.session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 待办的挂出与阻塞（票面 AC 1）
# ---------------------------------------------------------------------------

def test_zero_asset_ref_gets_the_extras_gate_and_stage2_waits(
        harness, monkeypatch):
    """派生 0 行也走补交口（#279「零资产合法」）：待办挂出、阶段 2 一次都没跑。"""
    _zero_asset_stage1(monkeypatch)
    body = _to_extras_gate(harness)

    assert body["status"] == "awaiting_human"
    assert harness.ledger() == [], "零资产的台账就是空的，不该为了补交口落占位行"
    assert harness.stage2_calls == []

    # 未解时推进被挡：会话停在人工待办上（409），阶段 2 仍没开始。
    resp = harness.client.post(f"/api/v1/sessions/{harness.session_id}/jobs")
    _assert_problem(resp, 409, "action_item_pending")
    assert harness.stage2_calls == []


def test_the_extras_gate_is_its_own_step_between_loop_and_stage2(
        harness, monkeypatch):
    """补交档单独占一步（#279 §步骤机）：循环走完时 ``_resume_step`` 落在 ``asset_extra``。

    复用 ``asset_loop`` 会让进度条在补交口仍写「素材循环」——循环其实已经走完，
    那段文字会把「逐件交图」和「要不要补视频/音频」两件事混成一件。
    """
    from minimax_h3_prompt import step_machine

    _zero_asset_stage1(monkeypatch)
    body = _to_extras_gate(harness)
    runner = harness.client.app.state.job_runner

    assert runner._resume_step(harness.session_id) == step_machine.STEP_ASSET_EXTRA
    steps = step_machine.STEPS
    assert (steps.index(step_machine.STEP_ASSET_LOOP)
            < steps.index(step_machine.STEP_ASSET_EXTRA)
            < steps.index(step_machine.STEP_STAGE2))
    # 落定之后不再回到这一档（接着走哪一步由既有用例覆盖，这里只管补交档的进出）。
    assert _resolve_extra(harness, _extra_item(body),
                          {"decision": "no_extras"}).status_code == 202
    assert runner._resume_step(harness.session_id) != step_machine.STEP_ASSET_EXTRA


def test_extras_gate_is_announced_by_an_action_item_created_event(
        harness, monkeypatch):
    """挂待办走既有 ``action_item.created``（十种事件闭集不扩）。"""
    _zero_asset_stage1(monkeypatch)
    _to_extras_gate(harness)

    created = [event for event in _events(harness)
               if event["type"] == "action_item.created"
               and event["payload"]["kind"] == "asset_extra"]
    assert len(created) == 1
    assert set(event["type"] for event in _events(harness)) <= {
        "session.created", "job.started", "job.progress", "job.succeeded",
        "job.failed", "job.cancelled", "job.interrupted",
        "action_item.created", "action_item.resolved", "artifact.created"}


# ---------------------------------------------------------------------------
# 「没有，就这样」（票面 AC 2）
# ---------------------------------------------------------------------------

def test_no_extras_passes_and_is_never_asked_again(harness, monkeypatch):
    """选「没有」即放行到阶段 2，而且之后不再挂第二张补交待办。"""
    _zero_asset_stage1(monkeypatch)
    body = _to_extras_gate(harness)
    item = _extra_item(body)

    resp = _resolve_extra(harness, item, {"decision": "no_extras"})
    assert resp.status_code == 202, resp.text
    body = harness.wait_until(
        lambda snapshot: bool(harness.stage2_calls)
        and any(entry["name"] == "video-prompt.md"
                for entry in snapshot["artifacts"]))
    assert body["status"] != "awaiting_human"

    on_disk = harness.on_disk()
    resolved = [entry for entry in on_disk["action_items"]
                if entry["kind"] == "asset_extra"]
    assert [(entry["status"], entry["decision"]) for entry in resolved] == [
        ("resolved", "no_extras")]
    created = [event for event in _events(harness)
               if event["type"] == "action_item.created"
               and event["payload"]["kind"] == "asset_extra"]
    assert len(created) == 1, "「没有」是落定的答案，不该再被问一遍"


def test_restart_keeps_the_gate_and_rehangs_it_after_a_crash_like_snapshot(
        harness, monkeypatch):
    """补交待办是持久事实：换一个进程照样读得到；快照丢了也能按台账重建。"""
    _zero_asset_stage1(monkeypatch)
    _to_extras_gate(harness)

    # 真·重启：同一数据根上的新 app 实例仍然看到那张待办。
    with workbench_client(create_app(sessions_root=harness.root)) as restarted:
        detail = restarted.get(
            f"/api/v1/sessions/{harness.session_id}").json()
        assert [entry["kind"] for entry in detail["action_items"]
                if entry["status"] == "pending"] == ["asset_extra"]

    # 模拟「循环已走完、待办还没挂出来就崩了」：把快照改成 interrupted 且抽掉待办。
    # 下一次推进必须回到补交口，而不是跳过它直接跑阶段 2。
    path = harness.root / Path(harness.session_id) / SESSION_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "interrupted"
    data["action_items"] = [entry for entry in data["action_items"]
                            if entry["kind"] != "asset_extra"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    resp = harness.client.post(f"/api/v1/sessions/{harness.session_id}/jobs")
    assert resp.status_code == 202, resp.text
    body = harness.wait_until(
        lambda snapshot: any(item["kind"] == "asset_extra"
                             and item["status"] == "pending"
                             for item in snapshot["action_items"]))
    assert harness.stage2_calls == [], "重建待办时越过了补交口"
    assert _extra_item(body)["decision"] is None


# ---------------------------------------------------------------------------
# 补交视频/音频：用户起名、独立号段（票面 AC 3）
# ---------------------------------------------------------------------------

def test_extras_are_named_by_the_user_and_numbered_per_kind(harness, monkeypatch):
    """视频/音频各走自己的号段（1、2…），不占 Picture 号段，语义名由用户给。"""
    _zero_asset_stage1(monkeypatch)
    _to_extras_gate(harness)

    video = harness.upload(_mp4(3), "camera.mp4", name="运镜参考")
    audio = harness.upload(_wav(4), "voice.wav", name="红裙女孩的音色")

    entries = harness.ledger()
    assert [(entry["name"], entry["kind"], entry["number"], entry["source"])
            for entry in entries] == [
        ("运镜参考", "video", 1, "uploaded"),
        ("红裙女孩的音色", "audio", 1, "uploaded"),
    ]
    # 回显走的还是同一份台账，前端靠它列出刚交的两件。
    refs = harness.detail()["brief"]["refs"]
    assert [(ref["kind"], ref["number"], ref["asset_id"])
            for ref in refs] == [
        ("video", 1, video["asset_id"]),
        ("audio", 1, audio["asset_id"]),
    ]

    resp = _resolve_extra(harness, _extra_item(harness.detail()),
                          {"decision": "submit_extras",
                           "asset_id": audio["asset_id"]})
    assert resp.status_code == 202, resp.text
    harness.wait_until(lambda snapshot: bool(harness.stage2_calls))
    assert [(entry["kind"], entry["number"]) for entry in harness.ledger()] == [
        ("video", 1), ("audio", 1)]


def test_pictures_cannot_ride_the_extras_gate_and_nothing_is_added_after_it(
        harness, monkeypatch):
    """补交口只收视频/音频；落定之后连视频/音频也不再接受增补（快照已定）。"""
    _zero_asset_stage1(monkeypatch)
    _to_extras_gate(harness)

    # Picture 的件数与名字由阶段 1 定：补交口开着一个口子但不包括它。
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/assets",
        files={"file": ("extra.png", _png(7), "application/octet-stream")},
        data={"name": "临时角色"})
    _assert_problem(resp, 409, "asset_locked")
    assert harness.ledger() == []

    video = harness.upload(_mp4(5), "camera.mp4", name="运镜参考")
    resp = _resolve_extra(harness, _extra_item(harness.detail()),
                          {"decision": "submit_extras",
                           "asset_id": video["asset_id"]})
    assert resp.status_code == 202, resp.text
    harness.wait_until(lambda snapshot: bool(harness.stage2_calls))

    # 落定之后：注入卡与段内子集都以这份台账快照为准，再交一件就会对不上。
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/assets",
        files={"file": ("late.mp4", _mp4(6), "application/octet-stream")},
        data={"name": "迟到的视频"})
    _assert_problem(resp, 409, "asset_locked")
    assert [entry["name"] for entry in harness.ledger()] == ["运镜参考"]


# ---------------------------------------------------------------------------
# 容量（票面 AC 4）
# ---------------------------------------------------------------------------

def test_extras_capacity_rejects_the_fourth_video_and_names_the_tier(
        harness, monkeypatch):
    """视频超过 3 件当场 422，点名「视频 4 个 > 3 个」并列出超出的那几件。"""
    _zero_asset_stage1(monkeypatch)
    _to_extras_gate(harness)

    for seed in (1, 2, 3):
        harness.upload(_mp4(seed), f"camera-{seed}.mp4", name=f"运镜参考 {seed}")
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/assets",
        files={"file": ("camera-4.mp4", _mp4(4), "application/octet-stream")},
        data={"name": "运镜参考 4"})
    problem = _assert_problem(resp, 422, "ref_capacity_exceeded")
    assert "视频 4 个 > 3 个" in problem["detail"]
    assert "运镜参考 4" in problem["detail"]
    assert len(harness.ledger()) == 3, "超限的那一件一个字节都不该登记"


def test_capacity_is_recomputed_when_the_gate_settles(harness, monkeypatch):
    """落定那一刻再按当时台账现算一次容量——注入卡与段内子集用的就是这份快照。

    正常上传路径每次登记都判过，所以超限只能在落定时由台账拼出来；这里把判据换成
    「只在补交口落定时炸」，验证那一道闸真的被调用（否则 AC 的「现算」只是上传时
    那一次，快照合法性没有独立保证）。
    """
    from minimax_h3_prompt import ref_capacity, workbench_store

    calls: list[str] = []

    def only_at_the_gate(items, *, where=""):
        calls.append(where)
        if where == "补交素材":
            raise ref_capacity.ReferenceCapacityError(
                "补交素材：参考素材超出官方单次请求上限（9 图 / 3 视频）：\n"
                "- 视频 4 个 > 3 个：<Video 4>「迟到的视频」")

    # 存储层按名导入过这个函数（上传闸与补交闸共用），要打在它的绑定处。
    monkeypatch.setattr(workbench_store, "check_reference_capacity",
                        only_at_the_gate)
    _zero_asset_stage1(monkeypatch)
    _to_extras_gate(harness)
    harness.upload(_mp4(1), "camera.mp4", name="运镜参考")

    resp = _resolve_extra(harness, _extra_item(harness.detail()),
                          {"decision": "submit_extras",
                           "asset_id": harness.ledger()[0]["asset_id"]})
    problem = _assert_problem(resp, 422, "ref_capacity_exceeded")
    assert "视频 4 个 > 3 个" in problem["detail"]
    assert "补交素材" in calls
    # 决议没有落地：待办仍 pending，用户还能换成「没有」或精简素材。
    assert _extra_item(harness.detail())["item_id"]


# ---------------------------------------------------------------------------
# 载荷错配一律 422（票面 AC 3 的接线面）
# ---------------------------------------------------------------------------

def test_extras_todo_rejects_every_other_payload(harness, monkeypatch):
    """缺 asset_id、拿 Picture 顶替、两种答案同时给、未知 decision 都是 422。"""
    _zero_asset_stage1(monkeypatch)
    body = _to_extras_gate(harness)
    item = _extra_item(body)

    _assert_problem(_resolve_extra(harness, item, {"decision": "submit_extras"}),
                    422, "asset_extra_invalid")
    _assert_problem(
        _resolve_extra(harness, item, {"decision": "no_extras",
                                       "asset_id": "x/y"}),
        422, "asset_image_invalid")
    _assert_problem(_resolve_extra(harness, item, {"decision": "approve"}),
                    422, "asset_extra_invalid")

    # 拿一张图片冒充补交素材：它不在台账里，也不是视频/音频。
    picture = harness.upload(_png(8), "still.png")
    _assert_problem(
        _resolve_extra(harness, item,
                       {"decision": "submit_extras", "asset_id": picture["asset_id"]}),
        422, "asset_extra_invalid")
    assert _extra_item(harness.detail())["item_id"] == item["item_id"]
