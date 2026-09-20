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


# 这些词让 H3 在段尾把画面冻住；下一段又从同一张冻住的画面长出来，
# 于是接缝处出现连续静止。2026-09-18 实测：镜头 1/3 的 end_hook 含「停在」，
# 末尾静止 11/29 帧；镜头 5 含「定格」却为 0——所以只是相关，不是因果，
# 只能给 warning。
FROZEN_WORDS: tuple[str, ...] = ("停住", "定格", "静止", "不再变化", "停在")

_DEFROST_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("停在", "刚落在"),
    ("停住", "刚收住"),
    ("定格", "姿态落定"),
    ("静止", "动作收势"),
    ("不再变化", "保持微动"),
)


def find_frozen_words(text: str) -> list[str]:
    """返回文本里命中的冻结词（去重，保持 FROZEN_WORDS 的顺序）。"""
    return [word for word in FROZEN_WORDS if word in text]


def suggest_defrost(text: str) -> str:
    """给出确定性改写建议：把"停住"类词换成"动作刚落定的姿态"。

    只做字符串替换，不改语义结构；产出需人工确认后才使用。
    """
    result = text
    for frozen, alive in _DEFROST_REPLACEMENTS:
        result = result.replace(frozen, alive)
    return result


def check_frozen_words(text: str) -> list[Issue]:
    """启发式规则：命中冻结词只提示，不阻断。"""
    hits = find_frozen_words(text)
    if not hits:
        return []
    return [Issue(
        "warning", "frozen_word",
        f"命中冻结词 {', '.join(hits)}——可能造成段尾冻结。"
        f"建议改写为：{suggest_defrost(text)[:120]}",
    )]


def estimate_remaining_shots(remaining_tokens: int, tokens_per_shot: int) -> int:
    """按单段实测消耗估算还能跑几段。"""
    if tokens_per_shot <= 0:
        return 0
    return max(0, remaining_tokens // tokens_per_shot)


def check_budget(remaining_tokens: int, tokens_per_shot: int,
                 remaining_shots: int) -> list[Issue]:
    """资源规则：剩余额度不够跑完时劝阻（不阻断，由人决定）。"""
    affordable = estimate_remaining_shots(remaining_tokens, tokens_per_shot)
    if affordable >= remaining_shots:
        return []
    return [Issue(
        "refrain", "budget_short",
        f"剩余额度按当前消耗还能跑 {affordable} 段，但计划还剩 {remaining_shots} 段。"
        f"2026-09-18 曾因百炼免费额度耗尽导致第 6 段中断",
    )]
