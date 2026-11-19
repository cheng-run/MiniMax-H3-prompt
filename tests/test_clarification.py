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
        # 直调入口会把看门狗回调塞进 config；记下来才能证「真的走了统一入口」
        # （复审 S1：裸 invoke 的生产形态正是 config 为空）。
        self.configs: list[dict | None] = []

    def invoke(self, request: str, config: dict | None = None) -> _Reply:
        self.requests.append(request)
        self.configs.append(config)
        return _Reply(self.content)


class _BoomLLM:
    def invoke(self, request: str, config: dict | None = None) -> _Reply:
        raise RuntimeError("gateway boom")


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


def test_generate_goes_through_the_direct_call_watchdog():
    """澄清是 ADR 0019 点名要接看门狗的直调点（复审 S1）。

    判据落在「模型收到了看门狗回调」上：裸 ``llm.invoke`` 时 config 为 None，
    调用期间既没有首字节心跳也没有卡住重试。变异检验：改回裸 invoke，本条红。
    """
    llm = _FakeLLM("庭院是哪个朝代？")
    generate_clarifying_questions("秋日庭院", llm, 1)
    assert llm.configs and llm.configs[0], "澄清调用没带 config——仍是裸 invoke"
    assert llm.configs[0].get("callbacks"), "澄清调用没挂看门狗回调（等不出首字节心跳）"


def test_generate_raises_with_the_watchdog_diagnosis_on_failure():
    """失败语义照旧：抛异常由调用方报出来再跳过澄清（绝不静默）。"""
    with pytest.raises(RuntimeError) as excinfo:
        generate_clarifying_questions("秋日庭院", _BoomLLM(), 1)
    assert "gateway boom" in str(excinfo.value), "异常里没带病因，用户看不出是网关还是内容"


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


# ---------------------------------------------------------------------------
# 出站请求守卫：朝向与空间站位维度（#337）
# ---------------------------------------------------------------------------
# 断言落在**模型眼前收到的请求全文**上，不直读 ``clarifier.md``：提示词措辞会漂，
# 文件内容断言会随句子改写一起失效，而「模型有没有被告知要问这一块」才是要守的事实。
# 锚词取语义单元的组合（朝向／站位／左／右），不取整句——整句一改守卫就假红。
_ORIENTATION_ANCHORS = ("朝向", "站位", "左", "右")

# 「不得问会推翻主题的物件」约束的语义锚（#337 票面要求它必须仍在）。同取组合：
# 单看「主题」满篇都是，单看「推翻」也可能是别的句子，三个一起才是那条约束。
_TOPIC_CONSTRAINT_ANCHORS = ("细化", "推翻", "主题")


def test_outbound_request_asks_for_orientation_and_spatial_placement():
    """模型必须被要求问人物朝向与关键环境物的左右前后（#337）。

    变异检验：删掉提示词里那条维度后本条变红（``clarifier_orientation_dimension_dropped``）。
    """
    llm = _FakeLLM("庭院是哪个朝代？")
    generate_clarifying_questions("秋日庭院里的橘猫", llm, 1)
    request = llm.requests[0]
    missing = [anchor for anchor in _ORIENTATION_ANCHORS if anchor not in request]
    assert not missing, f"出站请求里没有朝向/站位的问法锚词：{missing}"


def test_outbound_request_keeps_the_do_not_overturn_the_topic_rule():
    """补了新维度也不能丢掉「只能细化，不得推翻主题」的既有约束（#337 票面）。"""
    llm = _FakeLLM("庭院是哪个朝代？")
    generate_clarifying_questions("秋日庭院里的橘猫", llm, 1)
    request = llm.requests[0]
    missing = [anchor for anchor in _TOPIC_CONSTRAINT_ANCHORS if anchor not in request]
    assert not missing, f"出站请求里缺少「不得推翻主题」约束的锚词：{missing}"
