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
from minimax_h3_prompt.director.frames import SEGMENT_TYPE_LABELS
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
# 配乐 cue 延续纪律（#330）：base 两条走弱化版、导演台走强化版、ref 刻意不做
# ---------------------------------------------------------------------------
# 为什么不并进上面的 ``_FAMILIES`` 参数化：那里的语义是「五条路线都要有」。配乐这条
# 恰好相反——ref 两条路线**刻意不做**（用户 2026-11-18 裁定，见 CONTEXT.md 不迁清单），
# 并进去就只能靠 skip 列表把它摘出来，摘漏一次就是给已验收的多参产物悄悄加了新措辞。
# 「不做」本身也要钉住：反向断言在这里，顺手加上的人会当场红。


def test_music_cue_discipline_reaches_base_but_not_ref():
    """base 两条路径带弱化版全文，导演台带强化版全文，ref 两条一条都不带。

    先断言两份真源都非空：清空常量时 ``"" in text`` 恒真，守卫就永远不红
    （本票的变异条目「清空配乐弱化版」正靠这句先响）。
    """
    base_text = wd.MUSIC_CUE_DISCIPLINE_BASE
    director_text = wd.MUSIC_CUE_DISCIPLINE_DIRECTOR
    assert base_text, "配乐弱化版真源为空——空串让 in 断言恒真，守卫会失效"
    assert director_text, "配乐强化版真源为空——上面那条 in 断言会恒真"

    for route in ("base_v2", "base_fallback"):
        outbound = _outbound(route)
        assert outbound, f"{route} 没有取到任何出站文本"
        for label, out in outbound:
            assert out, f"{route} 的{label}是空的"
            assert base_text in out, f"{route} 的{label}缺配乐弱化版纪律"
            # 旧措辞与 cue 延续纪律互相矛盾：模型同时收到「同一支 cue」与「无声写 N/A」
            # 时按后者写，接缝处的电平台阶原样复发（#194 实测 −9.8 / +26.6 dB）。
            assert "无声时只写" not in out, f"{route} 的{label}还留着「无声时只写 N/A」的旧措辞"
            assert "无声写 `N/A`" not in out, f"{route} 的{label}还留着「无声写 N/A」的旧措辞"

    for label, out in _outbound("director"):
        assert director_text in out, f"导演台的{label}缺配乐强化版纪律"
        assert base_text not in out, f"导演台的{label}不该拿到弱化版（段类型分叉是导演台专有）"

    for route in ("ref_v2", "ref_fallback"):
        for label, out in _outbound(route):
            assert out, f"{route} 的{label}是空的"
            assert base_text not in out, f"{route} 的{label}被加上了配乐弱化版（本票刻意不做）"
            assert director_text not in out, f"{route} 的{label}被加上了配乐强化版（本票刻意不做）"


# 导演台「按段类型分叉」的候选措辞分两段，缺一段就漏一类判据：
# 1) 段类型标识符（camera_cut 等）＋中文标签（运镜·切镜 等）：从真源
#    ``SEGMENT_TYPE_LABELS`` 派生。字面重抄一份会在强化版改词时**静默失效**（被测的
#    正是「base 弱化版不含这套分叉措辞」，抄的词对不上就恒绿）。这十个词已逐个 ``in``
#    实测过 ``MUSIC_CUE_DISCIPLINE_BASE``（2026-11-18）：确实全部不含才放进来。
# 2) 段类型**复合称谓**「引导段／切镜段／开场段」：强化版正文里括号前那三个词
#    （「引导段（续接 continue / 运镜·同场景 camera_same / 收尾定格 ending）」等），
#    #330 的 AC 点名要防的正是这套分档规则。这三个词**派生不出来**：
#    ``GUIDED_SEGMENT_TYPES`` 是英文标识符集合（continue / camera_same / ending），
#    ``SEGMENT_TYPE_LABELS`` 只有「续接」「运镜·同场景」这类标签，库里没有任何常量写着
#    「引导段」这种「中文＋段」的复合称谓。所以这三个是**显式字面例外**，不是漏派生——
#    写在这里免得下一个人以为该从常量取。它们已核实：在强化版
#    ``MUSIC_CUE_DISCIPLINE_DIRECTOR`` 里确实出现（否则这条断言就是在防不存在的措辞），
#    在 base 版里逐个 ``in`` 实测均不含（2026-11-18），加回来不会误伤。
# 判据是「真的不含」不是「应该不含」——将来若某个短标签（如「续接」）撞上 base 的
# 自然语言，就把它从对应一段剔掉并在本注释写明原因，别留个会误伤的词。
_SEGMENT_TYPE_WORDS = (
    tuple(SEGMENT_TYPE_LABELS)
    + tuple(SEGMENT_TYPE_LABELS.values())
    + ("引导段", "切镜段", "开场段")
)


