"""D7-4（#257）：注入卡产物与只读视图。

验收面取 HTTP 与产物文件（spec #252 §5.3 / §9 / §11）：素材循环全部定稿之后、阶段 2
开始之前产出整片卡；卡是**只读文本产物**，绝不进模型载荷；段级卡由唯一写入口落盘
并登记（接线在 #258）。

判据尽量来自独立来源：卡的正文读**盘上的文件**，产物清单读详情快照，阶段 2 的出站
载荷用记录型桩 ``pipeline.run_stage2`` 抓下来——卡上的槽位名与素材文件名一个都不许
出现在里面。阶段 1 与素材循环走真实执行路径（与 #256 同一套夹具），只把
``run_stage1`` 换成固定 state、读图与改写打桩。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.workbench_store import ASSETS_DIRNAME, artifact_spec
from workbench_client import workbench_client

from test_api_asset_loop import CAT, LAMP, Harness, _RewriteStub, _png, _stage1_state

CLEAN = json.dumps({"claims": []})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """ref 会话 + 固定阶段 1 + 记录型阶段 2；读图/改写不真发网络请求。"""
    root = tmp_path / "sessions"
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: (
            _stage1_state({
                CAT: "一只橘色短毛猫蹲在木质窗台，尾巴绕住前爪。",
                LAMP: "一盏黄铜台灯立在书桌右角，灯罩是墨绿色。",
            }), None, None))
    stage2_calls: list[dict] = []

    def fake_stage2(stage_state, brief, config, **kwargs):
        # 记录两件事：出站载荷本体，以及「卡在阶段 2 开始时是否已经在盘上」——
        # 后者是「整片卡是进阶段 2 前的最后一件产物」的直接物证。
        stage2_calls.append({
            "state": dict(stage_state),
            "brief": brief,
            "card_on_disk": (
                root / Path(state.session_id) / "injection-card.md").is_file(),
        })
        # 与真实 run_stage2 同一份契约：state 里带 final_prompt（#258 起 ref 阶段 2
        # 之后按它接段循环；只交提示词不回填 state，续跑会不停重跑阶段 2）。
        updated = dict(stage_state)
        updated["final_prompt"] = "最终提示词：雨夜旧信的一段视频正文。"
        return updated, "最终提示词：雨夜旧信的一段视频正文。"

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


def _complete_asset_loop(harness) -> dict:
    """走完整条素材循环（两件逐件交图、校对通过），返回停下来的详情快照。"""
    harness.create()
    harness.register_pictures()
    harness.start_stage1()
    harness.approve_stage1()
    for number in (1, 2):
        body = harness.wait_until(
            lambda snapshot, number=number: any(
                item["kind"] == "asset_image" and item["status"] == "pending"
                and item["asset_index"] == number
                for item in snapshot["action_items"]))
        item = next(entry for entry in body["action_items"]
                    if entry["kind"] == "asset_image" and entry["status"] == "pending")
        # 还没收尾时没有整片卡：它是进阶段 2 前的最后一件产物，不是随时可出的。
        assert not any(entry["name"] == "injection-card.md"
                       for entry in body["artifacts"])
        image = harness.upload(_png(10 + number), f"gen-{number}.png")
        harness.readings.append(CLEAN)
        resp = harness.submit_image(item, image["asset_id"])
        assert resp.status_code == 202, resp.text
    return body


def _artifact(detail: dict, name: str) -> dict:
    return next(item for item in detail["artifacts"] if item["name"] == name)


def _wait_for_stage2_and_card(harness) -> dict:
    """等阶段 2 开跑且整片卡已在产物清单里。

    #258 起 ref 阶段 2 成功后会接段循环，不再直接 `completed`——等 `completed` 会
    永远等不到。本文件要的是「卡在阶段 2 之前落盘」这条时序，与段循环无关。
    """
    return harness.wait_until(
        lambda snapshot: bool(harness.stage2_calls)
        and any(entry["name"] == "injection-card.md"
                for entry in snapshot["artifacts"]))


def test_film_card_is_written_between_the_asset_loop_and_stage2(harness):
    """全部定稿 → 整片卡落成产物 → 阶段 2 才开跑；卡面含台账名、用量与官方上限。"""
    _complete_asset_loop(harness)
    body = _wait_for_stage2_and_card(harness)

    card = _artifact(body, "injection-card.md")
    assert card["kind"] == "injection_card"
    text = harness.artifact_text("injection-card.md")
    assert CAT in text and LAMP in text
    assert "官方单次请求上限：9 图 / 3 视频 / 3 音频 / 总文件 12" in text
    assert "本片用量：图 2 / 视频 0 / 音频 0，共 2 个文件" in text
    assert "ref_image_0" in text and "ref_image_1" in text
    # 正本路径要能在卡上直接照抄找到：渲染时带上了会话目录。
    session_dir = harness.root / Path(harness.session_id)
    assert str(session_dir / "assets") in text

    assert len(harness.stage2_calls) == 1
    assert harness.stage2_calls[0]["card_on_disk"] is True, "卡必须在阶段 2 开始前就在盘上"


def test_film_card_never_reaches_the_stage2_payload(harness):
    """卡是给人照抄的接线清单：槽位名与素材文件名不许出现在阶段 2 的出站载荷里。"""
    _complete_asset_loop(harness)
    _wait_for_stage2_and_card(harness)

    call = harness.stage2_calls[0]
    payload = json.dumps(
        {"state": call["state"], "brief": repr(call["brief"])},
        ensure_ascii=False, default=str)
    assert "ref_image_" not in payload
    assert "ref_video_" not in payload and "ref_audio_" not in payload
    assert "injection-card" not in payload
    for entry in harness.ledger():
        hash16 = str(entry["asset_id"]).rsplit("/", 1)[-1]
        assert hash16 not in payload, "素材文件名只许住卡上，不许进模型载荷"


def test_film_card_still_renders_when_the_ledger_exceeds_one_request(harness):
    """超限照出、不拒绝：卡是库存清单，9/3/3/12 的闸门住在 #255 的两处。

    上传口按官方 9 图硬拒，所以这里直接往会话台账补到 10 件（「台账已有 10 件」这
    条事实），再调卡的写入口——第 10 件也要列出来，并标「无对应槽位」。
    """
    _complete_asset_loop(harness)
    _wait_for_stage2_and_card(harness)
    # #258 起阶段 2 之后还有段循环；改会话文件前先等后台写手停下，别与它对着写。
    harness.wait_until(lambda state: state["status"] != "running")
    directory = harness.root / Path(harness.session_id)
    extra = directory / ASSETS_DIRNAME / "0123456789abcdef.png"
    extra.write_bytes(_png(55))
    digest = hashlib.sha256(extra.read_bytes()).hexdigest()
    snapshot = harness.on_disk()
    entries = [dict(entry) for entry in snapshot["asset_ledger"]]
    for number in range(len(entries) + 1, 11):
        entries.append({
            "name": f"素材{number}", "kind": "picture", "number": number,
            "asset_id": f"{harness.session_id}/0123456789abcdef",
            "sha256": digest, "round": 0, "status": "passed"})
    snapshot["asset_ledger"] = entries
    (directory / SESSION_FILENAME).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    harness.client.app.state.job_runner._write_film_injection_card(
        harness.session_id, "job-cap")
    text = harness.artifact_text("injection-card.md")
    assert "本片用量：图 10 / 视频 0 / 音频 0，共 10 个文件" in text
    assert "素材10" in text, "超限素材也要列出来（不静默丢）"
    assert "无对应槽位" in text and "槽位越界" in text


# ---------------------------------------------------------------------------
# 段级卡（能力在本票，接线在 #258）
# ---------------------------------------------------------------------------

class _StubRefJob:
    """段级卡写入口只需要 ``segment_asset_card``；#258 传的是真的 RefSegmentJob。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.positions: list[int] = []

    def segment_asset_card(self, position: int) -> str:
        self.positions.append(position)
        return self.text


