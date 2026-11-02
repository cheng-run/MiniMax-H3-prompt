"""多参段首/末帧的剥离与读图（issue #56 AC1 的工具侧）。

接缝：``tools.frame_auditor.extract_first_frame`` / ``describe_segment_frame``。
语义层要摆给人看的是**本段视频真实的首帧与末帧**，所以：
- 首帧取 0.00s 那一帧（**不做锐度优选**）——语义核验判的是「开场瞬间的画面」，
  挑一张更锐利的邻近帧就是另一个时刻的画面，判据当场错位（尾帧的锐度优选是
  桥接链的理由：那张图要当下一段的 first_frame 参考、不进成片时间线）；
- 读图走 ``reference_auditor.describe_image`` 唯一口，失败**说破**并降级（不伪造
  读图记录、不阻塞人工陪跑）。
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from minimax_h3_prompt.tools import frame_auditor, reference_auditor
from minimax_h3_prompt.tools.preflight import EXPECTED_VIDEO_SIZE


def _ramp_video(path, frames: int = 30, fps: int = 24):
    """写一段亮度逐帧递增的真视频：帧号可从像素值反读（首帧=20，末帧=20+frames-1）。"""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                             EXPECTED_VIDEO_SIZE)
    assert writer.isOpened(), "测试环境缺 MJPG 编码器"
    image = np.zeros((EXPECTED_VIDEO_SIZE[1], EXPECTED_VIDEO_SIZE[0], 3), np.uint8)
    for index in range(frames):
        image[:] = 20 + index
        writer.write(image)
    writer.release()
    return path


def _mean(path) -> float:
    image = cv2.imread(str(path))
    assert image is not None, f"读不到剥出的帧：{path}"
    return float(image.mean())


def test_extract_first_frame_takes_the_frame_at_zero(tmp_path):
    """首帧＝0.00s 那一帧（亮度 20），不是前几帧里更锐利的某一张。"""
    video = _ramp_video(tmp_path / "seg.avi", frames=30)
    frame = frame_auditor.extract_first_frame(video, tmp_path / "frames" / "first.png")
    assert frame == tmp_path / "frames" / "first.png"
    assert frame.is_file()
    assert _mean(frame) == pytest.approx(20.0, abs=3.0)


def test_extract_last_frame_still_takes_the_tail(tmp_path):
    """重构后的回归保护：尾帧仍取尾部（30 帧的末帧亮度 49），行为不许被首帧改动带偏。"""
    video = _ramp_video(tmp_path / "seg.avi", frames=30)
    frame = frame_auditor.extract_last_frame(video, tmp_path / "last.png")
    assert _mean(frame) == pytest.approx(49.0, abs=3.0)


def test_extract_first_frame_missing_video_raises(tmp_path):
    """文件不在是**可证事实**，显式失败（不静默返回一张空图）。"""
    with pytest.raises(FileNotFoundError):
        frame_auditor.extract_first_frame(tmp_path / "nope.avi", tmp_path / "f.png")


def test_describe_segment_frame_uses_the_single_image_reader(tmp_path, monkeypatch):
    """读图走 ``describe_image`` 唯一口，且问句点明这是本段的首帧/末帧。"""
    frame = tmp_path / "first.png"
    frame.write_bytes(b"png")
    seen: list[str] = []

    def fake_describe(path, prompt):
        seen.append(prompt)
        return "  画面里女孩举着陶罐  "

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    assert frame_auditor.describe_segment_frame(frame, "first") == "画面里女孩举着陶罐"
    assert "首帧" in seen[0] and "0.00" in seen[0]

    assert frame_auditor.describe_segment_frame(frame, "last") == "画面里女孩举着陶罐"
    assert "末帧" in seen[1]


def test_describe_segment_frame_failure_warns_and_returns_none(tmp_path, monkeypatch, capsys):
    """读图失败：显式警告 + 返回 None（调用方照旧并排展示「读图失败」，不阻塞陪跑）。"""
    frame = tmp_path / "last.png"
    frame.write_bytes(b"png")

    def boom(path, prompt):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    assert frame_auditor.describe_segment_frame(frame, "last") is None
    assert "读图失败" in capsys.readouterr().out


def test_describe_segment_frame_rejects_an_unknown_role(tmp_path):
    """帧位取值错是**编程错误**，响；不许被「单帧失败降级」那条路吞成 None。"""
    frame = tmp_path / "f.png"
    frame.write_bytes(b"png")
    with pytest.raises(ValueError):
        frame_auditor.describe_segment_frame(frame, "middle")
