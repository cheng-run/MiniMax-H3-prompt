"""首尾帧链的落账点位（issue #94）：两条产出路径各在哪儿写段产出账。

流程侧要钉住的是**外部可见**的事实：账里有哪几行、视频是哪一份、续跑从第几段起、
被拦下的轮次有没有落行、旧会话屏幕上说没说「拼接不可用」。账面本身（字段、同段取
末行、错形/跳号响错）在 ``test_shot_ledger.py``。

两条路径的时序**相反**，所以落账点也不同（这正是本票最容易接错的地方）：
- v2 规划式：本段末尾调闸门，通过即落账；
- 机械回退式：上一段的视频在**下一轮开头**才验，末段在 ``_verify_last_frame``。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts, session_store
from minimax_h3_prompt import shot_ledger
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.ui import wizard

FIXTURE = Path(__file__).parent / "fixtures" / "official_shot01_i2va.md"


def _brief(duration: float = 12.0) -> Brief:
    return Brief(mode="base", variant="I2VA", duration=duration, style="写实",
                 plot="深夜便利店一只橘猫推门跳上柜台蹭店员的手")


def _session(tmp_path, *, duration: float = 12.0) -> session_store.SessionState:
    gen = tmp_path / "GEN001"
    gen.mkdir(parents=True, exist_ok=True)
    return session_store.SessionState(
        directory=gen, brief=_brief(duration),
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_SEGMENTED_RUNNING)


# 四镜头的整片提示词：回退路径按它拆成 4 段，用来造「账上 {1,3} 跳号」的形态。
FOUR_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:04.000, the cat walks to the counter.\n"
    "[Shot 3] At 00:08.000, the cat jumps onto the counter.\n"
    "[Shot 4] At 00:12.000, the woman pets the cat.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _plans(total: int = 2) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=i, start_s=i * 4, end_s=(i + 1) * 4, shots_in_segment=(i + 1,),
                    summary=f"第 {i + 1} 段中文摘要", end_hook=f"第 {i + 1} 段末态")
        for i in range(total)
    ]


def _video(directory: Path, name: str, payload: bytes | None = None) -> Path:
    path = directory / name
    path.write_bytes(payload if payload is not None else f"bytes-of-{name}".encode())
    return path


def _answers(monkeypatch, sequence: list[str]):
    iterator = iter(sequence)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(iterator, ""))


def _confirm_series(monkeypatch, sequence: list[bool]):
    iterator = iter(sequence)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: next(iterator, True))


def _patch_capture(monkeypatch, tmp_path, *, description: str = "桥接帧画面"):
    """拦住真剥帧：本票测的是落账点位，剥帧/读图另有专门用例。"""
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    monkeypatch.setattr(wizard, "_capture_bridge_frame",
                        lambda *a, **k: ([], description, frame))
    # 末段无尾帧时只走可证层：这里直接放行，专测落账。
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())


def _rows(directory: Path) -> list[dict]:
    return shot_ledger.read_shot_ledger(directory)


# --- v2 规划式 ---------------------------------------------------------------

def test_v2_writes_one_row_per_passed_segment(tmp_path, monkeypatch):
    """一段通过可证层与语义层之后，账里恰好追加一行，字段齐全。"""
    session = _session(tmp_path)
    seg1 = _video(session.directory, "seg1.mp4", b"segment-one")
    seg2 = _video(session.directory, "seg2.mp4", b"segment-two")
    _patch_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _confirm_series(monkeypatch, [True, True])
    # 段1 生成好 → 交段1 视频 → 段2 生成好 → 交末段视频（本片无尾帧，只做可证层）
    _answers(monkeypatch, ["", str(seg1), "", str(seg2)])

    finished = wizard._run_segmented_flow_v2(session, _plans(2),
                                             session.stage_state, SimpleNamespace())

    assert finished is True
    rows = _rows(session.directory)
    assert [row["segment"] for row in rows] == [1, 2]
    assert rows[0]["video"] == str(seg1.resolve())
    assert rows[0]["start_s"] == 0 and rows[0]["end_s"] == 4
    assert rows[0]["sha256"] == hashlib.sha256(b"segment-one").hexdigest()
    assert rows[1]["video"] == str(seg2.resolve())
    assert rows[1]["start_s"] == 4 and rows[1]["end_s"] == 8
    # 新会话不再写旧进度文件：账是唯一真源
    assert not shot_ledger.legacy_progress_path(session.directory).is_file()


def test_v2_stopped_segment_writes_no_row(tmp_path, monkeypatch, capsys):
    """判「停下」的轮次不落通过行——落行＝这段过了，被拦下的不能算过。"""
    session = _session(tmp_path)
    seg1 = _video(session.directory, "seg1.mp4")
    _patch_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _confirm_series(monkeypatch, [False])
    _answers(monkeypatch, ["", str(seg1), ""])  # 生成好 → 交视频 → 判未达到 → 回车=停下

    finished = wizard._run_segmented_flow_v2(session, _plans(2),
                                             session.stage_state, SimpleNamespace())

    assert finished is False
    assert _rows(session.directory) == []
    assert "全部 2 段已人工确认完成" not in capsys.readouterr().out


def test_v2_rerun_round_records_only_the_video_that_passed(tmp_path, monkeypatch):
    """判「重跑本段」的那一轮不落行；重新交回并通过的那份视频才是账上的那行。"""
    session = _session(tmp_path)
    bad = _video(session.directory, "bad.mp4", b"bad")
    good = _video(session.directory, "good.mp4", b"good")
    last = _video(session.directory, "last.mp4")
    _patch_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _confirm_series(monkeypatch, [False, True, True])
    _answers(monkeypatch, ["", str(bad), "2", str(good), "", str(last)])

    finished = wizard._run_segmented_flow_v2(session, _plans(2),
                                             session.stage_state, SimpleNamespace())

    assert finished is True
    rows = _rows(session.directory)
    assert [row["segment"] for row in rows] == [1, 2]
    assert rows[0]["video"] == str(good.resolve()), "账上留的是通过的那份，不是被重跑的那份"


# --- 续跑只认新账 -------------------------------------------------------------

def _write_plan(directory: Path, total: int) -> None:
    (directory / "segments").mkdir(parents=True, exist_ok=True)
    (directory / "segments" / "plan.json").write_text(
        json.dumps([p.to_dict() for p in _plans(total)], ensure_ascii=False, indent=2),
        encoding="utf-8")


def _seed_row(directory: Path, segment: int, *, start_s: int, end_s: int, name: str) -> Path:
    video = _video(directory, name)
    shot_ledger.append_shot_output(directory, segment=segment, start_s=start_s,
                                   end_s=end_s, video_path=video)
    return video


def test_resume_comes_from_the_ledger_and_skips_passed_segments(tmp_path, monkeypatch, capsys):
    """续跑起点只来自新账；已通过的段不重问视频、不重写提示词。"""
    session = _session(tmp_path)
    _write_plan(session.directory, 4)
    for number in (1, 2):
        _seed_row(session.directory, number, start_s=(number - 1) * 4, end_s=number * 4,
                  name=f"passed{number}.mp4")
        (session.directory / "segments" / f"shot-{number:02d}.md").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    third = _video(session.directory, "seg3.mp4")
    last = _video(session.directory, "seg4.mp4")
    _patch_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_planner, "plan_segments",
                        lambda *a, **k: pytest.fail("续接时重跑了分段规划"))
    written: list[int] = []

    def fake_write(plan, *a, **k):
        written.append(plan.index)
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)
    _confirm_series(monkeypatch, [True, True])
    _answers(monkeypatch, ["", str(third), "", str(last)])

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is True
    assert "检测到已完成 2/4 段" in out
    assert written == [2, 3], f"续跑没有从第 3 段起：{written}"
    assert [row["segment"] for row in _rows(session.directory)] == [1, 2, 3, 4]


def test_resume_with_every_segment_passed_asks_nothing(tmp_path, monkeypatch, capsys):
    """全部段都已通过（会话没被标完成就退出）：续跑不再重问段路径、不重跑闸门。"""
    session = _session(tmp_path)
    _write_plan(session.directory, 2)
    for number in (1, 2):
        _seed_row(session.directory, number, start_s=(number - 1) * 4, end_s=number * 4,
                  name=f"passed{number}.mp4")
        (session.directory / "segments" / f"shot-{number:02d}.md").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    _patch_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_planner, "plan_segments",
                        lambda *a, **k: pytest.fail("续接时重跑了分段规划"))
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: pytest.fail("已通过的段不该重写提示词"))
    asked: list[str] = []
    monkeypatch.setattr(wizard, "_prompt", lambda text, *a, **k: asked.append(text) or "")
    monkeypatch.setattr(wizard, "_acquire_bridge_frame",
                        lambda *a, **k: pytest.fail("已通过的段不该再问路径"))
    monkeypatch.setattr(wizard, "_verify_last_frame",
                        lambda *a, **k: pytest.fail("末段已通过，不该再核一次"))

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    assert finished is True
    assert asked == [], f"续跑不该再问任何路径：{asked}"


@pytest.mark.parametrize("corruptor, needle", [
    ("broken_json", "不是合法 JSON"),
    ("gap", "跳号"),
])
def test_broken_ledger_stops_loudly_instead_of_starting_over(
        tmp_path, monkeypatch, capsys, corruptor, needle):
    """账行错形／跳号 → 响错停住，不许静默从第 1 段重来（那会把已通过的段当没做过）。"""
    session = _session(tmp_path)
    _write_plan(session.directory, 4)
    ledger = shot_ledger.shot_ledger_path(session.directory)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if corruptor == "broken_json":
        ledger.write_text("{ 这不是 JSON\n", encoding="utf-8")
    else:
        _seed_row(session.directory, 1, start_s=0, end_s=4, name="a.mp4")
        _seed_row(session.directory, 3, start_s=8, end_s=12, name="c.mp4")
    _patch_capture(monkeypatch, tmp_path)
    written: list[int] = []
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda plan, *a, **k: written.append(plan.index) or "SEG")

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is False
    assert "[错误]" in out and needle in out
    assert written == [], "账读不回来却继续写段了"


def test_fallback_path_rejects_a_gapped_ledger_before_writing_anything(
        tmp_path, monkeypatch, capsys):
    """回退路径同样要核「账与段表对得上」（2026-10-27 复审缺口）。

    ``progress.start = len(passed)`` 把 {1,3} 读成「过了 2 段」；不核就会从第 3 段
    （position=2）起跑——**第 2 段永远不做、还不报错**。这里必须停住、不写任何段。
    """
    session = _session(tmp_path, duration=16.0)
    _seed_row(session.directory, 1, start_s=0, end_s=4, name="a.mp4")
    _seed_row(session.directory, 3, start_s=8, end_s=12, name="c.mp4")
    _patch_capture(monkeypatch, tmp_path)
    # 强制走机械回退：无 plan.json + 规划失败
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: None)
    rewritten: list[int] = []
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt",
                        lambda segment, *a, **k: rewritten.append(segment.shot_number) or "SEG")
    _answers(monkeypatch, [])

    finished = wizard._run_segmented_flow(session.brief, session, FOUR_SHOT_PROMPT, None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is False
    assert "[错误]" in out and "跳号" in out
    assert rewritten == [], "账对不上却继续写段了"
    assert not (session.directory / "segments" / "shot-01.md").exists()
    assert [row["segment"] for row in _rows(session.directory)] == [1, 3], \
        "账不该被这次失败改动"


# --- 旧会话兼容 ---------------------------------------------------------------

def test_legacy_session_falls_back_to_progress_and_says_the_film_is_unavailable(
        tmp_path, monkeypatch, capsys):
    """旧会话（无新账）回退读 progress.json 继续跑，并当面说破拼接不可用。"""
    session = _session(tmp_path)
    _write_plan(session.directory, 4)
    for number in (1, 2):
        (session.directory / "segments" / f"shot-{number:02d}.md").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    legacy = shot_ledger.legacy_progress_path(session.directory)
    legacy.write_text(json.dumps({"done": 2, "total": 4}), encoding="utf-8")
    third = _video(session.directory, "seg3.mp4")
    last = _video(session.directory, "seg4.mp4")
    _patch_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _confirm_series(monkeypatch, [True, True])
    _answers(monkeypatch, ["", str(third), "", str(last)])

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is True
    assert "拼接不可用" in out, "旧会话必须当面说破：旧账没记段视频，拼不出成片"
    assert "检测到已完成 2/4 段" in out
    # 没给旧会话回溯补账（票面 Out of Scope），所以本轮也不写新账
    assert _rows(session.directory) == []
    # 旧会话继续用旧进度续跑
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"done": 4, "total": 4}


def test_new_session_without_any_ledger_is_not_treated_as_legacy(tmp_path, monkeypatch, capsys):
    """两条落盘物都不在＝一次新陪跑，不该冒出「拼接不可用」。"""
    session = _session(tmp_path)
    _write_plan(session.directory, 2)
    seg1 = _video(session.directory, "seg1.mp4")
    last = _video(session.directory, "last.mp4")
    _patch_capture(monkeypatch, tmp_path)
    # 零进度时走的是「规划 → v2」的新跑路径（缓存只在已有进度时短路规划）
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans(2))
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _confirm_series(monkeypatch, [True, True])
    _answers(monkeypatch, ["", str(seg1), "", str(last)])

    wizard._run_segmented_flow(session.brief, session, "prompt", None, session.stage_state)

    assert "拼接不可用" not in capsys.readouterr().out
    assert [row["segment"] for row in _rows(session.directory)] == [1, 2]


def test_long_form_hands_the_passed_rows_to_the_finish_point(tmp_path, monkeypatch):
    """收尾处（#96 的拼接落点）能拿到全部通过行；没跑完就是空表。"""
    session = _session(tmp_path)
    _write_plan(session.directory, 2)
    for number in (1, 2):
        _seed_row(session.directory, number, start_s=(number - 1) * 4, end_s=number * 4,
                  name=f"passed{number}.mp4")
    monkeypatch.setattr(wizard, "_run_segmented_flow", lambda *a, **k: True)

    passed = wizard._run_long_form(session.brief, session, "prompt", None, {},
                                   session.directory)

    assert sorted(passed) == [1, 2]
    assert passed[2]["video"].endswith("passed2.mp4")
    assert session_store.load_session(session.directory).status == \
        session_store.STATUS_COMPLETED


