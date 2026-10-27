"""首尾帧链收尾自动接出成片（issue #96）：全段通过 → ``final.mp4``。

钉住的是**外部可见**的事实：成片在不在、帧数对不对、屏幕上有没有落点与段数、
失败时会话是否仍停在「分段进行中」（能续拼）。拼接判据本身（缺段/SHA/覆盖）
住在共用件里，已由 ``test_final_film.py`` 钉住；这里钉的是**向导有没有把它接上**——
「规则在、接线不在」是本仓反复栽过的形态（第 1 次是阈值未贯通，第 9 次是
``stream_usage`` 没开导致成本表看着还在、数是空的）。

两条产出路径（规划式与机械回退式）各一条端到端：全程驱动 ``run_wizard``，
只打桩 LLM／写段／读图与闸门，**ffmpeg 真拼**几段真视频。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import (
    model_factory,
    segment_planner,
    segment_prompts,
    session_store,
    shot_ledger,
)
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.session_store import load_session, save_session
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.ui import wizard

FIXTURE = Path(__file__).parent / "fixtures" / "official_shot01_i2va.md"

# 两镜头的整片提示词：机械拆分得到 0-4s / 4-8s 两段，与 ``_plans(2)`` 的边界一致。
# 两个路径必须看到**同一套段序**，否则「两条路径各测一次」测的是两件事。
TWO_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:04.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _brief(duration: float = 8.0) -> Brief:
    return Brief(mode="base", variant="I2VA", duration=duration, style="写实",
                 plot="深夜便利店一只橘猫推门跳上柜台蹭店员的手")


def _start_session(tmp_path, *, duration: float = 8.0):
    """造一个「分段陪跑跑到一半」的会话，让 ``run_wizard`` 直接落到续接入口。

    这样不必重放阶段 1/2：那两步与本票无关，而它们要打的桩（澄清、素材、读图）
    会把用例拖成另一件事的回归。
    """
    gen = tmp_path / "sessions" / "某主题" / "GEN001"
    gen.mkdir(parents=True)
    save_session(gen, _brief(duration), {"shot_table": "[Shot 1] 深夜空店内……"},
                 status=session_store.STATUS_SEGMENTED_RUNNING)
    (gen / "video-prompt.md").write_text(TWO_SHOT_PROMPT, encoding="utf-8")
    return gen, load_session(gen)


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(sessions_root=str(tmp_path / "sessions"),
                           vision_model=SimpleNamespace(model="stub-vision"))


def _plans(total: int = 2) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=i, start_s=i * 4, end_s=(i + 1) * 4, shots_in_segment=(i + 1,),
                    summary=f"第 {i + 1} 段中文摘要", end_hook=f"第 {i + 1} 段末态")
        for i in range(total)
    ]


def _write_plans(directory: Path, total: int = 2) -> None:
    (directory / "segments").mkdir(parents=True, exist_ok=True)
    (directory / "segments" / "plan.json").write_text(
        json.dumps([plan.to_dict() for plan in _plans(total)], ensure_ascii=False, indent=2),
        encoding="utf-8")


def _stub_flow(monkeypatch, tmp_path) -> None:
    """打桩交互、闸门与读图；不动输入序列本身（新题只在 ``_prompt`` 里按题意作答）。

    ``_capture_bridge_frame`` 被拦住＝剥帧与读图都不发生（那是另一张票的地盘），
    但**用户交回的路径**照样经 ``on_pass`` 落账、照样进入拼接——用例要测的正是
    「账上的视频真的被接起来」。
    """
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    monkeypatch.setattr(wizard, "_capture_bridge_frame",
                        lambda *a, **k: ([], "桥接帧画面", frame))
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())


def _answers(monkeypatch, sequence: list[str]) -> list[str]:
    iterator = iter(sequence)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(iterator, "")

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _video(tmp_video, name: str, *, frames: int = 96):
    """4s / 24fps 的真视频：与 ``_plans`` 的段时长一致，成片时长核对不至于响得没道理。"""
    return tmp_video(name, frames=frames, fps=24, width=128, height=72)


# ---------------------------------------------------------------------------
# 1. 规划式（v2）端到端：全段通过 → final.mp4
# ---------------------------------------------------------------------------

def test_v2_path_concats_the_film_after_every_segment_passes(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, _session = _start_session(tmp_path)
    first = _video(tmp_video, "seg1.mp4")
    last = _video(tmp_video, "seg2.mp4")
    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans(2))
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    # 续接菜单选 1 → 段 1 生成好 → 交段 1 视频 → 段 2 生成好 → 交末段视频
    _answers(monkeypatch, ["", "", str(first), "", str(last)])

    wizard.run_wizard(_cfg(tmp_path))

    final = gen / "final.mp4"
    out = capsys.readouterr().out
    assert final.is_file(), out
    assert video_meta(final)["frames"] == 192, "两段各 96 帧，stream-copy 直接接起来"
    assert str(final) in out, "落点必须当面给出"
    assert "2 段" in out, "段数必须当面给出"
    assert load_session(gen).status == session_store.STATUS_COMPLETED


# ---------------------------------------------------------------------------
# 2. 机械回退式端到端：段表在收尾补写，成片同样接得出来
# ---------------------------------------------------------------------------

def test_fallback_path_writes_its_segment_table_and_concats(
        tmp_path, monkeypatch, tmp_video, capsys):
    """回退路径没有 plan 层，收尾必须补写 ``segments/plan.json``（段序真源）。

    只在**全部段通过之后**写：中途写会让中断后的续跑在 ``cached and done`` 分支
    误走规划式重写——机械段边界与规划器不是一套，按它续写等于换了一套段。
    """
    gen, _session = _start_session(tmp_path)
    first = _video(tmp_video, "seg1.mp4")
    last = _video(tmp_video, "seg2.mp4")
    _stub_flow(monkeypatch, tmp_path)

    def broken_planner(*_a, **_k):
        raise RuntimeError("规划挂了，按 [Shot N] 机械拆分")

    monkeypatch.setattr(segment_planner, "plan_segments", broken_planner)
    # 回退写段也打桩：本用例测收尾，不测声音重写；返回 None＝用机械拆分的原文。
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: None)
    _answers(monkeypatch, ["", "", str(first), "", str(last)])

    wizard.run_wizard(_cfg(tmp_path))

    final = gen / "final.mp4"
    out = capsys.readouterr().out
    assert final.is_file(), out
    assert video_meta(final)["frames"] == 192
    plan = json.loads((gen / "segments" / "plan.json").read_text(encoding="utf-8"))
    assert [(item["start_s"], item["end_s"]) for item in plan] == [(0, 4), (4, 8)]
    assert [item["shots_in_segment"] for item in plan] == [[1], [2]]
    assert all(item["summary"] == "" and item["end_hook"] == "" for item in plan), \
        "机械段表没有 plan 层的概述与末态声明，留空而不是编一句"
    assert load_session(gen).status == session_store.STATUS_COMPLETED


def test_fallback_writes_no_table_until_every_segment_has_passed(
        tmp_path, monkeypatch, tmp_video):
    """机械段表只在**全部段通过之后**补写。

    中途写会让中断后的续跑在 ``cached and done`` 分支误走规划式重写：机械段边界与
    规划器不是一套，按它续写等于换了一套段——而且**不报错**。
    """
    gen, _session = _start_session(tmp_path)
    first = _video(tmp_video, "seg1.mp4")
    _stub_flow(monkeypatch, tmp_path)

    def broken_planner(*_a, **_k):
        raise RuntimeError("规划挂了，按 [Shot N] 机械拆分")

    monkeypatch.setattr(segment_planner, "plan_segments", broken_planner)
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: None)
    # 末段视频回车＝放弃继续（`_acquire_last_video` 的停下口）
    _answers(monkeypatch, ["", "", str(first), "", ""])

    wizard.run_wizard(_cfg(tmp_path))

    assert not (gen / "segments" / "plan.json").exists(), \
        "没跑完就写段表，会把中断后的续跑引到规划式重写"
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


# ---------------------------------------------------------------------------
# 3. 收尾拒绝面：不产出成片，也不把会话标成完成（留着能续拼）
# ---------------------------------------------------------------------------

def _finish_only(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_run_segmented_flow", lambda *a, **k: True)


def test_finish_refuses_and_stays_running_when_a_segment_is_missing(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, session = _start_session(tmp_path)
    _write_plans(gen, 2)
    shot_ledger.append_shot_output(
        gen, segment=1, start_s=0, end_s=4,
        video_path=_video(tmp_video, "only1.mp4"))
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists(), "缺段的成片是伪产物，比没有更坏"
    assert "第 2 段" in out, "必须点名缺哪一段"
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING, \
        "拼接没成不得标完成——标了下次重跑会另开新 GEN，已通过的段全白做"


def test_finish_refuses_when_a_recorded_video_changed(tmp_path, monkeypatch,
                                                      tmp_video, capsys):
    """同名换版：账上记的 SHA256 就是「通过验收的那一份」的身份。"""
    gen, session = _start_session(tmp_path)
    _write_plans(gen, 2)
    first = _video(tmp_video, "seg1.mp4")
    second = _video(tmp_video, "seg2.mp4")
    shot_ledger.append_shot_output(gen, segment=1, start_s=0, end_s=4, video_path=first)
    shot_ledger.append_shot_output(gen, segment=2, start_s=4, end_s=8, video_path=second)
    second.write_bytes(b"a different cut of segment 2")
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists()
    assert "第 2 段" in out and "SHA256" in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


def test_finish_refuses_a_ledger_that_skips_a_segment(tmp_path, monkeypatch,
                                                      tmp_video, capsys):
    """账上跳号＝这不是同一套分割的进度，不能拿来拼成片。"""
    gen, session = _start_session(tmp_path, duration=12.0)
    _write_plans(gen, 3)
    for number in (1, 3):
        shot_ledger.append_shot_output(
            gen, segment=number, start_s=(number - 1) * 4, end_s=number * 4,
            video_path=_video(tmp_video, f"seg{number}.mp4"))
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists()
    assert "跳号" in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


def test_finish_refuses_when_the_table_stops_short_of_the_total(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, session = _start_session(tmp_path, duration=12.0)
    _write_plans(gen, 2)  # 只覆盖到 8s，欠起步总时长 12s
    first = _video(tmp_video, "seg1.mp4")
    second = _video(tmp_video, "seg2.mp4")
    shot_ledger.append_shot_output(gen, segment=1, start_s=0, end_s=4, video_path=first)
    shot_ledger.append_shot_output(gen, segment=2, start_s=4, end_s=8, video_path=second)
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists()
    assert "12s" in out and "段表" in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


# ---------------------------------------------------------------------------
# 4. 旧会话：拼接不可用，但照旧标完成（那不是失败）
# ---------------------------------------------------------------------------

def test_legacy_session_says_the_film_is_unavailable_and_still_completes(
        tmp_path, monkeypatch, capsys):
    gen, session = _start_session(tmp_path)
    (gen / "segments").mkdir(exist_ok=True)
    (gen / "segments" / "progress.json").write_text(
        json.dumps({"done": 2, "total": 2}), encoding="utf-8")
    # 旧会话即便碰巧有一份 plan.json，也不许拼：账上没有段视频路径。
    _write_plans(gen, 2)
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert "旧会话没记段视频，拼接不可用" in out
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_COMPLETED, \
        "旧会话拼不出成片是已知边界，不是失败——停在 running 会让它永远续不出来"


# ---------------------------------------------------------------------------
# 5. ≤10 秒不分段：没有可拼的东西
# ---------------------------------------------------------------------------

def test_short_video_never_produces_a_final_film(tmp_path, monkeypatch):
    """短视频（≤10s）不走长片收尾，**不**产出 final.mp4。

    ``_phase2_collect_and_finish`` 是唯一的路由点（``brief.duration > 10``），
    成片、段表、账在这里都不该出现——短视频本来就是一次生成的结果。
    """
    brief = _brief(6.0)
    gen = tmp_path / "GEN001"
    save_session(gen, brief, {"shot_table": "[Shot 1] 深夜空店内……"},
                 status=session_store.STATUS_AWAITING_FRAMES)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(wizard, "run_stage2", lambda *a, **k: ({}, "整条提示词"))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *a, **k: None)

    assert wizard._phase2_collect_and_finish(
        SimpleNamespace(vision_model=SimpleNamespace(model="stub-vision")),
        session_store.load_session(gen)) == 0

    assert not (gen / "final.mp4").exists()
    assert not (gen / "segments" / "shot-ledger.jsonl").exists()
    assert load_session(gen).status == session_store.STATUS_COMPLETED
