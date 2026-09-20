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


@pytest.fixture
def tmp_video(tmp_path: Path):
    """返回一个可调用对象：make(name="a.mp4", **kwargs) -> Path"""
    def make(name: str = "clip.mp4", **kwargs) -> Path:
        return write_test_video(tmp_path / name, **kwargs)
    return make
