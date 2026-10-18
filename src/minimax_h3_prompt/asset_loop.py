"""素材循环的领域件（issue #50）：队列排序、改写轮次留痕、单条提示词改写。

素材循环把多参路线的资产图生产从「一次性全量出词」改成**逐张闭环**
（CONTEXT.md「素材循环」）：出一张提示词 → 审查（填路径前就能打回）→ 交回 →
读图校对 → 裁决 → 下一张。本模块只放不碰终端的纯件；问答节奏住在
``ui.wizard`` 的 ``_asset_loop``。

改写留痕走 ``<generation_dir>/asset-rounds.jsonl`` **追加式轮次记录**——沿用
修订台账（frame-rounds.jsonl）的追加式快照思路：只追加不改写，历史可查。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Sequence

# 追加式轮次记录文件名（issue #50 AC2/AC3「留痕」）。放生成目录下，与
# frame-rounds.jsonl 同址；两者并存——帧的修订台账管首尾帧提示词重出，
# 这本管资产图提示词的改写轮次，混一本会让「一轮」指代两种节奏。
ASSET_ROUNDS_FILENAME = "asset-rounds.jsonl"

# 队列默认顺序（issue #50 AC4）：人物→场景→道具。**不是** KIND_LABELS 的
# 枚举序（人物/道具/场景）——那是渲染标题的顺序；这里是出题节奏的裁定。
_KIND_QUEUE_ORDER = {"character": 0, "scene": 1, "prop": 2}

# 留痕触发口（每行 round 记录的 trigger）：
# - 审查打回「改写提示词」（AC2）；
# - 校对后重生成「改词再掷」（AC3，词图皆错时连词一起换）；
# - 校对后重生成「原词再掷」（AC3 默认口——词没动也记一笔，因为裁决
#   记录会被换版后的重新校对**覆盖**，重掷历史只活在追加账里）。
TRIGGER_REVIEW_REWORD = "review_reword"
TRIGGER_REGEN_REWORD = "regen_reword"
TRIGGER_REGEN_REROLL = "regen_reroll"

# 打回「外部重掷」**不留痕**（issue #50 明确「不登记」）：它不是这本账的触发口，
# 故此处不设常量——写进轮次账就等于给被拒图记了名。


class QueueItem(NamedTuple):
    """队列里的一张待办资产：登记号 + 类型 + 语义名。

    提示词正文**不进队列项**：出词时刻要拿的是 state 里的最新版（改写后就变了），
    队列只管「出谁」；渲染时按语义名回 state 取，天然带上最新改写。
    """

    picture: int
    kind: str
    name: str


def asset_queue(entries: Sequence[dict[str, Any]], *, pictures: dict[str, int] | None = None) -> tuple[QueueItem, ...]:
    """把资产条目排成素材循环的待办队列（issue #50 AC4）。

    默认顺序**人物→场景→道具**，组内按 ``<Picture N>`` 登记号（剧本出场序，
    编号即叙事顺序）定序；``pictures`` 不给（或名字不在映射里）时按传入序
    稳定排组内末尾——与参考素材登记「找不到出场的排尾」同一口径。

    入参是 ``{kind: [条目]}`` 或 ``(kind, 条目)`` 序列；条目取四键契约
    （name/positive_prompt/negative_prompt/instructions，见
    ``generation._normalize_asset_entries``）。
    """
    items: list[tuple[int, int, int, QueueItem]] = []
    seq = 0

    def _add(kind: str, raw: dict[str, Any]) -> None:
        nonlocal seq
        if kind not in _KIND_QUEUE_ORDER:
            raise ValueError(f"未知资产类型：{kind!r}")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("资产条目缺语义名 name")
        number = (pictures or {}).get(name, 10**6 + seq)
        items.append((_KIND_QUEUE_ORDER[kind], number, seq, QueueItem(number, kind, name)))
        seq += 1

    if isinstance(entries, dict):
        for kind, rows in entries.items():
            for raw in rows or []:
                _add(str(kind), raw)
    else:
        for kind, raw in entries:
            _add(str(kind), raw)

    return tuple(item for *_keys, item in sorted(items, key=lambda t: t[:3]))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_asset_round(
    directory: str | Path,
    *,
    name: str,
    kind: str,
    trigger: str,
    feedback: str,
    prompt_before: str,
    prompt_after: str,
    round_index: int | None = None,
) -> Path:
    """追加一行改写轮次（``<generation_dir>/asset-rounds.jsonl``）。

    追加式快照：一行就是一轮的完整前后对照（``prompt_before``/``prompt_after``），
    只追加不改写——「某轮改了什么、谁要求的」事后逐行可查。``round_index``
    缺省按该语义名已有行数自动续号（跨调用不依赖内存计数）。
    """
    path = Path(directory) / ASSET_ROUNDS_FILENAME
    existing = read_asset_rounds(directory, name=name)
    record = {
        "round": int(round_index) if round_index is not None else len(existing) + 1,
        "name": name,
        "kind": kind,
        "trigger": trigger,
        "feedback": feedback,
        "prompt_before": prompt_before,
        "prompt_after": prompt_after,
        "at": _now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_asset_rounds(directory: str | Path, *, name: str | None = None) -> list[dict[str, Any]]:
    """读轮次账（可按语义名过滤）；文件不在＝空账，坏行**响**（ValueError）。

    坏行响不吞：追加账是「历史可查」的凭据，静默跳过坏行等于历史缺页无人知。
    """
    path = Path(directory) / ASSET_ROUNDS_FILENAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"轮次账第 {lineno} 行不是合法 JSON：{path}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"轮次账第 {lineno} 行不是对象：{path}")
        if name is None or record.get("name") == name:
            out.append(record)
    return out


def rewrite_asset_prompt(old_prompt: str, feedback: str, llm) -> str:
    """按用户意见改写一条资产生图提示词（一轮修订的产物）。

    ``llm`` 由调用方注入（带 ``invoke(str)`` 接口，同 ``rewrite_segment_prompt``
    的口径），测试可传 stub。只动被点名的问题——提示词是逐轮小步改的，
    全篇重写会把没提到的细节一并漂走（同修订基线「未提到的部分原样保留」的用意）。
    """
    request = (
        "请按用户意见改写下面这条 Z-Image 生图提示词。\n"
        "纪律：只落实意见点名的问题；没提到的部分逐字保留；"
        "仍然输出一条可直接复制进生图工具的提示词正文，不要解释、不要前后缀。\n\n"
        f"【当前提示词】\n{old_prompt.strip()}\n\n"
        f"【改写意见】\n{feedback.strip()}"
    )
    raw = llm.invoke(request)
    text = raw if isinstance(raw, str) else getattr(raw, "content", str(raw))
    new_prompt = str(text).strip()
    if not new_prompt:
        raise ValueError("改写结果为空")
    return new_prompt


__all__ = [
    "ASSET_ROUNDS_FILENAME", "QueueItem",
    "TRIGGER_REGEN_REROLL", "TRIGGER_REGEN_REWORD", "TRIGGER_REVIEW_REWORD",
    "asset_queue", "log_asset_round", "read_asset_rounds", "rewrite_asset_prompt",
]
