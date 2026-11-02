"""CONTEXT.md 结构守卫：词条标题与正文不许静默错配。

**为什么需要它**：``CONTEXT.md`` 是领域术语的**唯一真源**（``docs/agents/domain.md``
指明「按 CONTEXT.md 定义的术语用词」），而它同时是最容易被顺手改坏的文件——新票几乎
都要往里加词条。词条格式是「``**术语（english）**:`` 标题行 ＋ 正文 ＋ 可选
``_Avoid_:`` 行」，条目之间**没有任何校验**：标题行被漏掉时，正文会**悬空挂在上一条目的
``_Avoid_:`` 后面**，读起来像它的续写，而测试、diff、grep 全都不报错。

2026-10-20 实测（issue #55）：往「总时长」之前插「段产出账」时吃掉了
``**总时长（total duration）**:`` 那一行，正文就此悬空；是代码复审用
``grep "总时长（total duration）"`` 返回 NOT FOUND 才抓出来的。本文件把那条人眼判据
变成会响的判据。

**判据只取结构性事实，不数个数**：数标题个数那类钉合每次加词条都要改测试，而且实测
挡不住这次这个 bug——文件里有 9 行以 ``**`` 开头但**不是**词条标题的强调行（如
``**已知边界**：…``），朴素的 ``grep -c '^\\*\\*'`` 在「标题被吃」的坏版上给 61
（看着 +1 正常），精确的 ``^\\*\\*.+\\*\\*:$`` 才给 52（+0，报红）。故这里用四条
彼此独立的不变式，各自能单独变红：

1. 词条块必须有正文（挡住「加了标题忘了写正文」）；
2. ``_Avoid_:`` 只能落在词条块里（挡住孤立的 ``_Avoid_:``）；
3. ``_Avoid_:`` 之后的下一个非空行必须是标题或文件结束（**直接咬住本次那个 bug**）；
4. 同一节内出现过词条块之后，不许再出现散文块（**咬住更一般的形态**：正文块被从它的
   标题上切下来——包括被切的那一条**没有** ``_Avoid_:`` 的情形，那时第 3 条不响）。

**已知边界**：``_Avoid_:`` 是可选的（53 个词条里 6 个没有），所以「把某个无 ``_Avoid_:``
词条的标题吃掉、且它前后都是散文」这种形态四条都咬不住——但那种形态要求该节整段都是
散文，而散文块按第 4 条只允许出现在节首。
"""
from __future__ import annotations

import re
from pathlib import Path

CONTEXT_PATH = Path(__file__).parents[1] / "CONTEXT.md"

# 词条标题：整行加粗并以冒号收尾（``**术语（english）**:``）。判据必须带 ``:$``——
# 只认 ``^\\*\\*`` 会把 9 行强调正文误算成标题，那正是「坏版看起来正常」的原因。
_HEADING = re.compile(r"^\*\*.+\*\*:$")
_SECTION = re.compile(r"^#{1,6} ")
_AVOID = re.compile(r"^_Avoid_:")


def _lines() -> list[str]:
    return CONTEXT_PATH.read_text(encoding="utf-8").splitlines()


def _blocks(lines: list[str]) -> list[list[tuple[int, str]]]:
    """按空行切成块，每行带 1-based 行号（报错时能直接指到行）。"""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for number, text in enumerate(lines, 1):
        if text.strip():
            current.append((number, text))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _is_heading(block: list[tuple[int, str]]) -> bool:
    return bool(_HEADING.match(block[0][1]))


def _is_section(block: list[tuple[int, str]]) -> bool:
    return bool(_SECTION.match(block[0][1]))


def _describe(block: list[tuple[int, str]]) -> str:
    number, text = block[0]
    return f"第 {number} 行「{text[:48]}」"


def test_every_term_heading_has_a_body() -> None:
    """词条块里标题之后、``_Avoid_:`` 之前必须有正文（空词条＝术语没了定义）。"""
    bad = []
    for block in _blocks(_lines()):
        if not _is_heading(block):
            continue
        body = [text for _number, text in block[1:] if not _AVOID.match(text)]
        if not body:
            bad.append(_describe(block))
    assert not bad, "这些词条只有标题、没有正文：\n  " + "\n  ".join(bad)


def test_avoid_lines_live_inside_a_term_block() -> None:
    """``_Avoid_:`` 是词条的收尾行，不许单独成块（孤立＝它的标题被吃掉了）。"""
    bad = [
        _describe(block)
        for block in _blocks(_lines())
        if any(_AVOID.match(text) for _number, text in block) and not _is_heading(block)
    ]
    assert not bad, "这些块里有 _Avoid_: 却不以词条标题开头（标题可能被吃掉）：\n  " + "\n  ".join(bad)


def test_nothing_follows_an_avoid_line_without_a_heading() -> None:
    """``_Avoid_:`` 之后的下一个非空行必须是标题或文件结束。

    这条直接对应 issue #55 实测的那个形态：标题被吃 → 下一条目的正文紧跟在
    ``_Avoid_:`` 后面，读起来像上一条的续写。**中间隔不隔空行都要抓**，所以这里按
    行遍历而不是按块遍历（隔了空行时块级判据也能响，没隔空行时只有这条响）。
    """
    lines = _lines()
    bad = []
    for index, text in enumerate(lines):
        if not _AVOID.match(text):
            continue
        following = next((t for t in lines[index + 1:] if t.strip()), None)
        if following is not None and not (_HEADING.match(following) or _SECTION.match(following)):
            bad.append(f"第 {index + 1} 行的 _Avoid_: 之后是「{following[:48]}」")
    assert not bad, (
        "_Avoid_: 之后必须是词条标题或节标题（不是的话，下一条的正文悬空了）：\n  "
        + "\n  ".join(bad))


def test_a_section_does_not_go_back_to_prose_after_its_terms() -> None:
    """同一节内出现过词条块之后，不许再出现散文块。

    节首的散文是合法的（如「提示词格式纪律」节先讲迁移结论、再逐条给术语），但
    **词条之后**再冒出散文块，就是某条正文被从它的标题上切了下来——包括被切的那一条
    没有 ``_Avoid_:`` 的情形（那时上一条判据不响）。文件开头的引言区不属于任何节，不受此限。
    """
    bad = []
    in_section = False
    seen_term = False
    for block in _blocks(_lines()):
        if _is_section(block):
            in_section, seen_term = True, False
            continue
        if not in_section:
            continue
        if _is_heading(block):
            seen_term = True
        elif seen_term:
            bad.append(_describe(block))
    assert not bad, (
        "这些节在词条之后又出现了散文块（正文可能被从标题上切下来）：\n  "
        + "\n  ".join(bad))
