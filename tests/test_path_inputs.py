"""路径输入清洗与剥尾帧非 ASCII 路径兜底。"""
from __future__ import annotations

import numpy as np
import pytest

from minimax_h3_prompt.tools.frame_auditor import extract_last_frame
from minimax_h3_prompt.ui.wizard import _clean_path_input


# ---------------------------------------------------------------------------
# _clean_path_input：只剥首尾成对引号，无引号输入行为不变
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('"D:\\videos\\a b.mp4"', "D:\\videos\\a b.mp4"),   # 双引号（复制为路径）
    ("'D:\\videos\\a.mp4'", "D:\\videos\\a.mp4"),       # 单引号
    ("D:\\videos\\a b.mp4", "D:\\videos\\a b.mp4"),     # 无引号含空格：原样
    ("  D:\\a.mp4  ", "D:\\a.mp4"),                     # 首尾空白
    ('" D:\\a.mp4 "', "D:\\a.mp4"),                     # 引号内还有空白
    ("", ""),                                            # 空输入
    ('"', '"'),                                          # 无法成对剥：原样
    ('"a\'b"', "a'b"),                                     # 首尾同为双引号也算成对：剥
])
def test_clean_path_input(raw, expected):
    assert _clean_path_input(raw) == expected


def _make_tiny_video(path):
    """用 cv2 写一个 5 帧的极短视频。"""
    import cv2

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 48))
    if not writer.isOpened():  # pragma: no cover - 环境无编码器时跳过
        writer.release()
        pytest.skip("当前环境无 mp4v 编码器")
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[..., 2] = 200
    for _ in range(5):
        writer.write(frame)
    writer.release()


def test_extract_last_frame_non_ascii_path(tmp_path):
    """中文目录下的视频也能剥出尾帧（走临时副本兜底）。"""
    import shutil

    ascii_video = tmp_path / "seed.mp4"
    _make_tiny_video(ascii_video)
    video_dir = tmp_path / "中文目录"
    video_dir.mkdir()
    video = video_dir / "片段 01.mp4"
    shutil.copy2(ascii_video, video)  # VideoWriter 同样不吃中文路径，绕开写入侧

    out = extract_last_frame(video, tmp_path / "frame.png")
    assert out.is_file()
    assert out.stat().st_size > 0


def test_extract_last_frame_missing_file_mentions_quotes(tmp_path):
    with pytest.raises(FileNotFoundError, match="引号"):
        extract_last_frame(tmp_path / "nope.mp4")
