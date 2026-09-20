"""seam_audit 接缝审计测试。"""
from __future__ import annotations

import numpy as np
import pytest

from minimax_h3_prompt.tools.seam_audit import (
    STATIC_THR,
    diff_curve,
    find_ffmpeg,
    leading_run,
    longest_run,
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
