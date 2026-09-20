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

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .frame_match import mad, read_frames_gray

STATIC_THR = 1.0        # 灰度 0–255 刻度上的平均绝对差，低于此判定"画面没动"
SANITY_THR = 0.05       # 整段中位差低于此值时，认为素材本身有问题

BASELINE_NAME = "baseline.json"
AUDIT_DIR = "audit"


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


@dataclass
class SeamResult:
    """一个接缝。"""

    index: int
    prev_shot: int
    next_shot: int
    cross: float
    prev_trailing: int
    next_leading: int
    prev_median: float

    @property
    def total_still(self) -> int:
        return self.prev_trailing + self.next_leading


@dataclass
class AuditReport:
    """一次审计的完整结果。"""

    shots: list[ShotStill]
    seams: list[SeamResult]
    duration_s: float
    still_seconds: float
    still_ratio: float
    per_seam_avg_frames: float
    warnings: list[str] = field(default_factory=list)
    merged_path: str = ""


def merge_videos(videos: list[Path], out_path: Path) -> tuple[bool, str]:
    """ffmpeg concat。先试 stream copy（无损且快），失败再重编码。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.parent / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{v.as_posix()}'" for v in videos),
                         encoding="utf-8")
    base = [str(find_ffmpeg()), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    attempts = [
        ("stream-copy", ["-c", "copy"]),
        ("re-encode", ["-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-b:a", "192k"]),
    ]
    last_error = ""
    for tag, extra in attempts:
        proc = subprocess.run(base + extra + [str(out_path)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return True, tag
        last_error = (proc.stderr or "")[-1500:]
    return False, last_error


def analyze(videos: list[Path], out_dir: Path, *, merge: bool = True) -> AuditReport:
    """分析一组**已按正片顺序排好**的视频。

    读不到的镜头进 warnings 并跳过；不静默。
    """
    warnings: list[str] = []
    usable: list[tuple[int, Path]] = []
    for index, video in enumerate(videos, 1):
        if not video.is_file():
            warnings.append(f"镜头 {index} 的文件不存在：{video}")
            continue
        usable.append((index, video))

    states: list[ShotStill] = []
    frame_lists: list[list[np.ndarray]] = []
    for shot_no, video in usable:
        frames = read_frames_gray(video)
        if not frames:
            warnings.append(f"镜头 {shot_no} 读不出帧：{video.name}")
            continue
        state, alert = analyze_shot(frames, shot_no)
        if alert:
            warnings.append(f"镜头 {shot_no}：{alert}")
        states.append(state)
        frame_lists.append(frames)

    seams: list[SeamResult] = []
    for i in range(len(states) - 1):
        # 跨缝帧差 = 前镜头最后一帧 vs 后镜头第一帧。
        # 实测同源约 3–4，若接近 0 说明两帧基本是同一张图（合并层重复）。
        cross = mad(frame_lists[i][-1], frame_lists[i + 1][0])
        seams.append(SeamResult(
            index=i + 1, prev_shot=states[i].shot, next_shot=states[i + 1].shot,
            cross=round(cross, 2), prev_trailing=states[i].trailing,
            next_leading=states[i + 1].leading, prev_median=states[i].median,
        ))

    still_frames = sum(s.total_still for s in seams)
    duration = sum(s.frames for s in states) / 24.0
    merged_path = ""
    if merge and usable:
        ok, how = merge_videos([v for _, v in usable], out_dir / "merged_naive.mp4")
        if ok:
            merged_path = str(out_dir / "merged_naive.mp4")
        else:
            warnings.append(f"合并失败：{how}")

    return AuditReport(
        shots=states, seams=seams, duration_s=round(duration, 2),
        still_seconds=round(still_frames / 24.0, 2),
        still_ratio=round(still_frames / 24.0 / duration, 4) if duration else 0.0,
        per_seam_avg_frames=round(still_frames / len(seams), 1) if seams else 0.0,
        warnings=warnings, merged_path=merged_path,
    )


def load_baseline(session_dir: Path) -> dict | None:
    path = session_dir / AUDIT_DIR / BASELINE_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def promote_baseline(session_dir: Path, report: AuditReport, label: str) -> Path:
    """把本次结果提升为基线。人工动作，不会被自动覆盖。"""
    payload = {
        "label": label,
        "promoted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "shots": len(report.shots),
        "duration_s": report.duration_s,
        "seam_still_seconds": report.still_seconds,
        "seam_still_ratio": report.still_ratio,
        "per_seam_avg_frames": report.per_seam_avg_frames,
        "per_shot_trailing": [s.trailing for s in report.shots],
    }
    path = session_dir / AUDIT_DIR / BASELINE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _delta(current: float, base: float) -> str:
    diff = current - base
    if abs(diff) < 1e-9:
        return "持平"
    arrow = "↓" if diff < 0 else "↑"
    return f"{arrow} {abs(diff):.2f}"


def compare_to_baseline(report: AuditReport, baseline: dict) -> list[tuple[str, str, str, str]]:
    """返回 ``(指标, 基线, 本次, 变化)`` 四元组。**只比均值与比例，不比总数。**"""
    rows: list[tuple[str, str, str, str]] = []
    base_avg = baseline.get("per_seam_avg_frames")
    if base_avg is not None:
        rows.append(("接缝静止 · 每缝均值（帧）", f"{base_avg:.1f}",
                     f"{report.per_seam_avg_frames:.1f}",
                     _delta(report.per_seam_avg_frames, float(base_avg))))
    base_ratio = baseline.get("seam_still_ratio")
    if base_ratio is not None:
        rows.append(("接缝静止 · 占全片", f"{base_ratio * 100:.1f}%",
                     f"{report.still_ratio * 100:.1f}%",
                     _delta(report.still_ratio, float(base_ratio))))
    return rows


def render_markdown(report: AuditReport, baseline: dict | None) -> str:
    lines: list[str] = ["# 接缝审计报告", ""]
    lines.append(f"- 镜头数：**{len(report.shots)}** ｜ 总时长：{report.duration_s:.2f}s")
    lines.append(f"- 接缝静止：**{report.still_seconds:.2f}s**，占全片 "
                 f"**{report.still_ratio * 100:.1f}%**")
    lines.append(f"- 每缝均值：**{report.per_seam_avg_frames:.1f} 帧**")
    lines.append("")
    lines.append("> ⚠️ **总数不能跨镜头数比较。** 基线是 "
                 f"{baseline.get('shots', '?') if baseline else '?'} 个镜头、"
                 f"本次是 {len(report.shots)} 个。请只看**每缝均值**与**占全片比例**。")
    lines.append("")

    if baseline:
        lines.append("## 与基线对比")
        lines.append("")
        lines.append("| 指标 | 基线 | 本次 | 变化 |")
        lines.append("|---|---|---|---|")
        for name, base, cur, delta in compare_to_baseline(report, baseline):
            lines.append(f"| {name} | {base} | {cur} | {delta} |")
        lines.append("")

    lines.append("## 每个镜头")
    lines.append("")
    lines.append("| 镜头 | 帧数 | 典型运动量 | 末尾静止 | 开头静止 | 段内最长静止 |")
    lines.append("|---|---|---|---|---|---|")
    for s in report.shots:
        lines.append(f"| {s.shot} | {s.frames} | {s.median:.2f} | **{s.trailing}** | "
                     f"**{s.leading}** | {s.longest} |")
    lines.append("")
    lines.append("> 「末尾静止」若明显大于该镜头自己的「段内最长静止」，说明段尾出现了额外冻结。")
    lines.append("")

    if report.seams:
        lines.append("## 每个接缝")
        lines.append("")
        lines.append("| 接缝 | 前镜头末尾静止 | 后镜头开头静止 | 接缝总静止 |")
        lines.append("|---|---|---|---|")
        for s in report.seams:
            lines.append(f"| {s.prev_shot}→{s.next_shot} | {s.prev_trailing} | "
                         f"{s.next_leading} | **{s.total_still}**"
                         f"（{s.total_still / 24:.2f}s） |")
        lines.append("")

    if report.warnings:
        lines.append("## 警告")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- ⚠️ {w}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "STATIC_THR",
    "SANITY_THR",
    "AUDIT_DIR",
    "BASELINE_NAME",
    "AuditReport",
    "SeamResult",
    "ShotStill",
    "analyze",
    "analyze_shot",
    "compare_to_baseline",
    "diff_curve",
    "find_ffmpeg",
    "leading_run",
    "load_baseline",
    "longest_run",
    "merge_videos",
    "promote_baseline",
    "render_markdown",
    "sanity_check",
    "trailing_run",
]
