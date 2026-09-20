"""preflight 预检测试。"""
from __future__ import annotations

import cv2
import pytest

from minimax_h3_prompt.tools.frame_match import read_window
from minimax_h3_prompt.tools.ledger import Discarded, Ledger, SCHEMA
from minimax_h3_prompt.tools.preflight import (
    EXPECTED_VIDEO_SIZE,
    check_input_frame,
    check_not_discarded,
    check_shot_duration,
)


def _write_png(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buf.tobytes())


def _probe_from_tail(video, size=(1280, 736)):
    tail = read_window(video, window="tail", k=1)[0]
    return cv2.cvtColor(cv2.resize(tail, size, interpolation=cv2.INTER_NEAREST),
                        cv2.COLOR_GRAY2BGR)


def test_expected_size_matches_h3_output():
    assert EXPECTED_VIDEO_SIZE == (1280, 736)


def test_correct_input_frame_passes(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    frame_path = tmp_path / "shot-02-start.png"
    _write_png(frame_path, _probe_from_tail(prev))
    issues = check_input_frame(frame_path, prev)
    assert issues == []


def test_wrong_input_frame_is_error(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    unrelated = tmp_video("unrelated.mp4", base=(200, 200, 200))
    frame_path = tmp_path / "shot-02-start.png"
    _write_png(frame_path, _probe_from_tail(unrelated))
    issues = check_input_frame(frame_path, prev)
    assert [i.severity for i in issues] == ["error"]
    assert issues[0].code == "bridge_frame_mismatch"


def test_missing_input_frame_is_error(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    issues = check_input_frame(tmp_path / "无.png", prev)
    assert issues[0].code == "bridge_frame_missing"


def test_wrong_size_is_warning(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    frame_path = tmp_path / "shot-02-start.png"
    _write_png(frame_path, _probe_from_tail(prev, size=(640, 360)))
    issues = check_input_frame(frame_path, prev)
    assert any(i.code == "bridge_frame_size" and i.severity == "warning" for i in issues)


def test_duration_within_h3_grid_passes():
    assert check_shot_duration(6.0) == []
    assert check_shot_duration(10.0) == []


def test_duration_outside_grid_is_error():
    assert check_shot_duration(12.0)[0].code == "duration_out_of_range"
    assert check_shot_duration(3.0)[0].code == "duration_out_of_range"


def test_discarded_prev_video_is_error():
    ledger = Ledger(schema=SCHEMA, session={}, scan={}, shots=[],
                    discarded=[Discarded(video="old.mp4", video_dir="d", reason="废片",
                                         replaced_by="new.mp4", confidence="high")],
                    interruptions=[], review_needed=[], warnings=[])
    issues = check_not_discarded("old.mp4", ledger)
    assert issues[0].code == "prev_video_discarded"
    assert issues[0].severity == "error"
    assert check_not_discarded("new.mp4", ledger) == []
