"""preflight 预检测试。"""
from __future__ import annotations

import cv2
import pytest

from minimax_h3_prompt.tools.frame_match import read_window
from minimax_h3_prompt.tools.ledger import Discarded, Ledger, SCHEMA
from minimax_h3_prompt.tools.preflight import (
    EXPECTED_VIDEO_SIZE,
    FROZEN_WORDS,
    check_budget,
    check_frozen_words,
    check_input_frame,
    check_not_discarded,
    check_shot_duration,
    estimate_remaining_shots,
    find_frozen_words,
    suggest_defrost,
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


def test_frozen_words_cover_the_ones_found_in_the_plan():
    for word in ("停住", "定格", "静止", "不再变化", "停在"):
        assert word in FROZEN_WORDS


def test_find_frozen_words_detects():
    assert find_frozen_words("少年垂眼，双手停在巨书封面上") == ["停在"]
    assert find_frozen_words("镜头停在掌心尺度的特写") == ["停在"]


def test_find_frozen_words_none_when_clean():
    assert find_frozen_words("纸鸟卧进摊开掌心，头颈朝向少年") == []


def test_check_frozen_words_is_warning_not_error():
    issues = check_frozen_words("她停住脚步")
    assert issues and all(i.severity == "warning" for i in issues)
    assert issues[0].code == "frozen_word"


def test_suggest_defrost_replaces_hold_verbs():
    out = suggest_defrost("双手停在巨书封面上")
    assert "停在" not in out
    assert "刚落在" in out      # 换成了 _DEFROST_REPLACEMENTS 里的"活"词


def test_estimate_remaining_shots():
    assert estimate_remaining_shots(remaining_tokens=200_000, tokens_per_shot=50_000) == 4
    assert estimate_remaining_shots(remaining_tokens=0, tokens_per_shot=50_000) == 0
    assert estimate_remaining_shots(remaining_tokens=100, tokens_per_shot=0) == 0


def test_check_budget_refrains_when_short():
    issues = check_budget(remaining_tokens=50_000, tokens_per_shot=50_000,
                          remaining_shots=3)
    assert issues and issues[0].severity == "refrain"
    assert "还能跑 1 段" in issues[0].message


def test_check_budget_silent_when_plenty():
    assert check_budget(remaining_tokens=10_000_000, tokens_per_shot=50_000,
                        remaining_shots=3) == []
