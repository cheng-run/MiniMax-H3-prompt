"""尾帧剥离的两种取帧口径（issue #98，父 spec #92）。

**为什么同一件事要有两种口径**：``extract_last_frame`` 有两个读者，它们要的不是同一帧。

- 多参语义层（``wizard._capture_ref_segment_frames``）拿它当**本段的收尾画面**给人判，
  挑一张更锐利的邻近帧不影响判据（它比的也是"接近尾部的画面"）。
- 首尾帧链的桥接帧（``wizard._capture_bridge_frame``）是**下一段的第一帧输入**，而
  成片要在接缝处裁掉下一段的首帧。「取倒数第几帧」直接决定「回退了几帧」，进而决定
  「裁多少帧」——随机取帧会让成片的接缝回退量无法确定（spec #92：段尾落定时最后五帧
  锐度是平盘 100.9/100.8/100.0/99.8/101.9，取最大等于随机）。

所以：默认口径（最锐利）一个字不改，首尾帧链显式走「恒取末帧」，锐度降级为告警。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from minimax_h3_prompt.tools import frame_auditor
from minimax_h3_prompt.tools.frame_match import mad, to_gray

# 末 4 帧重度模糊（实测 Laplacian 方差 ≈3.4），前段锐利（≈930）；阈值 20 落在两者之间，
# 判据不靠"调阈值凑出来"。
_BLUR_FROM = 8
_FRAMES = 12


def _write_tail_blur_video(path: Path) -> Path:
    """写一段「尾部运动模糊」的小视频：后 4 帧重高斯模糊，前 8 帧边缘锐利。"""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 24, (128, 72))
    assert writer.isOpened(), f"VideoWriter 打不开 {path}"
    for index in range(_FRAMES):
        frame = np.full((72, 128, 3), 30, np.uint8)
        cv2.rectangle(frame, (8 + index * 2, 20), (28 + index * 2, 40), (255, 255, 255), -1)
        if index >= _BLUR_FROM:
            frame = cv2.GaussianBlur(frame, (31, 31), 0)
        writer.write(frame)
    writer.release()
    return path


def _decoded_frames(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    frames: list[np.ndarray] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(to_gray(frame))
    capture.release()
    assert len(frames) == _FRAMES, f"夹具没写全帧：{len(frames)}"
    return frames


def _png_gray(path: Path) -> np.ndarray:
    buffer = np.fromfile(str(path), dtype=np.uint8)
    return to_gray(cv2.imdecode(buffer, cv2.IMREAD_COLOR))


# ---------------------------------------------------------------------------
# 1. 首尾帧链口径：恒取末帧（锐度只告警）
# ---------------------------------------------------------------------------

def test_exact_tail_frame_takes_the_last_frame_even_when_a_nearby_one_is_sharper(
        tmp_path, capsys):
    """末帧模糊、倒数第 5 帧锐利时，桥接图仍是**末帧**，同时打印那条告警。

    取最锐利的那一帧（旧口径）在这里会退 4 帧——spec #92 实测这份回退在段尾冻住时
    看不出来（4 帧 = 0.167s），段尾一旦活起来就是可见的倒带；更麻烦的是它让「裁多少帧」
    随画面内容漂移。
    """
    video = _write_tail_blur_video(tmp_path / "clip.mp4")
    frames = _decoded_frames(video)

    frame = frame_auditor.extract_last_frame(
        video, tmp_path / "last.png", sharpest=False)

    captured = _png_gray(frame)
    assert mad(captured, frames[-1]) < 1.0, "桥接图必须是最后一帧"
    assert mad(captured, frames[_BLUR_FROM - 1]) > 5.0, \
        "桥接图不该退到倒数第 5 帧那张锐利图（旧口径的取法）"
    out = capsys.readouterr().out
    assert "运动模糊" in out, "末帧锐度低于阈值要告警"
    assert "下一段识别可能不稳" in out, "告警要说清后果（下一段识别可能不稳）"
    assert "照旧用末帧" in out or "仍用末帧" in out, "要说清照旧用末帧，别让人以为换了帧"


def test_exact_tail_frame_is_quiet_when_the_tail_is_sharp(tmp_path, capsys):
    """末帧够锐利时不该无端告警——告警只在真有问题时出现，否则就是噪声。"""
    video = tmp_path / "sharp.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 24, (128, 72))
    for index in range(8):
        frame = np.full((72, 128, 3), 30, np.uint8)
        cv2.rectangle(frame, (8 + index * 2, 20), (28 + index * 2, 40), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()

    frame_auditor.extract_last_frame(video, tmp_path / "last.png", sharpest=False)

    assert "运动模糊" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. 共用原语的旧口径不许被带着改（多参语义层还在用它）
# ---------------------------------------------------------------------------

def test_default_extract_last_frame_still_prefers_the_sharpest_tail_frame(tmp_path):
    """默认口径仍是「最后 5 帧里最锐利的一张」——本票只加参数，不改共用的语义。"""
    video = _write_tail_blur_video(tmp_path / "clip.mp4")
    frames = _decoded_frames(video)

    frame = frame_auditor.extract_last_frame(video, tmp_path / "sharpest.png")

    captured = _png_gray(frame)
    assert mad(captured, frames[_BLUR_FROM - 1]) < 1.0, "默认口径应取到唯一那张锐利候选"
    assert mad(captured, frames[-1]) > 5.0, "默认口径不该退化成末帧"


# ---------------------------------------------------------------------------
# 3. 接线：首尾帧链的剥帧入口走新口径
# ---------------------------------------------------------------------------

def test_bridge_frame_gate_strips_the_exact_last_frame(tmp_path, monkeypatch, capsys):
    """``_capture_bridge_frame``（base 的桥接帧/末段末帧入口）必须走「恒取末帧」。"""
    from minimax_h3_prompt.ui import wizard

    video = _write_tail_blur_video(tmp_path / "clip.mp4")
    frames = _decoded_frames(video)
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    monkeypatch.setattr(frame_auditor, "describe_bridge_frame", lambda path: None)

    errors, _description, frame = wizard._capture_bridge_frame(
        str(video), 0, tmp_path, expected_seconds=0.5)

    assert errors == []
    assert frame is not None
    assert mad(_png_gray(frame), frames[-1]) < 1.0, "桥接图必须是末帧"
    assert "运动模糊" in capsys.readouterr().out, "告警必须出现在向导这一层"
