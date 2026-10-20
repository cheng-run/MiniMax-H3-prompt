"""手动拼接命令（``ref-concat``，issue #57 AC4）测试。

它存在的理由：**重跑某段之后不必从头走完整流程**。段序取 ``segments/plan.json``、
「哪几段过了」取段产出账的 ``event=output`` 行——与向导里的自动拼接**同一份编排**
（``ref_segments.assemble_final_film``），所以「少一段就拒绝」的牙齿也只有一副。
"""
from __future__ import annotations

import json
from pathlib import Path

from minimax_h3_prompt.main import build_parser, main
from minimax_h3_prompt.ref_segments import log_segment_output
from minimax_h3_prompt.segment_planner import SegmentPlan


def test_parser_has_ref_concat_with_replace():
    args = build_parser().parse_args(
        ["ref-concat", "GEN001", "--replace", "3", "new.mp4"])
    assert args.command == "ref-concat"
    assert args.session_dir == "GEN001"
    assert args.replace == [["3", "new.mp4"]]


def _write_plan(directory: Path, windows: list[tuple[int, int]]) -> None:
    seg_dir = directory / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    plans = [
        SegmentPlan(index=index, start_s=start, end_s=end,
                    shots_in_segment=(index + 1,), summary=f"seg{index + 1}",
                    end_hook="the scene at the segment's end",
                    start_state="the scene at the segment's start")
        for index, (start, end) in enumerate(windows)
    ]
    (seg_dir / "plan.json").write_text(
        json.dumps([p.to_dict() for p in plans], ensure_ascii=False), encoding="utf-8")


def test_ref_concat_builds_the_film_from_the_ledger(tmp_path, tmp_video, capsys):
    """全段已在账上 → 一条命令拼出成片（段序照 plan.json）。"""
    from minimax_h3_prompt.tools.frame_match import video_meta

    _write_plan(tmp_path, [(0, 2), (2, 5)])
    first = tmp_video("s1.mp4", frames=48)    # 2.0s 整
    second = tmp_video("s2.mp4", frames=72)   # 3.0s 整
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=2, video_path=first)
    log_segment_output(tmp_path, segment=2, start_s=2, end_s=5, video_path=second)

    code = main(["ref-concat", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    film = tmp_path / "final.mp4"
    assert film.is_file()
    assert str(film) in out
    assert round(video_meta(film)["duration_s"], 3) == 5.0, "成片时长＝各段计划时长之和"
    # 这个目录没有会话文件 → 起步总时长取不到，**说破**后按计划值核对（不静默冒充）
    assert "读不到会话文件里的起步总时长" in out


def test_ref_concat_refuses_and_names_the_missing_segment(tmp_path, tmp_video, capsys):
    """少一段 → 不产出成片、点名卡在哪一段、返回码非零（缺段成片是伪产物）。"""
    _write_plan(tmp_path, [(0, 2), (2, 4), (4, 6)])
    first = tmp_video("s1.mp4", frames=48)
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=2, video_path=first)

    code = main(["ref-concat", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert not (tmp_path / "final.mp4").exists()
    assert "未产出成片" in out and "第 2、3 段尚未通过验收" in out


def test_ref_concat_replace_uses_the_rerun_segment(tmp_path, tmp_video, capsys):
    """重跑某段后单独触发：``--replace`` 指定该段的新视频，不必从头走流程。"""
    from minimax_h3_prompt.tools.frame_match import video_meta

    _write_plan(tmp_path, [(0, 2), (2, 5)])
    first = tmp_video("s1.mp4", frames=48)
    stale = tmp_video("s2-old.mp4", frames=72)
    rerun = tmp_video("s2-new.mp4", frames=72, offset=60)
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=2, video_path=first)
    log_segment_output(tmp_path, segment=2, start_s=2, end_s=5, video_path=stale)

    code = main(["ref-concat", str(tmp_path), "--replace", "2", str(rerun)])
    out = capsys.readouterr().out
    assert code == 0
    assert "第 2 段改用你指定的视频" in out, "替换必须当面说破（这一份没经过本系统验收）"
    film = tmp_path / "final.mp4"
    assert film.is_file()
    # 成片用的是**新**那一份：两段帧数相同，内容差异体现在接缝帧上
    from minimax_h3_prompt.tools.frame_match import mad, read_frames_gray
    frames = read_frames_gray(film)
    assert len(frames) == 48 + 72
    assert mad(frames[48], read_frames_gray(rerun)[0]) < 1.0
    assert video_meta(film)["frames"] == 120


def test_ref_concat_without_plan_refuses_with_a_pointer(tmp_path, capsys):
    """没有 plan.json（不是多参生成目录）→ 说破并返回非零，不静默从零开始。"""
    code = main(["ref-concat", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "读不到分段规划" in out


def test_ref_concat_missing_directory_returns_one(tmp_path, capsys):
    code = main(["ref-concat", str(tmp_path / "无")])
    assert code == 1
    assert "不存在" in capsys.readouterr().out


def test_ref_concat_replace_with_unknown_segment_is_refused(tmp_path, tmp_video, capsys):
    """段号写错（本片没这一段）当面报——不报的话这一份会被静默丢掉，用户以为换上了。"""
    _write_plan(tmp_path, [(0, 2)])
    first = tmp_video("s1.mp4", frames=48)
    other = tmp_video("s9.mp4", frames=48)
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=2, video_path=first)

    code = main(["ref-concat", str(tmp_path), "--replace", "9", str(other)])
    out = capsys.readouterr().out
    assert code == 1
    assert "本片没有第 9 段" in out
    assert not (tmp_path / "final.mp4").exists()


def test_ref_concat_takes_the_total_duration_from_the_session(tmp_path, tmp_video, capsys):
    """起步总时长取**会话里的**（用户目标），不是各段计划时长之和——末段留余量时两者不等。

    两者混用就是「同一份契约两处口径」：段表允许末段超 total（留余量），拿计划值冒充
    起步总时长，报出来的「成片该多长」就不是用户要的那个数了。
    """
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.session_store import STATUS_AWAITING_FRAMES, save_session

    _write_plan(tmp_path, [(0, 2), (2, 5)])          # 段表覆盖到 5s
    save_session(tmp_path, Brief(mode="ref", variant="T2VA", duration=4.0, plot="x"),
                 {}, status=STATUS_AWAITING_FRAMES)
    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=72)
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=2, video_path=first)
    log_segment_output(tmp_path, segment=2, start_s=2, end_s=5, video_path=second)

    code = main(["ref-concat", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "读不到会话文件" not in out
    assert "起步总时长 4s（各段计划时长之和 5s，末段留余量）" in out


def test_ref_concat_with_an_empty_plan_refuses(tmp_path, capsys):
    """段表是空的（手改过 plan.json）→ 说破并拒绝，不去跑一趟注定失败的 ffmpeg。"""
    _write_plan(tmp_path, [])
    code = main(["ref-concat", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "段表是空的" in out
