"""组装器：对提示词工程师的产出做确定性格式保障（补缺段头）+ 终检报告。

LLM 负责内容，这里保证「段落一定齐全」这个硬约束。
"""
from __future__ import annotations

import re

from ..tools.h3_validator import BASE_SECTIONS, REF_SECTIONS

_NA_SECTIONS = {"overall_soundscape", "non_diegetic_music"}


def extract_fenced_block(text: str) -> str:
    """若模型把提示词包在 ```text/``` 代码围栏里，抽出围栏内内容（去掉前言/结尾注释）。"""
    m = re.search(r"```(?:text)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text


def assemble_and_repair(prompt: str, mode: str, variant: str = "T2VA") -> tuple[str, str]:
    """确定性格式保障：抽围栏 → 段头齐全 → 声音段规范为 N/A。返回 (修复后提示词, 说明)。"""
    notes: list[str] = []
    sections = REF_SECTIONS if mode == "ref" else BASE_SECTIONS

    # 0) 抽代码围栏（prompt_engineer 有时把提示词包在 ```text 里并附前言/结尾说明）
    fenced = extract_fenced_block(prompt)
    if fenced != prompt.strip():
        notes.append("抽取代码围栏内正文")
        prompt = fenced

    # 1) 缺失段头补齐
    existing = [h for h in sections if re.search(rf"^{re.escape(h)}\s*[:：]", prompt, re.MULTILINE)]
    missing = [h for h in sections if h not in existing]
    additions: list[str] = []
    for h in missing:
        val = "N/A" if h in _NA_SECTIONS else ""
        prompt = prompt.rstrip() + f"\n\n{h}: {val}"
        additions.append(h)
    if additions:
        notes.append(f"补齐缺失段落：{', '.join(additions)}")

    # 2) 声音段把 "None/无" 之类规范成官方 N/A
    for h in _NA_SECTIONS:
        if h not in sections:
            continue
        m = re.search(rf"^{re.escape(h)}\s*[:：]\s*(.*)$", prompt, re.MULTILINE)
        if m and m.group(1).strip().lower() in ("none", "无", "n/a", "na", ""):
            prompt = prompt[: m.start()] + f"{h}: N/A" + prompt[m.end():]
            notes.append(f"把 {h} 规范为 N/A")

    return prompt, ("；".join(notes) if notes else "段落齐全，无需修复。")
