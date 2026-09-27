"""主题忠实度守卫：用主题中的可判定地点约束校验最终视频提示词。

背景：视频提示词由链上中间产物组装，主题（如「在酒馆喝酒」）经过多层转述后
可能漂移成无关场景（如野外打斗）。本模块从主题提取少量可确定校验的地点约束
（酒馆→tavern/inn 等），对提示词描述正文做确定性检查；未知地点不臆测。
与 FL2VA 首尾帧校验（generation.validate_fl2va_bundle）共享同一词表，
保证视频正文与首尾帧画面服从同一地点约束。
"""
from __future__ import annotations

import re

from .h3_validator import ValidationIssue

_SCENE_REQUIREMENT_MAP = (
    (("酒馆", "酒吧", "客栈", "tavern", "inn", "alehouse"), ("tavern", "inn", "alehouse", "酒馆", "酒吧", "客栈")),
    (("室内", "屋内", "室内场景", "indoor", "interior"), ("indoor", "interior", "inside", "室内", "屋内")),
    (("森林", "树林", "forest", "woodland"), ("forest", "woodland", "森林", "树林")),
    (("街道", "街上", "street", "road"), ("street", "road", "街道", "街上")),
    (("海边", "海滩", "beach", "seaside", "coast"), ("beach", "seaside", "coast", "海边", "海滩")),
)

_CONFLICTING_OUTDOOR_TERMS = ("forest clearing", "open field", "wilderness", "outdoor camp")

_DESCRIPTION_HEADER = {
    "base": "integrated_multimodal_description",
    "ref": "detailed_description",
}
_NEXT_SECTION_RE = re.compile(
    r"^(?:overall_soundscape|non_diegetic_music|subject_definitions|summary|retention_analysis)\s*[:：]",
    re.MULTILINE,
)


def scene_requirement_groups(plot: str) -> tuple[tuple[str, ...], ...]:
    """从主题提取少量可确定校验的地点约束；未知地点不臆测。"""
    text = str(plot or "").lower()
    groups: list[tuple[str, ...]] = []
    for source_terms, required_terms in _SCENE_REQUIREMENT_MAP:
        if any(term.lower() in text for term in source_terms):
            groups.append(required_terms)
    return tuple(groups)


def contains_unqualified_conflict(text: str, term: str) -> bool:
    """野外冲突词出现时若周围无『窗外/透过/背景』等限定词，视为场景漂移。"""
    lower = text.lower()
    start = 0
    while True:
        index = lower.find(term, start)
        if index < 0:
            return False
        context = lower[max(0, index - 60): index + len(term) + 60]
        if not any(marker in context for marker in ("window", "through", "outside", "beyond", "seen from")):
            return True
        start = index + len(term)


def description_body(prompt: str, mode: str) -> str:
    """提取提示词的描述正文段（base: integrated_multimodal_description / ref: detailed_description）。"""
    header = _DESCRIPTION_HEADER.get(mode, _DESCRIPTION_HEADER["base"])
    m = re.search(rf"^{re.escape(header)}\s*[:：]", prompt, re.MULTILINE)
    if not m:
        return prompt
    rest = prompt[m.end():]
    stop = _NEXT_SECTION_RE.search(rest)
    return rest[: stop.start()] if stop else rest


def theme_fidelity_issues(
    prompt: str,
    *,
    plot: str,
    mode: str,
    extra_groups: tuple[tuple[str, ...], ...] | list[list[str]] = (),
) -> list[ValidationIssue]:
    """校验视频提示词正文是否守住主题地点约束；error 级问题应触发有界修复。"""
    issues: list[ValidationIssue] = []
    body = description_body(prompt, mode)
    groups = list(scene_requirement_groups(plot))
    groups.extend(tuple(str(term) for term in group) for group in extra_groups if group)
    for group in groups:
        if not any(term.lower() in body.lower() for term in group):
            issues.append(ValidationIssue(
                "error", "THEME_SCENE_MISSING",
                f"主题要求的场景词（{'/'.join(group)}）未出现在描述正文——视频会偏离用户主题",
            ))
    if groups:
        for term in _CONFLICTING_OUTDOOR_TERMS:
            if contains_unqualified_conflict(body, term):
                issues.append(ValidationIssue(
                    "error", "THEME_SCENE_DRIFT",
                    f"描述正文出现无场景限定的『{term}』，与主题地点冲突",
                ))
    return issues


def theme_requirements_text(
    plot: str,
    extra_groups: tuple[tuple[str, ...], ...] | list[list[str]] = (),
    *,
    target: str = "正文",
) -> str:
    """把主题地点约束渲染成给修复 LLM 的明确指令文本；无约束时返回空。

    ``target``：这批词该出现在**哪儿**。整片正文用默认的「正文」；关键帧节点要的是
    「scene_anchor 或画面确实处在该地点的那一帧」——词表是从**整条**主题抽的，而一张
    关键帧只画主题里的一个时刻，写成「每帧都必须出现」就是一条不可满足的指令
    （issue #37 实测：主题前半是天宫、后半是高楼室内，I2VA 首帧画天宫却要含「室内」）。
    """
    groups = list(scene_requirement_groups(plot))
    groups.extend(tuple(str(term) for term in group) for group in extra_groups if group)
    if not groups:
        return ""
    required = "；".join("/".join(group) for group in groups)
    forbidden = "、".join(_CONFLICTING_OUTDOOR_TERMS)
    return (
        f"{target}必须出现的场景词（任一同组词即可）：{required}。"
        f"禁止出现无场景限定的：{forbidden}（仅允许作为窗外/远景背景描述）。"
    )


__all__ = [
    "scene_requirement_groups",
    "contains_unqualified_conflict",
    "description_body",
    "theme_fidelity_issues",
    "theme_requirements_text",
]
