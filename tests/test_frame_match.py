"""frame_match 帧读取与比对原语测试。"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from minimax_h3_prompt.tools.frame_match import (
    ANALYZE_H,
    ANALYZE_W,
    MAD_MAYBE,
    MAD_SAME,
    classify_match,
    imread_unicode,
    mad,
    match_video,
    read_frames_gray,
    read_window,
    to_gray,
    video_meta,
)


def test_imread_unicode_reads_chinese_path(tmp_path):
    """cv2.imread 打不开中文路径，imread_unicode 必须能打开。"""
    target = tmp_path / "中文目录" / "桥接帧.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    img = np.zeros((72, 128, 3), dtype=np.uint8)
    # 写也必须绕开 cv2.imwrite——它同样不支持中文路径
    ok, buf = cv2.imencode(".png", img)
    assert ok
    target.write_bytes(buf.tobytes())

    loaded = imread_unicode(target)
    assert loaded is not None
    assert loaded.shape == (72, 128, 3)


def test_imread_unicode_missing_file_returns_none(tmp_path):
    assert imread_unicode(tmp_path / "不存在.png") is None


def test_to_gray_shape_and_dtype(tmp_path, tmp_video):
    video = tmp_video()
    frames = read_frames_gray(video)
    assert frames, "应至少读到一帧"
    assert frames[0].shape == (ANALYZE_H, ANALYZE_W)
    assert frames[0].dtype == np.float32


def test_mad_identical_is_zero(tmp_path, tmp_video):
    frames = read_frames_gray(tmp_video())
    assert mad(frames[0], frames[0]) == 0.0


def test_mad_different_frames_is_positive(tmp_path, tmp_video):
    frames = read_frames_gray(tmp_video(frames=24))
    assert mad(frames[0], frames[-1]) > 1.0


def test_video_meta_reports_dimensions_and_duration(tmp_path, tmp_video):
    video = tmp_video(frames=24, fps=24, width=128, height=72)
    meta = video_meta(video)
    assert meta["width"] == 128
    assert meta["height"] == 72
    assert meta["fps"] == pytest.approx(24.0)
    assert meta["duration_s"] == pytest.approx(1.0, abs=0.05)


def test_video_meta_missing_file_returns_empty(tmp_path):
    assert video_meta(tmp_path / "无.mp4") == {}


def test_read_window_head_and_tail_do_not_overlap(tmp_path, tmp_video):
    video = tmp_video(frames=24)
    head = read_window(video, window="head", k=5)
    tail = read_window(video, window="tail", k=5)
    assert len(head) == 5
    assert len(tail) == 5
    # 第 1 帧与最后一帧画面不同，所以头窗口与尾窗口不应相同
    assert mad(head[0], tail[-1]) > 1.0


def test_read_window_rejects_bad_window(tmp_path, tmp_video):
    with pytest.raises(ValueError, match="head"):
        read_window(tmp_video(), window="middle")


def test_classify_match_three_bands():
    assert classify_match(0.0) == "high"
    assert classify_match(1.99) == "high"
    assert classify_match(2.0) == "medium"
    assert classify_match(4.99) == "medium"
    assert classify_match(5.0) == "no_match"
    assert classify_match(66.98) == "no_match"


def test_match_video_finds_source_by_tail(tmp_path, tmp_video):
    """一张由某视频尾帧复制而来的图，match_video 应找出那个视频。"""
    source = tmp_video("source.mp4", frames=24)
    other = tmp_video("other.mp4", frames=24, base=(200, 200, 200))

    tail = read_window(source, window="tail", k=1)[0]
    # 把灰度小图放大回 BGR 供 match_video 使用
    probe = cv2.cvtColor(
        cv2.resize(tail, (128, 72), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
    )

    best = match_video(probe, [source, other], window="tail")
    assert best is not None
    assert best[0] == source
    assert best[1] < MAD_SAME


def test_match_video_returns_none_for_empty_list(tmp_path):
    blank = np.zeros((72, 128, 3), dtype=np.uint8)
    assert match_video(blank, [], window="tail") is None


def test_match_video_skips_unreadable_video(tmp_path, tmp_video):
    good = tmp_video("good.mp4")
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    tail = read_window(good, window="tail", k=1)[0]
    probe = cv2.cvtColor(
        cv2.resize(tail, (128, 72), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
    )
    best = match_video(probe, [broken, good], window="tail")
    assert best is not None
    assert best[0] == good
