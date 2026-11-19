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


# #320：导演台有两张名字都带「首帧」的图——开场首帧图（全片第一帧，收在阶段 1 之后）
# 与段首帧图（切镜段逐段挂的那张，写段时还不存在）。混用会直接推出错误实现，所以
# 「两条词条同时在场」本身是契约：只写一条、或把两条并成一条，读的人就无法分辨。
# 只钉标题原文（与 ``_HEADING`` 同一口径），不钉正文措辞——正文怎么改都行，名字不能少。
_DISTINCT_FRAME_IMAGES = (
    "**开场首帧图（opening still）**:",
    "**段首帧图（segment first frame）**:",
)


def test_opening_and_segment_first_frames_stay_distinct_terms() -> None:
    """两张「首帧图」必须各有一条词条，且分属两个标题（#320）。"""
    headings = {block[0][1] for block in _blocks(_lines()) if _is_heading(block)}
    missing = [heading for heading in _DISTINCT_FRAME_IMAGES if heading not in headings]
    assert not missing, (
        "这两张图的词条必须同时在场（混用它们会推出错误实现方案）：\n  "
        + "\n  ".join(missing))


# #335：两类空间关系必须分开命名。C1 事故（「窗在人物左侧」被改写成「窗在身后」）改的是
# **朝向**，而她「靠不靠窗」在改写前后没变——把两者并成一个「空间关系」，事后既说不清
# 改了什么，也会让守卫的缺口看起来比实际更宽。与 #320 同款判据：只钉标题原文与互相点名，
# 不钉正文措辞。
_SPATIAL_RELATION_TERMS = (
    "**朝向关系（orientation relation）**:",
    "**接触关系（contact relation）**:",
)


def _section_text(keyword: str) -> str:
    """取标题里含 ``keyword`` 那一节的全部正文（到下一个标题为止）。"""
    lines = _lines()
    start = next((index for index, text in enumerate(lines)
                  if _SECTION.match(text) and keyword in text), None)
    if start is None:
        return ""
    body = []
    for text in lines[start + 1:]:
        if _SECTION.match(text):
            break
        body.append(text)
    return "\n".join(body)


def _term_body(heading: str) -> list[str]:
    """取某条词条的正文行（不含标题行，空表＝这条词条不在）。"""
    for block in _blocks(_lines()):
        if _is_heading(block) and block[0][1] == heading:
            return [text for _number, text in block[1:]]
    return []


def test_orientation_and_contact_relations_stay_distinct_terms() -> None:
    """两类空间关系各有一条词条，且互相点名（#335）。"""
    missing = [heading for heading in _SPATIAL_RELATION_TERMS if not _term_body(heading)]
    assert not missing, (
        "这两条词条必须同时在场（并成一条就说不清 C1 改的是哪一类关系）：\n  "
        + "\n  ".join(missing))
    orientation, contact = (_term_body(heading) for heading in _SPATIAL_RELATION_TERMS)
    assert any("接触关系" in line for line in orientation), (
        "「朝向关系」正文必须点名「接触关系」，否则读一条找不到另一条")
    assert any("朝向关系" in line for line in contact), (
        "「接触关系」正文必须点名「朝向关系」，否则读一条找不到另一条")


def test_frame_description_states_its_layer_boundary() -> None:
    """「关键帧实际画面」要写清效力只覆盖画面层，并注明现有守卫对两类关系都看不见（#335）。"""
    body = "\n".join(_term_body("**关键帧实际画面（frame description）**:"))
    assert body, "「关键帧实际画面」词条不见了"
    for token in ("画面层", "设定层", "看不见"):
        assert token in body, (
            f"「关键帧实际画面」的边界必须写明「{token}」——"
            "读者要能看出它的优先级管不到设定层、也没有机读守卫盯着这两类关系")
    # 补边界是**就地追加**：原有的事实源表述一条都不许丢（不删历史正文）。
    for original in ("唯一画面事实源", "分镜表只是计划", "两者冲突时一律以画面为准"):
        assert original in body, f"既有正文被改写了，原文片段「{original}」必须保留"


def test_n6_ruling_is_recorded_as_not_done_yet() -> None:
    """N6 的裁定（维持以画面为准、根因归首帧图、延后方向归 N6 自己的批次）要落在「明确不做」里（#335）。

    延后方向在 2026-11-19（#352）改成「**归 N6 自己的批次**，**不**并入共享编剧层那批」。
    旧 token「共享编剧层」靠否定句也能满足——把裁定改回「并入共享编剧层那批」时，那四个
    字仍在、用例照样绿，断言已失去区分力。所以 token 换成「归 N6 自己的批次」，并另断言
    否定形态本身在：只有「不并入」这半句还在，N6 才不会被顺回动机批次。
    """
    rulings = _section_text("明确不做")
    assert rulings, "CONTEXT.md 没有「明确不做」一节，N6 的裁定无处可落"
    for token in ("N6", "以画面为准", "首帧图", "归 N6 自己的批次"):
        assert token in rulings, (
            f"「明确不做」里的 N6 条目必须写明「{token}」——理由与延后方向缺一样，"
            "下一轮就会当成「还没发现」重新讨论一遍")
    assert "**不**并入共享编剧层那批" in rulings, (
        "N6 的延后方向必须明确「不并入共享编剧层那批」——只留「归 N6 自己的批次」"
        "仍可能被读成与动机批次同批；改回并入写法时正需要这条报红")


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
