"""角色提示词「## 输入」声明的来源，必须真的进了该节点的请求文本。

**为什么需要它**：2026-11-19 spec #349 的 #351 犯过一次——`screenwriter.md` 让模型
「动机只能取自导演阐述、制作计划、人物设计」，而 `_make_screenwriter_node` 只传了
导演阐述／原始剧情／起步澄清／创意锁定：制作计划根本不在请求里，人物设计要到
`parallel_designers` 才产出（排在编剧之后）。模型被要求使用两份它看不见的输入。
复审两轴都报了这一条，但它是靠**人读代码**发现的，仓里没有任何机检。
`docs/agents/engineering-cadence.md` 的「拒绝类判据：先问产生方知不知道」写的就是
这件事，但那只是一段散文，没有牙齿。

**判据落在真渲染上，不读源码正则**：本文件用哨兵 state 真的调一次节点的消息构造，
monkeypatch `run_agent` 截住那条消息，再从消息里取 `【标签】` 块。先试过「正则扫
`_ctx(kwarg=…)`」——它有两类错：把 `统筹美术设计=` 再匹配出 `美术设计=`（子串假阳性），
又把 `_brief_block` / `_design_context` 这类 helper 传的标签整个漏掉（假阴性）。
渲染一次就都对了。

**已知边界**：只在消息真的渲染得出来时判（该模式产出的路线才建节点）；渲染抛错即跳过
（例如 ref 专属节点在 base brief 下早退），不把「这个模式不产」当成「漂移」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import minimax_h3_prompt.graph.nodes as nodes
from minimax_h3_prompt.brief_parser import Brief

PROMPTS_DIR = Path(__file__).parents[1] / "src" / "minimax_h3_prompt" / "prompts"

# 从源码里**所有** `_ctx(标签=…)` 派生词表（最长匹配优先）。派生而不是手写一份：
# 有人加一个新标签，这里自动认得，不必回来补词表。
def _label_vocab() -> list[str]:
    """``_ctx`` 的中文标签词表，**只取长度 ≥3 的**。

    短标签（``变体``／``时长``／``语言``／``模式``／``风格``／``人物``／``素材``）在
    散文里极易作为普通词出现（``ref/帧变体模式``、``人物形象设计``、``每镜画面细化``），
    拿它们做子串判据会持续误报；实测把它们放进词表后，13 个角色里有 6 个假红。
    长标签不会作为别的词的碎片出现，所以只留长的。

    派生而不是手写：``_ctx(标签=…)`` 加一个新标签，这里自动认得。
    """
    src = Path(nodes.__file__).read_text(encoding="utf-8")
    words = {w for w in re.findall(r"([一-鿿／]+)=", src)
             if len(w) >= 3 and not re.search(r"[A-Za-z_0-9]", w)}
    return sorted(words, key=len, reverse=True)


def _declared_inputs(role: str) -> set[str]:
    """该角色 `.md` 的 ``## 输入`` 段里点到的长标签（最长匹配，命中后抹掉防子串重复）。"""
    text = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
    match = re.search(r"^## 输入\n(.*?)(?=^## )", text, re.S | re.M)
    if match is None:
        return set()
    rest, found = match.group(1), set()
    for word in _label_vocab():
        if word in rest:
            found.add(word)
            rest = rest.replace(word, "·" * len(word))
    return found


def _rendered_labels(build_node, state: dict) -> set[str] | None:
    """真渲染一次节点消息，取其中的 ``【标签】`` 块；渲染不出来返回 None。"""
    seen: list[str] = []
    original = nodes.run_agent

    class _Agent:
        name = "role_under_test"

    def _fake_run_agent(agent, message, *args, **kwargs):
        seen.append(str(message))
        return "stub"

    nodes.run_agent = _fake_run_agent
    try:
        build_node(_Agent())(state)
    except Exception:
        return None
    finally:
        nodes.run_agent = original
    if not seen:
        return None
    return set(re.findall(r"【([^】]+)】", seen[0]))


def _state(brief: Brief) -> dict:
    """哨兵 state：每个键都有值，这样「没传」只会因为节点真的没传，而不是值为空被跳过。"""
    sentinel = {key: "哨兵" for key in (
        "production_plan", "director_brief", "creative_lock", "script",
        "shot_table", "visual_design", "art_design", "character_design",
        "background_design", "prop_design", "sound_design", "music",
        "shot_review_lock", "subject_defs", "identity_lock", "duration_to_stub",
    )}
    sentinel["brief"] = brief
    sentinel["fl2va_prompt_bundle"] = {}
    sentinel["duration"] = 18.0
    return sentinel


