"""提交前预检：把错误拦在昂贵的生成之前。

背景：一个镜头要跑约 10 分钟。若输入帧绑错（2026-09-20 就发生过——一次
重跑误用了上一段的**首帧**而不是尾帧，导致接缝硬跳切），要等 10 分钟才
发现，而且事后无法从磁盘还原。

规则分三档，按**可证性**分级（借鉴 guardrails 的 on_fail 语义，但不引入依赖）：

- 可证规则 → ``error``，阻断。都是能算出来的事实，错了就是错了
- 启发式规则 → ``warning``，不阻断。可能误报
- 资源规则 → ``refrain``，劝阻但由人决定

启发式为什么只能是 warning：2026-09-18 实测里，``end_hook`` 写「定格」的
镜头 5 末尾静止为 0，而写「停在」的镜头 1/3 分别是 11/29。冻结词是相关，
不是因果。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .frame_match import MAD_MAYBE, imread_unicode, mad, read_window, to_gray
from .ledger import Ledger

EXPECTED_VIDEO_SIZE = (1280, 736)
SEGMENT_MIN_S = 4.0
SEGMENT_MAX_S = 10.0


@dataclass
class Issue:
    """一条预检结论。``severity`` 取 "error" | "warning" | "refrain"。"""

    severity: str
    code: str
    message: str


def check_input_frame(frame_path: Path, prev_video: Path | None) -> list[Issue]:
    """校验待提交的输入帧：存在、来自上一段尾帧、尺寸正确。"""
    issues: list[Issue] = []

    image = imread_unicode(frame_path)
    if image is None:
        issues.append(Issue("error", "bridge_frame_missing",
                            f"输入帧读不到或不存在：{frame_path}"))
        return issues

    height, width = image.shape[:2]
    if (width, height) != EXPECTED_VIDEO_SIZE:
        issues.append(Issue(
            "warning", "bridge_frame_size",
            f"输入帧尺寸 {width}×{height} 与 H3 输出 {EXPECTED_VIDEO_SIZE[0]}×"
            f"{EXPECTED_VIDEO_SIZE[1]} 不一致，可能不是从上一段视频抽出的帧",
        ))

    if prev_video is None:
        issues.append(Issue("warning", "prev_video_unknown",
                            "没有提供上一段视频，无法校验输入帧来源"))
        return issues

    tail = read_window(prev_video, window="tail")
    if not tail:
        issues.append(Issue("warning", "prev_video_unreadable",
                            f"上一段视频读不到：{prev_video}"))
        return issues

    probe = to_gray(image)
    best = min(mad(probe, frame) for frame in tail)
    if best >= MAD_MAYBE:
        issues.append(Issue(
            "error", "bridge_frame_mismatch",
            f"输入帧与上一段尾帧对不上（mad={best:.2f} ≥ {MAD_MAYBE}）。"
            f"请确认绑的是 {prev_video.name} 的**尾帧**，不是首帧或其他段的帧",
        ))
    return issues


def check_shot_duration(seconds: float) -> list[Issue]:
    """段时长必须落在 ComfyUI 的 H3 整数档 4–10 秒内。"""
    if seconds < SEGMENT_MIN_S or seconds > SEGMENT_MAX_S:
        return [Issue("error", "duration_out_of_range",
                      f"段时长 {seconds}s 超出 H3 可选区间 "
                      f"{SEGMENT_MIN_S:.0f}–{SEGMENT_MAX_S:.0f}s")]
    if abs(seconds - round(seconds)) > 1e-6:
        return [Issue("error", "duration_not_integer",
                      f"段时长 {seconds}s 不是整数秒，H3 只接受整数档")]
    return []


def check_not_discarded(prev_video_name: str, ledger: Ledger) -> list[Issue]:
    """上一段不能是被判定为废弃的视频。"""
    for item in ledger.discarded:
        if item.video == prev_video_name:
            replaced = f"，应改用 {item.replaced_by}" if item.replaced_by else ""
            return [Issue("error", "prev_video_discarded",
                          f"上一段 {prev_video_name} 是废弃版本（{item.reason}）"
                          f"{replaced}")]
    return []
