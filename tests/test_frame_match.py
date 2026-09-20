"""frame_match 帧读取与比对原语测试。"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from minimax_h3_prompt.tools.frame_match import (
    ANALYZE_H,
    ANALYZE_W,
    imread_unicode,
    mad,
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