def test_segment_card_writer_persists_and_registers_the_exact_text(harness):
    """写入口把**同一份**文本落盘、登记成 ``segment_injection_card``，content 可读。"""
    harness.create()
    text = "# 第 1 段接线卡\n\n| ref_image_0 | 橘猫 |\n"
    job = _StubRefJob(text)
    info = harness.client.app.state.job_runner._write_segment_injection_card(
        harness.session_id, 1, job, job_id="job-seg-card")

    assert job.positions == [1], "文本必须取自 segment_asset_card(position)"
    assert info is not None
    assert info.name == "segments/injection-card-01.md"
    assert info.kind == "segment_injection_card"
    path = (harness.root / Path(harness.session_id)
            / "segments" / "injection-card-01.md")
    assert path.read_text(encoding="utf-8") == text
    detail = harness.detail()
    assert any(item["name"] == "segments/injection-card-01.md"
               and item["kind"] == "segment_injection_card"
               for item in detail["artifacts"])

    resp = harness.client.get(
        f"/api/v1/artifacts/{harness.session_id}"
        "/segments/injection-card-01.md/content")
    assert resp.status_code == 200, resp.text
    assert resp.text == text


def test_segment_without_a_card_has_no_artifact(harness):
    """段循环没跑到时该段没有卡——不是空卡（空文本不落盘、不登记）。"""
    harness.create()
    runner = harness.client.app.state.job_runner
    assert runner._write_segment_injection_card(
        harness.session_id, 1, _StubRefJob(""), job_id="job-empty") is None
    assert not (harness.root / Path(harness.session_id)
                / "segments" / "injection-card-01.md").exists()
    assert [item for item in harness.detail()["artifacts"]
            if item["kind"] == "segment_injection_card"] == []


def test_both_cards_are_registered_by_the_single_table():
    """两种卡都走 ``artifact_spec``：文本形态；编号两位、目录不对就不算产物。"""
    card = artifact_spec("injection-card.md")
    assert card is not None and card.kind == "injection_card"
    assert card.media_type is None
    segment = artifact_spec("segments/injection-card-01.md")
    assert segment is not None and segment.kind == "segment_injection_card"
    assert segment.media_type is None
    assert artifact_spec("segments/injection-card-1.md") is None
    assert artifact_spec("segments/injection-card-001.md") is None
    assert artifact_spec("assets/injection-card-01.md") is None

