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

import math
from dataclasses import dataclass
from pathlib import Path

from .frame_match import MAD_MAYBE, imread_unicode, mad, read_window, to_gray, video_meta
from .ledger import Ledger

EXPECTED_VIDEO_SIZE = (736, 1280)
SEGMENT_MIN_S = 4.0
SEGMENT_MAX_S = 10.0

# H3 实测产物帧数表（2026-09-23 全量探测 ComfyUI 真产物校准，736×1280@24fps）：
# 「4s」档恒产 107 帧 = 4.458s——H3 产出**从不是整数秒**，旧的「整数秒 + 0.1s 容差」
# 会把每一个真产物都误杀成硬阻断（GEN005 段 1 实测，16 个 09-22 产物帧数严格一致）。
# 档间帧距不均（4→5s 差 17 帧、5→6s 差 34 帧，相邻档产物只差 0.7s），容差近似
# 区分不了相邻档，必须按帧数精确匹配。
H3_TIER_FRAMES = {4: 107, 5: 124, 6: 158}

# 帧数匹配容差：16 个实测产物帧数与档位严格一致，但解码器数帧可能差一两帧。
# ±2 仍远小于最近档距（17 帧），不影响分辨相邻档。
FRAME_COUNT_TOLERANCE = 2


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


def check_segment_video(video_path: Path, expected_seconds: float | None = None) -> list[Issue]:
    """校验本段交上来的输出视频：读得到、帧数落在 H3 实测档位、且与本段计划时长一致。

    分段的交接物是「本段输出视频 + 由它剥出的锚帧」这一对。视频给错（拿错段、用了
    别的时长档、被插帧改过时长），下游整条接错，所以要在剥帧当轮判——这是**可证**的
    事实，错了就是错了，故报 ``error``。

    判据是**实测帧数表**（``H3_TIER_FRAMES``，2026-09-23 对 16 个真产物校准），不是
    「整数秒 + 容差」：H3 产出从不是整数秒（4s 档恒为 4.458s，档间帧距不均，容差
    区分不了相邻档）。实测档位外的 7–10s 档暂无样本，首次遇到只警告不阻断——
    测不出事实就不许阻断，攒到样本再固化。

    为什么不能拿 ``check_input_frame`` 当这一档用：向导的锚帧是**当场从这段视频剥出来
    的**，而那个函数比的正是**同一段视频**的尾窗（窗口最小 mad 必然 < ``MAD_MAYBE``），
    在这个调用点恒真。它的 MAD 档只在「用户回报实际投喂的那张图」时才有牙齿。

    尺寸不符只报 ``warning``：H3 输出 736×1280 是我们的假设，用户可以改工作流。
    """
    issues: list[Issue] = []
    meta = video_meta(video_path)
    seconds = float(meta.get("duration_s") or 0.0)
    if not math.isfinite(seconds) or not seconds:
        # 测不出事实就不许阻断——这不是「视频错了」，是「我们量不出来」。
        # isfinite 也挡住 NaN：fps 报 NaN 时 duration 是 NaN，round(nan) 会抛 ValueError，
        # 而调用点在向导的剥帧 try 之外，异常逃逸会直接崩掉陪跑流程。
        issues.append(Issue("warning", "segment_video_unmeasurable",
                            f"读不到本段视频的时长元数据：{video_path}（跳过时长校验）"))
    else:
        frames = meta.get("frames")
        tier = None
        if isinstance(frames, (int, float)) and frames > 0:
            for candidate in sorted(H3_TIER_FRAMES):
                if abs(frames - H3_TIER_FRAMES[candidate]) <= FRAME_COUNT_TOLERANCE:
                    tier = candidate
                    break
        if tier is not None:
            # 帧数落在已知档位：以档位标称秒数判与本段计划是否一致
            if expected_seconds is not None and tier != round(expected_seconds):
                issues.append(Issue(
                    "error", "segment_video_duration_mismatch",
                    f"本段视频 {frames} 帧对应 H3 的 {tier}s 时长档，与本段计划时长 "
                    f"{expected_seconds:g}s 不符——可能交错了段，或用错了时长档"))
        elif seconds < SEGMENT_MIN_S or seconds > SEGMENT_MAX_S:
            issues.append(Issue("error", "duration_out_of_range",
                                f"段时长 {seconds:.2f}s 超出 H3 可选区间 "
                                f"{SEGMENT_MIN_S:.0f}–{SEGMENT_MAX_S:.0f}s"))
        elif expected_seconds is not None:
            # 4–10s 区间内但不在帧数表（7–10s 档暂无样本）：无法确证档位，只警告
            issues.append(Issue(
                "warning", "segment_video_duration_unknown_tier",
                f"本段视频 {frames} 帧 ≈{seconds:.2f}s 不在 H3 实测帧数表 "
                f"{max(H3_TIER_FRAMES)}s 档内（7–10s 档暂无样本），无法确证时长档，"
                f"请人工核对是否本段计划 {expected_seconds:g}s 的产出"))
    if meta:
        size = (int(meta.get("width") or 0), int(meta.get("height") or 0))
        if size != EXPECTED_VIDEO_SIZE:
            issues.append(Issue(
                "warning", "segment_video_size",
                f"本段视频 {size[0]}×{size[1]} 与 H3 输出 "
                f"{EXPECTED_VIDEO_SIZE[0]}×{EXPECTED_VIDEO_SIZE[1]} 不一致，可能不是 H3 这段的产出"))
    return issues


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
