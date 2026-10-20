"""合成素材 fixture：不依赖真实 ComfyUI 输出，秒级可跑。

另外集中放**机器专属路径**——真实素材在仓库外（`output/` 已 gitignore），
换机器时需要覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

# 本机便利默认值（这台机器上 ComfyUI 的 H3 视频输出目录）。
# 换机器请设 H3_COMFY_OUTPUT；未设且该目录不存在时，真实素材模块整体 SKIP。
DEFAULT_COMFY_OUTPUT = Path(
    r"D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18")


def comfy_output_dir() -> Path:
    """真实素材的输出目录：``H3_COMFY_OUTPUT`` 优先，空字符串视为未设。"""
    return Path(os.environ.get("H3_COMFY_OUTPUT") or DEFAULT_COMFY_OUTPUT)


def write_test_video(
    path: Path,
    *,
    frames: int = 24,
    fps: int = 24,
    width: int = 128,
    height: int = 72,
    base: tuple[int, int, int] = (30, 30, 30),
    moving: bool = True,
    step: int = 3,
    offset: int = 0,
) -> Path:
    """写一段小 mp4：纯色背景 + 一个逐帧移动的白色方块。

    moving=False 时整段完全静止（用于测 sanity check）。

    ``step`` / ``offset`` 用来构造**尾帧接力**的相邻片段：给第 k 段的
    ``offset`` 取 ``(k-1) * step * (frames - 1)``，它的首帧方块位置就
    恰好等于上一段的尾帧，从而"某帧等于某段尾帧"这件事在磁盘上真实成立。
    默认值 3 / 0 与历史行为完全一致。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    assert writer.isOpened(), f"VideoWriter 打不开 {path}"
    for i in range(frames):
        frame = np.full((height, width, 3), base, dtype=np.uint8)
        x = 8 + offset + (i * step if moving else 0)
        cv2.rectangle(frame, (x, 20), (x + 20, 40), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()
    return path


def write_h3_tier_video(path: Path, seconds: float, *, fps: int = 24) -> Path:
    """写一段**按 H3 实测帧数档**的真视频（736×1280），供可证层判档的用例用。

    与 ``write_test_video`` 的分工：那条是通用小视频（128×72，尺寸随便）；这一条必须
    **帧数落在 ``H3_TIER_FRAMES`` 档位上、尺寸是 H3 输出尺寸**——可证层
    （``preflight.check_segment_video``）按帧数档表判「交回来的是不是本段、用没用错时长
    档」，96 帧的「假 4s」落不进任何档位、只报「无法确证档位」，那两条 AC 就测不出来
    （issue #55 的用例 docstring 记着这个坑）。档位外的秒数（如 10/11s）按 ``fps`` 折算，
    落进「区间内但不在帧数表」的 warning 档。
    """
    from minimax_h3_prompt.tools.frame_match import video_meta
    from minimax_h3_prompt.tools.preflight import EXPECTED_VIDEO_SIZE, H3_TIER_FRAMES

    frames = H3_TIER_FRAMES.get(int(round(seconds)), int(round(seconds * fps)))
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                             EXPECTED_VIDEO_SIZE)
    assert writer.isOpened(), "测试环境缺 MJPG 编码器"
    image = np.full((EXPECTED_VIDEO_SIZE[1], EXPECTED_VIDEO_SIZE[0], 3), 128, np.uint8)
    for i in range(frames):
        image[:] = 128 + (i % 8)
        writer.write(image)
    writer.release()
    actual = video_meta(path).get("duration_s", 0.0)
    assert abs(actual - frames / fps) < 0.05, f"测试视频时长不可控：{actual}"
    return path


@pytest.fixture
def tmp_video(tmp_path: Path):
    """返回一个可调用对象：make(name="a.mp4", **kwargs) -> Path"""
    def make(name: str = "clip.mp4", **kwargs) -> Path:
        return write_test_video(tmp_path / name, **kwargs)
    return make


@pytest.fixture
def h3_tier_video(tmp_path: Path):
    """返回一个可调用对象：make(seconds, name="seg.avi") -> Path（H3 档位帧数的真视频）。"""
    def make(seconds: float, name: str = "clip.avi") -> Path:
        return write_h3_tier_video(tmp_path / name, seconds)
    return make