def test_long_form_returns_no_rows_when_the_flow_stops(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr(wizard, "_run_segmented_flow", lambda *a, **k: False)

    passed = wizard._run_long_form(session.brief, session, "prompt", None, {},
                                   session.directory)

    assert passed == {}
    assert session_store.load_session(session.directory).status == \
        session_store.STATUS_SEGMENTED_RUNNING


# --- 末段一律索要视频 ---------------------------------------------------------

def _state(*, with_last: bool = False) -> dict:
    frames = [] if not with_last else [
        {"picture": 2, "role": "last", "path": "b.png", "description": "承诺的尾帧画面"}]
    return {"shot_table": "[Shot 1] x", "fl2va_frame_descriptions": frames}


def test_last_segment_video_is_asked_for_even_without_a_promised_tail(
        tmp_path, monkeypatch, capsys):
    """无尾帧变体也要索要末段输出视频（拼成片要用），但只做可证层，不剥帧不读图。"""
    from minimax_h3_prompt.tools import frame_auditor

    session = _session(tmp_path)
    video = _video(session.directory, "last.mp4")
    monkeypatch.setattr(frame_auditor, "extract_last_frame",
                        lambda *a, **k: pytest.fail("没有尾帧承诺时不该剥帧核验"))
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    _answers(monkeypatch, [str(video)])
    seen: list[str] = []

    allowed = wizard._verify_last_frame(
        _state(), 1, session.directory, end_hook=None, segment_number=2, total=2,
        expected_seconds=4.0, on_pass=seen.append)

    out = capsys.readouterr().out
    assert allowed is True
    assert seen == [str(video)], "末段通过后要把视频路径交给落账回调"
    assert "末段输出视频" in out
    assert not (session.directory / "segments" / "gate-log.jsonl").is_file(), \
        "没有可核的承诺，不该产出闸门遭遇记录"


def test_last_segment_without_promised_tail_still_blocks_on_a_missing_video(
        tmp_path, monkeypatch, capsys):
    """索要之后拿不到视频就停下：没有末段视频就没有成片，不能假装完成。"""
    session = _session(tmp_path)
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    _answers(monkeypatch, [""])

    allowed = wizard._verify_last_frame(
        _state(), 1, session.directory, end_hook=None, segment_number=2, total=2,
        expected_seconds=4.0)

    assert allowed is False
    assert "没有拿到末段输出视频" in capsys.readouterr().out


def test_last_segment_without_promised_tail_still_runs_the_provable_check(
        tmp_path, monkeypatch):
    """没有语义层可核，可证层照样要判：交一段时长不对的视频不能放行。"""
    from minimax_h3_prompt.tools.preflight import Issue

    session = _session(tmp_path)
    video = _video(session.directory, "bad.mp4")
    monkeypatch.setattr(
        wizard, "_gate_segment_handoff",
        lambda *a, **k: [Issue("error", "duration_wrong", "时长不对")])
    _answers(monkeypatch, [str(video), ""])

    assert wizard._verify_last_frame(
        _state(), 1, session.directory, end_hook=None, segment_number=2, total=2,
        expected_seconds=4.0) is False


# --- 新交付口轮换旧账 ---------------------------------------------------------

def test_new_delivery_rotates_the_previous_ledger(tmp_path, monkeypatch):
    """复用 GEN 重走一遍流程时，开跑分段陪跑之前必须把上一轮的账挪开。"""
    gen = tmp_path / "GEN001"
    gen.mkdir()
    _write_plan(gen, 2)
    _seed_row(gen, 1, start_s=0, end_s=4, name="stale.mp4")
    shot_ledger.legacy_progress_path(gen).write_text(
        json.dumps({"done": 1, "total": 2}), encoding="utf-8")
    session = session_store.SessionState(
        directory=gen, brief=_brief(18.0), stage_state={},
        status=session_store.STATUS_AWAITING_FRAMES)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(wizard, "run_stage2", lambda *a, **k: ({}, "整条提示词"))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *a, **k: None)
    seen: list[bool] = []

    def fake_long_form(*a, **k):
        seen.append(shot_ledger.shot_ledger_path(gen).is_file())

    monkeypatch.setattr(wizard, "_run_long_form", fake_long_form)

    assert wizard._phase2_collect_and_finish(SimpleNamespace(sessions_root=str(tmp_path)),
                                             session) == 0

    assert seen == [False], "进分段陪跑时旧账还在，本轮会读到上一轮的通过行"
    assert not shot_ledger.legacy_progress_path(gen).is_file()
