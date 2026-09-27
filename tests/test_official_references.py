"""官方 references（``src/minimax_h3_prompt/references/*.txt``）是常量的唯一对照物（issue #35）。

**改前是什么样**：两份 vendor 进来的官方原文谁也没当回事——`ref-en.txt` 没有任何代码打开，
`base-en.txt` 只有一条断言顺手读过它一次；而代码里写死的官方措辞（帧变体对齐指令、
edge-stability 句、六段 / 三段字段名、retention 标记）全部靠注释自陈「官方如此」，
链条上没有任何东西会去核对。这正是本仓「规则在、接线不在」的一类：声明在，判据不在。

**本文件把它翻过来**：每个常量必须在官方原文里**逐字**找得到；反向地，被判为「官方没有」
的自创结构必须**逐字**找不到。于是三件事同时成立——

1. 两份 references 真被读到（本文件是它们的消费者，`base` / `ref` 各有人读）；
2. 常量的唯一可写来源就是它自己（改官方文件不同步常量 → 报红；反之亦然）；
3. 注释里的「官方要求 / 官方没有」不再是无从验证的断言。

**为什么用逐字子串而不是解析官方文档**：解析要给一份给人读的散文引入脆弱的位置依赖
（改个排版就 import 崩），而本仓的纪律是加载链上不做模板与解析（ADR 0006）。钉合放在
测试里，代价是「上游换版本 → 报红 → 人工确认」，这正是我们要的：官方措辞变了应当是一次
**有意的**改动，而不是悄悄跟着漂（裁定见 ``docs/adr/0008``）。
"""
from pathlib import Path

from minimax_h3_prompt.tools.h3_validator import (
    ALIGN_TEMPLATES,
    BASE_SECTIONS,
    EDGE_STABILITY_SENTENCE,
    REF_SECTIONS,
    RETENTION_MARKERS,
    _BANNED_SECTION_MARKERS,
)

REFERENCES_DIR = Path(__file__).parents[1] / "src" / "minimax_h3_prompt" / "references"

# 刻意取私有名：要钉的正是**这个常量自己**的声明（「官方 base-en.txt 不存在这些结构」）。
# 在测试里另抄一份字面量等于再造一处可写来源，那正是本票要消掉的东西。
BASE_EN = (REFERENCES_DIR / "base-en.txt").read_text(encoding="utf-8")
REF_EN = (REFERENCES_DIR / "ref-en.txt").read_text(encoding="utf-8")


def test_base_mode_constants_are_verbatim_from_official_text():
    """base 侧：对齐指令、edge-stability 句、三段字段名，逐字都在 base-en.txt 里。"""
    assert EDGE_STABILITY_SENTENCE in BASE_EN, \
        "EDGE_STABILITY_SENTENCE 与官方原文不再逐字一致（官方换了措辞？）"
    for variant, line in ALIGN_TEMPLATES.items():
        assert line in BASE_EN, f"ALIGN_TEMPLATES[{variant!r}] 与官方原文不再逐字一致"
    for section in BASE_SECTIONS:
        assert section in BASE_EN, f"BASE_SECTIONS 的 {section!r} 不在 base-en.txt 里"


def test_ref_mode_constants_are_verbatim_from_official_text():
    """ref 侧：六段字段名与 retention 标记，逐字都在 ref-en.txt 里。

    本条同时是 ref-en.txt 的第一个消费者——改前它一份代码都没读过。
    """
    for section in REF_SECTIONS:
        assert section in REF_EN, f"REF_SECTIONS 的 {section!r} 不在 ref-en.txt 里"
    for marker in RETENTION_MARKERS:
        assert marker in REF_EN, f"RETENTION_MARKERS 的 {marker!r} 不在 ref-en.txt 里"


def test_banned_self_invented_markers_are_absent_from_official_text():
    """反向：被判「自创结构、出现即 error」的标记，官方两份原文里确实一个都没有。

    这条把「官方没这些字段」从注释里的断言变成可执行的判据。哪天真在官方原文里出现了，
    要做的不是删掉这条用例，而是把该结构从 ``_BANNED_SECTION_MARKERS`` 里去掉。
    """
    for marker in _BANNED_SECTION_MARKERS:
        assert marker not in BASE_EN, f"{marker} 出现在 base-en.txt 里——它不再是「自创结构」"
        assert marker not in REF_EN, f"{marker} 出现在 ref-en.txt 里——它不再是「自创结构」"
