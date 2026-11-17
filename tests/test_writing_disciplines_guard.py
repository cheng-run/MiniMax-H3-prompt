"""跨路线守卫：五条写段路径都要携带三族纪律的真源文本（#316）。

为什么要这组用例：三族纪律已经单源化（#312–#315），但「常量对了、某条路线的组装
漏了」不会让任何既有用例报红——直接 import 常量再自比自只是被测物自证。守卫必须
落在**出站文本**上：首尾帧 v2／回退、多参 v2／回退取真实请求正文；导演台取写段
模型收到的**系统提示**（``load_instruction``，占位符替换之后）与**完整出站请求**
（``build_segment_request``，它把系统提示与窗外清单一起拼进去）。两处都断言，是
因为模型同时看得到这两份文本，漏一处就等于少一条纪律。

每条断言前先断言真源常量非空：否则「把常量清空」的变异会被 ``"" in text`` 恒真
骗过，守卫永远不红——本票的变异条目正是清空 ``CAMERA_SPEED_DISCIPLINE``。
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
