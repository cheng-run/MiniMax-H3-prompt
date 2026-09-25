"""起步前置澄清的纯函数层：问句抽取、跳过词、澄清块渲染（issue #29 / T7）。

本文件只测**没有 UI、没有 LLM** 的那部分；交互与接线在
``tests/test_wizard_clarification.py``。两者合起来覆盖票面 5 条验收标准。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt import clarification
from minimax_h3_prompt.clarification import (
    generate_clarifying_questions,
    is_skip,
    parse_questions,
    render_clarification_block,
)


class _Reply:
    """假 LLM 回复：只需要 ``content``（与 segment_planner 的取文本方式一致）。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[str] = []

    def invoke(self, request: str) -> _Reply:
        self.requests.append(request)
        return _Reply(self.content)


def test_limit_caps_question_count():
    """上限是硬上限：模型多给也不许多问（票面「数量不超过配置上限」）。"""
    text = "\n".join(f"问题{i}是什么？" for i in range(1, 6))
    assert len(parse_questions(text, 3)) == 3


def test_numbering_and_bullets_are_stripped():
    """模型很爱编号/加项目符号；剥掉后问题本身才能原样问给用户。"""
    text = "1. 庭院是哪个朝代？\n- 人物服装按哪个年代？\n（3）结局是团聚还是分离？"
    assert parse_questions(text, 9) == [
        "庭院是哪个朝代？",
        "人物服装按哪个年代？",
        "结局是团聚还是分离？",
    ]


def test_preamble_without_question_mark_is_not_a_question():
    """模型答「无需澄清」时不能把它当成一个问题去问用户。

    判据是提示词的硬约束（每行问句以问号结尾），不是启发式：全篇没有问号 = 没有问句。
    """
    assert parse_questions("这个主题已经很清楚了，无需澄清。", 3) == []


def test_blank_lines_are_dropped():
    assert parse_questions("庭院是哪个朝代？\n\n\n人物服装按哪个年代？\n", 3) == [
        "庭院是哪个朝代？",
        "人物服装按哪个年代？",
    ]


def test_zero_limit_asks_nothing_and_never_calls_llm():
    """上限为 0 = 关闭澄清：连模型都不该调（用户的 token 不该被这一步花掉）。"""
    llm = _FakeLLM("庭院是哪个朝代？")
    assert generate_clarifying_questions("秋日庭院", llm, 0) == []
    assert llm.requests == []


def test_generate_passes_topic_and_limit_to_model():
    llm = _FakeLLM("庭院是哪个朝代？\n人物服装按哪个年代？\n结局是团聚还是分离？")
    questions = generate_clarifying_questions("秋日庭院里的橘猫", llm, 2)
    assert questions == ["庭院是哪个朝代？", "人物服装按哪个年代？"]
    assert "秋日庭院里的橘猫" in llm.requests[0]
    assert "2" in llm.requests[0], "上限没写进请求——模型无从知道该给几个问题"


@pytest.mark.parametrize("raw", ["跳过", "skip", " SKIP ", "跳过 "])
def test_skip_words(raw):
    assert is_skip(raw) is True


@pytest.mark.parametrize("raw", ["", "北宋", "不要森林", "跳过吧"])
def test_non_skip_words(raw):
    assert is_skip(raw) is False


def test_render_block_lists_pairs_with_question_and_answer():
    block = render_clarification_block([
        ("庭院是哪个朝代？", "北宋"),
        ("人物服装按哪个年代？", "参考现代汉服审美"),
    ])
    assert "北宋" in block and "参考现代汉服审美" in block
    assert "庭院是哪个朝代？" in block
    assert block.startswith("1."), "清单要带编号，与用户修订清单同形"
    assert "必须落实" in block, "澄清块要自述其效力，否则模型会当参考资料"


def test_render_block_is_empty_without_answers():
    """回车不回答 = 没有澄清结论：空块（``_ctx`` 会整个跳掉，请求逐字不变）。"""
    assert render_clarification_block([]) == ""
    assert render_clarification_block([("庭院是哪个朝代？", "   ")]) == ""
    assert render_clarification_block(None) == ""


def test_instruction_file_exists_and_forbids_preamble():
    """提示词文件是这个模块的契约来源：找不到它，抽取规则就没有依据。"""
    instruction = clarification._load_instruction()
    assert "问号" in instruction
    assert clarification.CLARIFY_PROMPT_PATH.is_file()
