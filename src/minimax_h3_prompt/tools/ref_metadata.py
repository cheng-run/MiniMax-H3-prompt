"""参考资产元数据工具：把 brief 里的 <Picture N> 映射整理成 agent 可用的素材。"""
from __future__ import annotations

from ..brief_parser import RefItem


def format_ref_meta(refs: list[RefItem]) -> str:
    """给 agent 看的人类可读参考资产清单（ref 模式）。"""
    if not refs:
        return "（无参考图）"
    lines = [f"- <Picture {r.picture}>：{r.name} — {r.description}" for r in sorted(refs, key=lambda x: x.picture)]
    return "\n".join(lines)


def to_tuple(refs: list[RefItem]) -> list[tuple[int, str, str]]:
    """转成 h3_validator 期望的 (picture, name, description) 元组列表。"""
    return [(r.picture, r.name, r.description) for r in refs]
