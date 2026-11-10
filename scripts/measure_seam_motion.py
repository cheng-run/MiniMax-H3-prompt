#!/usr/bin/env python
"""段尾运动量量尺（#198）：把「接缝像一次停顿」变成可对照的数字。

只读 mp4，不进产品路径。判据来自 #191 的验收线：

- 每段**段尾窗口**（默认 6 帧 = 0.25s）里，低于本段中位运动量 50% 的帧数 ≤6；
- 段尾不能「静止之后硬跳」：段尾中位运动量不低于相邻段内中位运动量的 50%；
- 全片累计低于本段中位 50% 的帧 ≤1.5s。

为什么要按段而不是按全片：实测的缺陷是**段级**的——段尾 0.2–2s 冻结，全片平均会被
其它段的正常运动盖掉。段边界由调用方显式给出（导演台 plan.json 的 export 帧是唯一
真源），本脚本不猜分段。

用法：

    python scripts/measure_seam_motion.py video.mp4 --boundaries 107,248,452,...
    python scripts/measure_seam_motion.py video.mp4 --boundaries-file bounds.txt --json

``--boundaries`` 是**累计结束帧**（不含）：段 1 = [0, b1)，段 2 = [b1, b2) …
最后一段自动延伸到视频末帧。帧号与导演台 ``plan.json`` 的 ``export_frames`` 前缀和
同号；文件里第一段从 0 起，别塞 0 进去。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

# 量尺刻意不 import 产品代码（``test_measure_seam_motion`` 钉了这条：它只读 mp4），
# 所以帧率是独立常量，不与 ``director.frames.FPS`` 联动——两边都是 24，但改一边
# 不该悄悄改变这条脚本的读数。
FPS = 24.0
DEFAULT_TAIL_FRAMES = 6          # 0.25s @ 24fps（#191 验收线）
DEFAULT_RATIO = 0.5              # 低于本段中位运动量的 50% 记为「停」
DEFAULT_WORK_WIDTH = 320         # 只为降采样提速；运动量是相对比较，不需要原分辨率
DEFAULT_WHOLE_BUDGET_S = 1.5     # 全片累计静止帧上限


def read_motion(video: Path, work_width: int = DEFAULT_WORK_WIDTH) -> np.ndarray:
    """逐帧运动量：相邻两帧灰度图的平均绝对差。

    返回长度 = 帧数 - 1（第 i 个元素是第 i 与 i+1 帧之间的差）。第一帧没有「上一帧」，
    所以索引与帧号的对应关系是 ``motion[i] = frame[i+1] - frame[i]``。
    """
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise SystemExit(f"[错误] 打不开视频：{video}")
    motions: list[float] = []
    previous: np.ndarray | None = None
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if work_width and gray.shape[1] > work_width:
                scale = work_width / gray.shape[1]
                gray = cv2.resize(gray, (work_width, max(1, round(gray.shape[0] * scale))),
                                  interpolation=cv2.INTER_AREA)
            current = gray.astype(np.float32)
            if previous is not None:
                motions.append(float(np.mean(np.abs(current - previous))))
            previous = current
    finally:
        capture.release()
    if not motions:
        raise SystemExit(f"[错误] 视频不足两帧，无法计算运动量：{video}")
    return np.asarray(motions, dtype=np.float64)


def _segment_bounds(boundaries: list[int], frame_count: int) -> list[tuple[int, int]]:
    """累计结束帧 → [(start, end), ...]；最后一段延伸到 frame_count。"""
    starts = [0, *boundaries]
    ends = [*boundaries, frame_count]
    return [(start, end) for start, end in zip(starts, ends) if end > start]


def measure(motion: np.ndarray, bounds: list[tuple[int, int]], *,
            tail_frames: int = DEFAULT_TAIL_FRAMES,
            ratio: float = DEFAULT_RATIO) -> dict:
    """按段统计段尾冻结与跨段硬跳。"""
    # motion[i] 对应帧 i+1：段 [start, end) 的内部运动量是 motion[start:end-1]。
    segments: list[dict] = []
    medians: list[float] = []
    for index, (start, end) in enumerate(bounds, 1):
        inner = motion[max(0, start):max(0, end - 1)]
        if inner.size == 0:
            inner = motion[max(0, start):max(0, end)]
        median = float(np.median(inner)) if inner.size else 0.0
        medians.append(median)
        tail = inner[-tail_frames:] if inner.size else inner
        threshold = median * ratio
        still = int(np.sum(tail < threshold)) if tail.size else 0
        tail_median = float(np.median(tail)) if tail.size else 0.0
        segments.append({
            "index": index,
            "start_frame": start,
            "end_frame": end,
            "frames": end - start,
            "median_motion": round(median, 4),
            "tail_frames": int(tail.size),
            "tail_median_motion": round(tail_median, 4),
            "tail_below_ratio": still,
            "tail_ratio": round(tail_median / median, 4) if median > 0 else None,
        })

    # 跨段硬跳：接缝后第一段的起点运动量 vs 前一/后一段的中位运动量。
    for position, segment in enumerate(segments):
        previous = segments[position - 1] if position > 0 else None
        following = segments[position + 1] if position + 1 < len(segments) else None
        neighbors = [item["median_motion"] for item in (previous, following) if item]
        segment["neighbor_median_motion"] = (
            round(float(np.median(neighbors)), 4) if neighbors else None)
        segment["tail_vs_neighbor"] = (
            round(segment["tail_median_motion"] / float(np.median(neighbors)), 4)
            if neighbors and float(np.median(neighbors)) > 0 else None)

    total_frames = int(len(motion) + 1)
    still_frames = sum(item["tail_below_ratio"] for item in segments)
    # 全片口径独立于段尾窗口：整段里低于本段中位 50% 的帧都算（不只段尾）。
    whole_still = 0
    for (start, end), segment in zip(bounds, segments):
        inner = motion[max(0, start):max(0, end - 1)]
        if inner.size and segment["median_motion"] > 0:
            whole_still += int(np.sum(inner < segment["median_motion"] * ratio))

    return {
        "fps": FPS,
        "frames": total_frames,
        "seconds": round(total_frames / FPS, 3),
        "tail_frames": tail_frames,
        "ratio": ratio,
        "segments": segments,
        "tail_still_frames": still_frames,
        "tail_still_seconds": round(still_frames / FPS, 3),
        "whole_still_frames": whole_still,
        "whole_still_seconds": round(whole_still / FPS, 3),
        "whole_budget_seconds": DEFAULT_WHOLE_BUDGET_S,
    }


def _parse_boundaries(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if values != sorted(set(values)) or any(value <= 0 for value in values):
        raise SystemExit("[错误] --boundaries 必须是严格递增的正整数（累计结束帧）")
    return values


def render(result: dict) -> str:
    lines = [
        f"视频：{result['frames']} 帧 / {result['seconds']}s / {result['fps']:g}fps"
        f"（段尾窗口 {result['tail_frames']} 帧，阈值 {result['ratio']:.0%}）",
        "",
        "段号  帧范围        中位运动  段尾中位  段尾低于阈值  段尾/邻段",
    ]
    for item in result["segments"]:
        neighbor = item["tail_vs_neighbor"]
        lines.append(
            f"{item['index']:>3}  {item['start_frame']:>5}-{item['end_frame']:<5} "
            f"{item['median_motion']:>8.3f}  {item['tail_median_motion']:>8.3f} "
            f"{item['tail_below_ratio']:>10}/{item['tail_frames']:<3} "
            f"{'—' if neighbor is None else f'{neighbor:.2f}'}")
    lines += [
        "",
        f"段尾累计低于阈值：{result['tail_still_frames']} 帧 "
        f"（{result['tail_still_seconds']}s）",
        f"全片累计低于本段中位 50%：{result['whole_still_frames']} 帧 "
        f"（{result['whole_still_seconds']}s，预算 {result['whole_budget_seconds']}s）",
    ]
    return "\n".join(lines)


def main() -> int:
    # Windows 控制台默认 cp1252：中文一 print 就 UnicodeEncodeError，整个量尺以崩溃
    # 收场（engineering-cadence 的同一道兜底，工作流的 PYTHONIOENCODING 管不到本机）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="mp4 路径（只读）")
    parser.add_argument("--boundaries", default="",
                        help="累计结束帧，逗号分隔（如 107,248,452）")
    parser.add_argument("--boundaries-file", type=Path, default=None,
                        help="从文件读累计结束帧（每行一个或用逗号分隔）")
    parser.add_argument("--tail-frames", type=int, default=DEFAULT_TAIL_FRAMES)
    parser.add_argument("--ratio", type=float, default=DEFAULT_RATIO)
    parser.add_argument("--work-width", type=int, default=DEFAULT_WORK_WIDTH)
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器对照用）")
    args = parser.parse_args()

    motion = read_motion(args.video, work_width=args.work_width)
    raw = args.boundaries
    if args.boundaries_file:
        raw = args.boundaries_file.read_text(encoding="utf-8").replace("\n", ",")
    boundaries = _parse_boundaries(raw) if raw.strip() else []
    bounds = _segment_bounds(boundaries, len(motion) + 1)
    result = measure(motion, bounds, tail_frames=args.tail_frames, ratio=args.ratio)
    result["video"] = str(args.video)
    result["boundaries"] = boundaries

    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