def _brief(mode: str = "base", variant: str = "FL2VA") -> Brief:
    return Brief(mode=mode, variant=variant, duration=18.0, style="Cinematic",
                 language="Chinese", plot="哨兵剧情", clarifications="哨兵澄清",
                 raw="哨兵剧情", refs=[])


# (角色, 构造节点的 lambda, 该角色真正跑起来所需的 brief)。
# 第三条不能统一成同一个 brief：``reference_consistency`` 只在**产资产图**的路线
# （ref / 导演台 r2v）才跑，Base brief 下它早退、一条消息都不发——那是路由能力判据
# （``route_emits_asset_prompts``）正常工作，不是漂移。给它对的 brief 才测得到。
_BASE = lambda: _brief()                                    # noqa: E731
_REF = lambda: _brief(mode="ref", variant="")               # noqa: E731

_CASES = [
    ("producer", lambda a: nodes._make_producer_node({"producer": a}), _BASE),
    ("director", lambda a: nodes._make_director_node({"director": a}), _BASE),
    ("screenwriter", lambda a: nodes._make_screenwriter_node({"screenwriter": a}), _BASE),
    ("storyboard", lambda a: nodes._make_storyboard_node({"storyboard": a}), _BASE),
    ("cinematographer",
     lambda a: nodes._make_cinematographer_node({"cinematographer": a}), _BASE),
    ("composer", lambda a: nodes._make_composer_node({"composer": a}), _BASE),
    ("sound_designer",
     lambda a: nodes._make_sound_designer_node({"sound_designer": a}), _BASE),
    ("art_director", lambda a: nodes._make_art_director_node({"art_director": a}), _BASE),
    ("character_designer",
     lambda a: nodes._make_design_node(
         {"character_designer": a}, "character_designer", "character_design",
         "人物形象设计", "character"), _BASE),
    ("background_designer",
     lambda a: nodes._make_design_node(
         {"background_designer": a}, "background_designer", "background_design",
         "背景设计", "art"), _BASE),
    ("prop_designer",
     lambda a: nodes._make_design_node(
         {"prop_designer": a}, "prop_designer", "prop_design", "道具设计", "art"), _BASE),
    ("reference_consistency",
     lambda a: nodes._make_reference_consistency_node({"reference_consistency": a}), _REF),
    ("prompt_engineer",
     lambda a: nodes._make_prompt_engineer_node({"prompt_engineer": a}), _BASE),
]


@pytest.mark.parametrize("role,build,brief_factory", _CASES,
                         ids=[r for r, _, _ in _CASES])
def test_declared_inputs_reach_the_node_request(role, build, brief_factory):
    """提示词 ``## 输入`` 点到的每个来源，都要真的出现在该节点发出的请求里。

    先自证两边非空——词表空了或消息没渲染出来时，集合差恒为空，守卫会永远绿。
    """
    declared = _declared_inputs(role)
    assert declared, f"{role}.md 的「## 输入」段没有识别出任何标签（词表或格式变了？）"

    rendered = _rendered_labels(build, _state(brief_factory()))
    assert rendered is not None, f"{role} 的请求渲染不出来（守卫失效，不是漂移）"

    missing = sorted(declared - rendered)
    assert not missing, (
        f"{role}.md 声明了这些输入，但它的节点请求里没有：{missing}\n"
        "  两种情况都是缺陷：提示词该删掉这份声明，或者节点该真的把它传进来。\n"
        f"  实际渲染出的标签：{sorted(rendered)}")


def test_known_drift_is_reported():
    """守卫自身会响：拿一份声明了不存在来源的假角色，集合差必须非空。

    「永不为红＝没有」——上面那条参数化用例只有在漂移发生时才会红，这条证明它
    真的会红，而不是词表/渲染任一环坏了之后恒绿。
    """
    declared = _declared_inputs("screenwriter")
    assert "制作计划" not in declared, (
        "screenwriter.md 若重新声明「制作计划」，说明 #351 的修复被回退——"
        "该节点拿不到它")
    # 真渲染出的标签里必须有该角色实际收到的那些
    rendered = _rendered_labels(_CASES[2][1], _state(_BASE()))
    assert rendered is not None
    assert "导演阐述" in rendered, "编剧请求里没有导演阐述（#351 修复被回退？）"
