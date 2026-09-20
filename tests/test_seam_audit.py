"""seam_audit 接缝审计测试。"""
from __future__ import annotations

import json

import numpy as np
import pytest

from minimax_h3_prompt.tools.seam_audit import (
    STATIC_THR,
    AuditReport,
    analyze,
    compare_to_baseline,
    diff_curve,
    find_ffmpeg,
    leading_run,
    load_baseline,
    longest_run,
    promote_baseline,
    render_markdown,
    sanity_check,
    trailing_run,
)


def _frames(values: list[float]) -> list[np.ndarray]:
    """构造一串单像素灰度帧，使相邻差恰好等于 values。"""
    out = [np.zeros((2, 2), dtype=np.float32)]
    for v in values:
        out.append(out[-1] + v)
    return out


def test_diff_curve_length_is_frames_minus_one():
    assert len(diff_curve(_frames([1.0, 2.0, 3.0]))) == 3


def test_trailing_run_counts_consecutive_still_frames():
    diffs = [9.0, 8.0, 0.2, 0.3, 0.1]
    assert trailing_run(diffs) == 3


def test_trailing_run_zero_when_last_frame_moves():
    assert trailing_run([0.1, 0.2, 9.0]) == 0


def test_leading_run_counts_from_start():
    assert leading_run([0.1, 0.2, 3.0, 9.0, 9.0]) == 2
    assert leading_run([9.0, 0.1]) == 0


def test_longest_run_finds_max_anywhere():
    assert longest_run([0.1, 0.1, 9.0, 0.1, 0.1, 0.1]) == 3


def test_threshold_constant_is_one():
    assert STATIC_THR == 1.0


def test_sanity_check_flags_fully_static_clip():
    """整段全静止的视频——那是素材坏了，不是接缝问题。"""
    assert sanity_check([0.01] * 30) is not None


def test_sanity_check_passes_normal_clip():
    assert sanity_check([3.0, 5.0, 2.0] * 10) is None


def test_find_ffmpeg_returns_existing_path():
    assert find_ffmpeg().is_file()


def test_analyze_builds_report_and_merges(tmp_path, tmp_video):
    a = tmp_video("a.mp4", frames=24)
    b = tmp_video("b.mp4", frames=24)
    report = analyze([a, b], tmp_path / "audit")
    assert [s.shot for s in report.shots] == [1, 2]
    assert len(report.seams) == 1
    assert report.duration_s > 0
    assert (tmp_path / "audit" / "merged_naive.mp4").is_file()


def test_analyze_without_merge(tmp_path, tmp_video):
    report = analyze([tmp_video("a.mp4"), tmp_video("b.mp4")], tmp_path / "audit",
                     merge=False)
    assert not (tmp_path / "audit" / "merged_naive.mp4").exists()
    assert report.merged_path == ""


def test_analyze_single_shot_has_no_seams(tmp_path, tmp_video):
    report = analyze([tmp_video("only.mp4")], tmp_path / "audit", merge=False)
    assert report.seams == []
    assert report.still_seconds == 0.0


def test_compare_to_baseline_reports_per_seam_average():
    report = AuditReport(shots=[], seams=[], duration_s=30.0, still_seconds=6.0,
                         still_ratio=0.2, per_seam_avg_frames=36.0, warnings=[],
                         merged_path="")
    baseline = {"per_seam_avg_frames": 24.3, "seam_still_ratio": 0.141}
    rows = compare_to_baseline(report, baseline)
    labels = [r[0] for r in rows]
    assert any("每缝均值" in label for label in labels)
    assert any("占全片" in label for label in labels)


def test_render_markdown_warns_against_comparing_totals_across_shot_counts():
    report = AuditReport(shots=[], seams=[], duration_s=30.0, still_seconds=6.0,
                         still_ratio=0.2, per_seam_avg_frames=36.0, warnings=[],
                         merged_path="")
    text = render_markdown(report, {"shots": 5, "per_seam_avg_frames": 24.3})
    assert "每缝均值" in text
    assert "镜头数" in text


def test_promote_and_load_baseline_roundtrip(tmp_path, tmp_video):
    session = tmp_path / "GEN001"
    session.mkdir()
    report = analyze([tmp_video("a.mp4"), tmp_video("b.mp4")],
                     tmp_path / "audit", merge=False)
    path = promote_baseline(session, report, "修复前")
    assert path.is_file()
    loaded = load_baseline(session)
    assert loaded is not None
    assert loaded["label"] == "修复前"
    assert loaded["per_seam_avg_frames"] == report.per_seam_avg_frames


def test_load_baseline_missing_returns_none(tmp_path):
    assert load_baseline(tmp_path) is None
