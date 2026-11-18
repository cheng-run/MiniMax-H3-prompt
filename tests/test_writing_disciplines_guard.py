"""跨路线守卫：五条写段路径都要携带三族纪律的真源文本（#316）。

为什么要这组用例：三族纪律已经单源化（#312–#315），但「常量对了、某条路线的组装
漏了」不会让任何既有用例报红——直接 import 常量再自比自只是被测物自证。守卫必须
落在**出站文本**上：首尾帧 v2／回退、多参 v2／回退取真实请求正文；导演台取写段
模型收到的**系统提示**（``load_instruction``，占位符替换之后）与**完整出站请求**
（``build_segment_request``，它把系统提示与窗外清单一起拼进去）。两处都断言，是
因为模型同时看得到这两份文本，漏一处就等于少一条纪律。

每条断言前先断言真源常量非空：否则「把常量清空」的变异会被 ``"" in text`` 恒真
骗过，守卫永远不红——本票的变异条目正是清空 ``CAMERA_SPEED_DISCIPLINE``。

末节是帧一致性纪律（#319）：它目前只发往首尾帧两条路径（v2 与回退），导演台要到
#321/#322 才接，所以不并进上面的五路线参数化——单独钉它真正该到的两处，外加一条
「源码里不许出现第二份」：纪律句被整句抄进别的文件时，出站文本断言照样绿。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt import writing_disciplines as wd
from minimax_h3_prompt.director.prompts import build_segment_request, load_instruction
from minimax_h3_prompt.segment_prompts import (
    build_ref_segment_v2_request,
    build_segment_v2_request,
    rewrite_ref_segment_prompt,
    rewrite_segment_prompt,
    split_shots_from_prompt,
)
from minimax_h3_prompt.tools.h3_validator import split_ref_sections
from test_director_prompts import _SUBJECT_DEFS, _plan
from test_segment_prompts import (
    SAMPLE_PROMPT,
    _REF_WHOLE_PROMPT,
    FakeLLM,
    _ref_plans,
    _ref_request_kwargs,
    _ref_v2_llm,
    _v2_plans,
)

ROUTES = ("base_v2", "base_fallback", "ref_v2", "ref_fallback", "director")

# 只有「块形态」路线会把窗外事件清单整块拼进请求，标题句才该出现；两条回退路径
# 拿不到 all_plans（没有窗外清单可列），只有纪律句（#314）。
_BLOCK_ROUTES = ("base_v2", "ref_v2", "director")

# (家庭名, writing_disciplines 里的常量名)；测试时再取值，清空真源才能被看见。
_FAMILIES = (
    ("camera_speed", "CAMERA_SPEED_DISCIPLINE"),
    ("future_events", "FUTURE_EVENTS_DISCIPLINE"),
    ("anaphora", "ANAPHORA_DISCIPLINE"),
)


def _outbound(route: str) -> list[tuple[str, str]]:
    """该路线的全部出站文本：逐份返回，任何一份漏了纪律都要报红。

    最后一项固定是「完整出站请求」——块形态路线的窗外标题句在它里面断言。
    """
    if route == "base_v2":
        plans = _v2_plans()
        # 取第 0 段：只有存在后续段时，时间窗外清单（标题句＋纪律句）才会被拼进请求。
        return [("首尾帧 v2 请求",
                 build_segment_v2_request(plans[0], plans, {"shot_table": "x"}))]
    if route == "base_fallback":
        segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=15.0)
        llm = FakeLLM()
        rewrite_segment_prompt(segments[1], SAMPLE_PROMPT, llm)
        return [("首尾帧回退请求", llm.last_request)]
    if route == "ref_v2":
        plan, all_plans = _ref_plans()[0], _ref_plans()
        return [("多参 v2 请求",
                 build_ref_segment_v2_request(plan, all_plans, **_ref_request_kwargs()))]
    if route == "ref_fallback":
        plan, all_plans = _ref_plans()[1], _ref_plans()
        llm = _ref_v2_llm()
        rewrite_ref_segment_prompt(
            base_prompt=_REF_WHOLE_PROMPT, plan=plan, total_segments=len(all_plans),
            total_s=22,
            shot_texts=["[Shot 2] A close-up as she hands 青铜钥匙 to the boy."],
            llm=llm,
        )
        return [("多参回退请求", llm.requests[-1])]
    if route == "director":
        plan = _plan()
        instruction = load_instruction(plan.mode)
        request = build_segment_request(
            plan, plan.segments[1], sections=split_ref_sections(_SUBJECT_DEFS))
        return [("导演台写段系统提示", instruction), ("导演台完整出站请求", request)]
    raise AssertionError(f"未知路线：{route}")


@pytest.mark.parametrize(
    "family,constant_name", _FAMILIES, ids=[name for name, _ in _FAMILIES])
@pytest.mark.parametrize("route", ROUTES)
def test_every_write_route_sends_the_family_discipline(route, family, constant_name):
    """五条路径 × 三族纪律：每条出站文本都要含真源全文。

    先断言真源非空，再断言它在出站文本里：清空常量时若没有第一句，``"" in text``
    恒真，守卫会永远绿（本票变异条目「清空机位速度真源」就杀不死它了）。
    """
    family_text = getattr(wd, constant_name)
    assert family_text, f"{family} 的真源文本为空——空串让 in 断言恒真，守卫会失效"

    outbound = _outbound(route)
    assert outbound, f"{route} 没有取到任何出站文本"
    for label, text in outbound:
        assert text, f"{route} 的{label}是空的"
        assert family_text in text, f"{route} 的{label}缺 {family} 纪律"

    if family == "future_events" and route in _BLOCK_ROUTES:
        assert wd.FUTURE_EVENTS_TITLE, "时间窗外真源标题句为空"
        assert wd.FUTURE_EVENTS_TITLE in outbound[-1][1], \
            f"{route} 的{outbound[-1][0]}缺时间窗外真源标题句"


# ---------------------------------------------------------------------------
# 帧一致性纪律（#319）：只发往首尾帧两条路径，且全句在源码里只许有一份
# ---------------------------------------------------------------------------

# 源码根：真源与全部写段路径都住在 ``src/minimax_h3_prompt`` 下，提示词文件在它的
# ``prompts/`` 子目录里——两处都扫，`prompts/*.md` 的同义改写同样算第二份拷贝。
from pathlib import Path  # noqa: E402 - 只服务本节扫描，别把 import 提到文件头扰乱既有分组
import ast  # noqa: E402 - 同上：扫描器只在末节用

_SRC_ROOT = Path(wd.__file__).resolve().parent
_SCANNED_SUFFIXES = (".py", ".md")


def _frame_discipline() -> str:
    return wd.FRAME_CONSISTENCY_DISCIPLINE


def test_frame_consistency_discipline_reaches_both_base_route_write_requests():
    """首尾帧 v2 与回退两条写段请求都要带帧一致性真源全文（#319）。

    先断言真源非空再断言 ``in``：清空常量时空串让 ``in`` 恒真，守卫永远不会红，
    本票的变异条目就杀不死它。
    """
    text = _frame_discipline()
    assert text, "帧一致性真源为空——空串让 in 断言恒真，守卫会失效"
    for route in ("base_v2", "base_fallback"):
        outbound = _outbound(route)
        assert outbound, f"{route} 没有取到任何出站文本"
        for label, out in outbound:
            assert out, f"{route} 的{label}是空的"
            assert text in out, f"{route} 的{label}缺帧一致性纪律"


def test_frame_consistency_discipline_has_no_second_copy_in_source():
    """整句在源码与提示词文件里**只出现一次**（就在真源模块里）。

    为什么光有出站断言不够：把整句抄进 ``segment_prompts`` 的另一处（或某个
    ``prompts/*.md``）时，出站文本仍旧含它，上面那条照样绿——第二份拷贝从此各自漂。
    这里用整句做判据（不是特征词组）：真源措辞一改，抄的那份立刻数得出来。

    判据要同时看**源码原文**与**Python 字符串常量的值**：长句在本仓按源码行宽拆成
    多段隐式拼接（真源自身就是），原文里根本没有连着的整句——只扫原文会 0 命中，
    守卫就成了永远绿的空转。``ast`` 把隐式拼接折成一个常量，抄一份的写法再变也逃不掉。
    """
    text = _frame_discipline()
    assert text, "帧一致性真源为空——空串会让「出现在任何文件」恒真"

    def _carries(path: Path) -> bool:
        raw = path.read_text(encoding="utf-8")
        if text in raw:
            return True
        if path.suffix != ".py":
            return False
        return any(
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            and text in node.value
            for node in ast.walk(ast.parse(raw))
        )

    hits = sorted(
        str(path.relative_to(_SRC_ROOT)).replace("\\", "/")
        for path in _SRC_ROOT.rglob("*")
        if path.is_file() and path.suffix in _SCANNED_SUFFIXES and _carries(path)
    )
    assert hits == ["writing_disciplines.py"], \
        f"帧一致性纪律句在源码里应只出现一份（真源模块），实际：{hits}"