def test_base_music_discipline_keeps_silence_exception_only():
    """弱化版的形态约束：保留静音例外（官方 base-en.txt §4.6 overall_soundscape 的口径，
    不是 §4.7 non_diegetic_music），且**不许**混入导演台那套按段类型分叉的措辞。

    这条对真源常量断言，断言的是两条**反向排斥**（该有的一句在、导演台专有的词一个
    都不在），不是「常量等于某个值」——后者才是被测物自证。真源形态一漂，上面那条
    出站断言只会证明「漂过的那份确实送到了」，看不出它已经不是规格要求的那份。
    """
    text = wd.MUSIC_CUE_DISCIPLINE_BASE
    assert text, "配乐弱化版真源为空"
    assert "用户明确要求全片静音" in text, \
        "弱化版丢了静音例外句（官方 base-en.txt §4.6 overall_soundscape 的静音例外）"
    assert "同一支 cue" in text, "弱化版没写「同一支 cue 延续」"
    for word in _SEGMENT_TYPE_WORDS:
        assert word not in text, f"弱化版混入了导演台的段类型分叉措辞「{word}」"


# ---------------------------------------------------------------------------
# 两条「一眼看穿」的细节纪律（#329）：只发往「该带的那几条」路线
# ---------------------------------------------------------------------------
# 为什么不能塞进上面的 ``_FAMILIES``：那一族是**无差别**的「五条路线全含」，而这两条
# 恰恰有路线刻意不做——ref 两条路径明确不加（用户 2026-11-18 裁定，#327），门槛那条
# 还有强弱两版（base 弱化、导演台强化）。所以这里按路线写明「该带哪几个常量」，
# 不在表里的常量就断言**不含**：正向与反向都是决定的一部分，缺哪一半都会让
# 「有人顺手给 ref 加上」或「有人把强化版塞进 base」静默通过。
_CARRIED_DETAIL_DISCIPLINES = {
    "base_v2": ("PROP_OWNERSHIP_DISCIPLINE", "THRESHOLD_CROSSING_DISCIPLINE_BASE"),
    "base_fallback": ("PROP_OWNERSHIP_DISCIPLINE", "THRESHOLD_CROSSING_DISCIPLINE_BASE"),
    # ref 刻意不做（#327 不迁清单）：它那两条指令是与 base 无耦合的独立常量，
    # 加进去会改动已验收的多参产物措辞。
    "ref_v2": (),
    "ref_fallback": (),
    # 导演台走同一个道具归属常量（逐字共用），门槛取**强化版**——不是 BASE 版。
    "director": ("PROP_OWNERSHIP_DISCIPLINE", "THRESHOLD_CROSSING_DISCIPLINE_DIRECTOR"),
}

# 这一族全部候选常量：某路线没带的，逐个反向断言「不含」。
_DETAIL_DISCIPLINE_CONSTANTS = tuple(sorted({
    name for names in _CARRIED_DETAIL_DISCIPLINES.values() for name in names
}))


@pytest.mark.parametrize("route", ROUTES)
def test_detail_disciplines_reach_only_the_routes_that_should_carry_them(route):
    """该带的路线含全文，不该带的路线**一个字都不含**（#329）。

    先自证真源非空：空串让 ``in`` 恒真、让 ``not in`` 恒假，正向与反向会同时失效。
    刻意不把这两条并进 ``_FAMILIES``：那里是「五条路线全含」，而 ref 两条的决定
    正是不含——并进去就只能靠删路线来迁就，等于把「刻意不做」这条决定抹掉。
    """
    for name in _DETAIL_DISCIPLINE_CONSTANTS:
        assert getattr(wd, name), \
            f"{name} 为空——空串让正向断言恒真、反向断言恒假，守卫会失效"

    carried = _CARRIED_DETAIL_DISCIPLINES[route]
    outbound = _outbound(route)
    assert outbound, f"{route} 没有取到任何出站文本"
    for label, text in outbound:
        assert text, f"{route} 的{label}是空的"
        for name in carried:
            assert getattr(wd, name) in text, f"{route} 的{label}缺 {name} 纪律"
        for name in set(_DETAIL_DISCIPLINE_CONSTANTS) - set(carried):
            assert getattr(wd, name) not in text, (
                f"{route} 的{label}不该带 {name}"
                "（#327 的路线分工：ref 刻意不做，base 只取门槛弱化版）")


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
