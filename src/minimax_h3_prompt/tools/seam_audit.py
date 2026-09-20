"""接缝审计：拼接镜头并量化接缝处的静止。

背景：本项目用「尾帧接力」——镜头 N+1 的首帧由镜头 N 的尾帧抽出后喂入。
若镜头 N 的 ``end_hook`` 写了「停住/定格」这类词，H3 会在段尾把画面冻住，
而镜头 N+1 又从同一张冻住的画面开始 → 接缝处出现连续静止（"双静止停顿"）。

2026-09-18《古老图书馆》修复前实测：4 个接缝累计约 4.04 秒连续静止，
占全片 28.67 秒的 14.1%；每段的「末尾静止」恰好等于它自己的「段内最长静止」，
说明冻结就发生在段尾。

本模块用**逐帧像素差**判定静止——确定性、零成本、可重复，
不需要 LLM 评委。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .frame_match import mad, read_frames_gray

STATIC_THR = 1.0        # 灰度 0–255 刻度上的平均绝对差，低于此判定"画面没动"
SANITY_THR = 0.05       # 整段中位差低于此值时，认为素材本身有问题


@dataclass
class ShotStill:
    """单个镜头的静止统计。"""

    shot: int
    frames: int
    median: float
    trailing: int
    leading: int
    longest: int


def find_ffmpeg() -> Path:
    """定位 ffmpeg 可执行文件。优先用 imageio-ffmpeg 自带的，其次 PATH。

    找不到时抛 RuntimeError，绝不静默降级——合并失败必须让人看见。
    """
    try:
        import imageio_ffmpeg
    except ImportError:
        imageio_ffmpeg = None  # type: ignore[assignment]
    if imageio_ffmpeg is not None:
        try:
            return Path(imageio_ffmpeg.get_ffmpeg_exe())
        except (OSError, RuntimeError):
            # 自带二进制缺失/不可执行时退回 PATH，不算静默失败：
            # 真正找不到时下面会抛 RuntimeError
            pass
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise RuntimeError(
        "找不到 ffmpeg。请执行 `uv add imageio-ffmpeg`，或把 ffmpeg 放进 PATH。"
    )


def diff_curve(frames: list[np.ndarray]) -> list[float]:
    """相邻帧的平均绝对差序列。"""
    return [mad(frames[i + 1], frames[i]) for i in range(len(frames) - 1)]


def trailing_run(diffs: list[float], thr: float = STATIC_THR) -> int:
    """末尾连续"没动"的次数。k 次 → 末尾 k+1 帧是同一画面。"""
    count = 0
    for d in reversed(diffs):
        if d < thr:
            count += 1
        else:
            break
    return count


def leading_run(diffs: list[float], thr: float = STATIC_THR) -> int:
    """开头连续"没动"的次数。k 次 → 开头 k+1 帧是同一画面。"""
    count = 0
    for d in diffs:
        if d < thr:
            count += 1
        else:
            break
    return count


def longest_run(diffs: list[float], thr: float = STATIC_THR) -> int:
    """整段里最长的连续"没动"次数。"""
    best = current = 0
    for d in diffs:
        current = current + 1 if d < thr else 0
        best = max(best, current)
    return best


def sanity_check(diffs: list[float]) -> str | None:
    """先验证素材本身是好的：整段几乎不动时，问题在素材而不在接缝。"""
    if not diffs:
        return "这段视频没读出帧，无法分析"
    if float(np.median(diffs)) < SANITY_THR:
        return "整段几乎完全静止——素材本身可能有问题，不要据此判断接缝"
    return None


def analyze_shot(frames: list[np.ndarray], shot: int) -> tuple[ShotStill, str | None]:
    """算一个镜头的静止统计，并返回素材自检告警（无问题为 None）。"""
    diffs = diff_curve(frames)
    return (
        ShotStill(shot=shot, frames=len(frames),
                  median=round(float(np.median(diffs)), 2) if diffs else 0.0,
                  trailing=trailing_run(diffs), leading=leading_run(diffs),
                  longest=longest_run(diffs)),
        sanity_check(diffs),
    )


__all__ = [
    "STATIC_THR",
    "SANITY_THR",
    "ShotStill",
    "analyze_shot",
    "diff_curve",
    "find_ffmpeg",
    "leading_run",
    "longest_run",
    "sanity_check",
    "trailing_run",
]
