"""段尾运动量量尺的冒烟测试（#198）。

合成两段已知运动的视频，验证量尺能把「段尾冻结」与「全程在动」分开——cv2/numpy
升级后量尺漂移时，这里会先红。再钉一条边界：量尺只读 mp4，**不进产品路径**。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scripts.measure_seam_motion import measure, read_motion

W, H, FPS = 64, 64, 24


def _write_video(path: Path, frames: list[np.ndarray]) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    assert writer.isOpened()
    for frame in frames:
        writer.write(frame)
    writer.release()


def _moving_frame(index: int) -> np.ndarray:
    """黑白竖条逐帧平移：相邻帧差稳定且非零。"""
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    offset = (index * 7) % W
    frame[:, offset:offset + W // 2] = 220
    return frame


def test_tail_freeze_is_measured_against_the_segment_median(tmp_path):
    frames = [_moving_frame(i) for i in range(20)]
    frames += [frames[-1].copy() for _ in range(10)]  # 末 10 帧完全静止
    video = tmp_path / "freeze.mp4"
    _write_video(video, frames)

    motion = read_motion(video)
    result = measure(motion, [(0, len(frames))], tail_frames=6)
    segment = result["segments"][0]

    assert segment["median_motion"] > 0
    assert segment["tail_frames"] == 6
    assert segment["tail_below_ratio"] == 6, "末 6 帧全静止，必须全部低于阈值"
    assert segment["tail_ratio"] == 0.0
    # 全片口径独立于段尾窗口：静止帧共约 9 帧（第 21 帧起）
    assert result["whole_still_frames"] >= 9


def test_continuous_motion_has_no_tail_freeze(tmp_path):
    video = tmp_path / "moving.mp4"
    _write_video(video, [_moving_frame(i) for i in range(30)])

    result = measure(read_motion(video), [(0, 30)], tail_frames=6)
    segment = result["segments"][0]

    assert segment["tail_below_ratio"] == 0
    assert segment["tail_ratio"] > 0.5


def test_boundaries_split_segments_and_last_one_reaches_the_end():
    motion = np.full(47, 5.0, dtype=np.float64)
    result = measure(motion, [(0, 24), (24, 48)], tail_frames=6)
    assert [item["frames"] for item in result["segments"]] == [24, 24]
    assert result["frames"] == 48


def test_measure_script_stays_out_of_the_product_path():
    script = Path("scripts/measure_seam_motion.py").read_text(encoding="utf-8")
    assert "minimax_h3_prompt" not in script, "量尺不许依赖产品代码（只读 mp4）"
    product_hits = [
        path for path in Path("src/minimax_h3_prompt").rglob("*.py")
        if "measure_seam_motion" in path.read_text(encoding="utf-8")
    ]
    assert product_hits == [], f"产品路径引用了量尺：{product_hits}"
