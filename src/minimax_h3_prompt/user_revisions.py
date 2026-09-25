"""用户修订：阶段 1 首帧环节人提的意见真源，以及每轮重出的台账。

**为什么要有独立真源**：意见原本拼在 ``brief.plot`` 上走私，而 ``plot`` 同时喂给
主题地点守卫（**按子串**提取地点硬约束）与生图常识判官（「不得新增主题中不存在的
主体、地点或道具」的判据）——散文意见塞进去等于让意见文字随机改写两条闸门的判据，
且有否定句反噬（用户说「不要森林，改到庭院」会让守卫反而开始要求正文出现「森林」）。

**为什么按轮累积**（而不是像自动质检问题那样每轮替换）：人提的意见是一次性说出口的
**断言**，静默丢掉是用户看不见的信息丢失；机检问题是**对当前产物**的判断，产物一变
判断就该重算。两条同形循环的语义分叉见 ``docs/adr/0005``。术语见 ``CONTEXT.md``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

# 修订台账文件名（``<generation_dir>/frame-rounds.jsonl``，CONTEXT.md「修订台账」）。
FRAME_ROUNDS_FILENAME = "frame-rounds.jsonl"

# 层次＝重跑起点（CONTEXT.md「用户修订」）。本模块只定义**画面级**：它对应「只重跑
# 首帧提示词节点」，是本票落地的唯一一档。设定级三档（人物设定／美术场景道具／
# 剧情分镜）各自要回灌产物并重跑下游，由 T6 连同重跑路由一起加——先占位一个没有
# 消费者的取值，正是本项目「写了没人读」那一族缺陷的形态。
LAYER_FRAME = "frame"
LAYER_LABELS = {LAYER_FRAME: "只改画面"}


def _entry_round(entry: Any) -> int:
    """条目里的轮次号；缺失或不合法都报错——静默归零会让后续轮次与台账行号重复。"""
    if not isinstance(entry, dict) or "round" not in entry:
        raise ValueError(f"用户修订条目缺少轮次：{entry!r}")
    try:
        return int(entry["round"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"用户修订条目的轮次不是整数：{entry!r}") from exc


def _checked(entry: Any) -> dict[str, Any]:
    """校验清单条目（对象、有内容、有轮次）。

    不合法就报错而**不是静默跳过**：静默丢掉一条修订，正是本模块存在的理由所要消灭的
    那种故障（用户说了、系统没听见、用户看不见）。条目只由 ``record_user_revision``
    写入，真读到坏条目意味着状态被手工改过或损坏，此时声张比容忍安全。
    """
    if not isinstance(entry, dict):
        raise ValueError(f"用户修订条目必须是对象：{entry!r}")
    if not str(entry.get("text", "")).strip():
        raise ValueError(f"用户修订条目没有内容：{entry!r}")
    _entry_round(entry)
    return entry


def _layer_label(layer: Any) -> str:
    """层次的展示标签；未登记的取值原样透出（T6 新增档位时不必先改这里）。"""
    value = str(layer or "")
    return LAYER_LABELS.get(value, value) or "未标层次"


def next_round(state: dict[str, Any]) -> int:
    """下一轮重出的轮次号（**1 起**；初始产物不算一轮重出，它记在会话状态里）。

    只增不减：按编号回收任意一条（T2）之后也必须继续往上走——若按「清单长度 + 1」
    推，撤掉一条就会重发一个用过的轮次，台账里出现两行 ``round`` 相同，事后无法按轮
    对齐。状态里没有计数器时（旧会话、手工改过的 state）从清单里的最大轮次自愈。
    """
    recorded = max((_entry_round(entry) for entry in state.get("user_revisions") or []), default=0)
    return max(int(state.get("frame_round") or 0), recorded) + 1


def record_user_revision(state: dict[str, Any], *, layer: str, text: str) -> dict[str, Any]:
    """把一条意见追加进累积清单，返回写入的条目（就地改 state）。

    空意见是调用方的 bug：静默写入会让台账多出一条没有内容的轮次，所以直接报错。
    """
    content = str(text).strip()
    if not content:
        raise ValueError("用户修订不能为空")
    entry = {"round": next_round(state), "layer": str(layer), "text": content}
    state["user_revisions"] = [*(state.get("user_revisions") or []), entry]
    state["frame_round"] = entry["round"]
    return entry


def active_revisions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """当前活动清单的**快照**（副本；台账与展示都取它，不递内部对象出去）。"""
    return [dict(_checked(item)) for item in state.get("user_revisions") or []]


def render_revision_block(revisions: Iterable[dict[str, Any]] | None) -> str:
    """把累积清单渲染成注入块；空清单返回空串（调用方用 ``_ctx``，空值自动跳过）。

    编号取**当前活动清单**的位置而不是提出时的轮次：按编号回收任意一条时，编号与
    清单一一对应，回收后不必重排台账。
    """
    entries = [_checked(entry) for entry in revisions or []]
    if not entries:
        return ""
    lines = [
        f"{index}. [{_layer_label(entry.get('layer'))}] {str(entry['text']).strip()}"
        for index, entry in enumerate(entries, 1)
    ]
    lines.append("（以上为用户累积提出的修订：每一条都必须落实，不是只落实最后一条。）")
    return "\n".join(lines)


def log_frame_round(directory: str | Path, *, round_index: int, layer: str,
                    feedback_this_round: str, active_revisions: list[dict[str, Any]],
                    bundle: dict[str, Any] | None, base_used: bool) -> Path:
    """追加一行修订台账（``<generation_dir>/frame-rounds.jsonl``）。

    ``active_revisions`` 取**当轮活动清单的全量快照**而非增量：于是「某条修订在第 N 轮
    被撤掉」可事后 diff 相邻两轮看出来，撤销本身不必单开一列。
    ``base_used`` 是日后判定「累积到底生效没有」的分组变量——缺它就无法把「上了上一版
    做基线」与「纯重掷」的轮次分开看（基线本身由 T3 落地，此前恒为 False）。
    """
    record = {
        "round": int(round_index),
        "layer": str(layer),
        "layer_label": _layer_label(layer),
        "feedback_this_round": str(feedback_this_round),
        "active_revisions": [dict(_checked(item)) for item in active_revisions],
        "bundle": bundle,
        "base_used": bool(base_used),
    }
    path = Path(directory) / FRAME_ROUNDS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


__all__ = [
    "FRAME_ROUNDS_FILENAME", "LAYER_FRAME", "LAYER_LABELS",
    "active_revisions", "log_frame_round", "next_round",
    "record_user_revision", "render_revision_block",
]
