"""资产图生图提示词三个节点接通阶段 1 链（issue #36）。

票面缺陷：人物／道具／场景三个生图提示词节点**已注册、state 字段已声明、持久化白名单也收了**，
却**从未被任何链调度**——折叠节点清单的注释声称它们「已折叠进并行组合节点」，而组合节点里
没有任何分支返回它们，注释在说反话。后果不是「少一个功能」而是**产出是错的**：取不到专门的
生图提示词时 ``generation.image_prompt_variants`` 回退到**设计正文**，于是喂进 ComfyUI 的
资产图提示词实际上是「人物／道具／场景的设计说明」。

四层，都只断言**外部行为**（链上真跑了什么、请求里真有什么、下游真取到了什么）：

1. **链上**：真图跑一遍阶段 1，三个节点各跑一次、请求里带的是**上游产物**（设计／美术统筹）；
2. **不回退**：下游 ``image_prompt_variants`` 取到的是生图提示词本身，不是设计正文；
3. **请求**：三个节点发出的真实请求带【画布】块（生图词表的指针指着它）；
4. **折叠清单**：``_FOLDED_NODES``／组合节点声明／链三者一致（注释不再说反话）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.generation import (
    IMAGE_PROFILE_DEFAULTS,
    image_canvas_size,
    image_prompt_variants,
)
from minimax_h3_prompt.graph import nodes, pipeline, roundtable

TOPIC = "秋日庭院里的橘猫"

# 节点请求的开头（``nodes._make_image_prompt_node`` 里那句要求）。用它把资产图节点与
# 关键帧节点、设计节点区分开——本文件考的是「请求真的发出去了」，不是角色提示词。
_ASSET_HEAD = "生成 Z-Image 和 Flux.2 两个可直接复制的生图提示词"

# 三类资产：节点名 → (请求里的资产名, 上游设计 state 键, 上游设计正文, 产物 state 键)
_ASSETS = (
    ("image_prompt_character", "人物", "character_design", "人物设计正文", "character_image_prompts"),
    ("image_prompt_prop", "道具", "prop_design", "道具设计正文", "prop_image_prompts"),
    ("image_prompt_scene", "场景", "background_design", "背景设计正文", "scene_image_prompts"),
)
# 产物标记：每个资产要认出**自己那三个标记**，才能证明三条路径没有互相串（都写进同一个键、
# 或三个键写同一份产物，在「只断言产非空」的测试下都是绿的）。
_MARK = "专属生图提示词"


def _brief() -> Brief:
    return Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC)


def _asset_json(kind: str) -> str:
    """资产图节点的假回复：两个模型键各一条正向提示词（形状同角色提示词的要求）。"""
    return json.dumps({
        "zimage": {"positive_prompt": f"{kind}{_MARK}", "instructions": ["粘贴到节点 10"]},
        "flux2": {"positive_prompt": f"{kind}{_MARK}（Flux.2 版）"},
    })


def _run_stage1(tmp_path, monkeypatch) -> tuple[dict, list[str]]:
    """真图跑一遍阶段 1（模型与圆桌都打桩），返回 ``(state, 全部出站请求)``。"""
    requests: list[str] = []

    # 各节点的假回复：按**请求正文**辨认调用方（本文件考的是请求，不是角色提示词）。
    # 设计／美术这两层回可辨认的正文，好让下游请求里能逐字认出「读到了谁」。
    design_replies = {
        "请完成人物形象设计": "人物设计正文",
        "请完成背景设计": "背景设计正文",
        "请完成道具设计": "道具设计正文",
        "请统筹并裁决最终美术设计": "美术统筹正文",
    }

    def fake_run_agent(agent, message):
        requests.append(message)
        for _node, kind, _design_field, _design, _out in _ASSETS:
            if _ASSET_HEAD in message and f"请为{kind}" in message:
                return _asset_json(kind)
        if "请为这一段" in message or "请只生成首帧" in message or "请只生成尾帧" in message:
            return json.dumps({
                "scene_anchor": "秋日庭院",
                "first": {"zimage": {"positive_prompt": "首帧画面"}},
                "continuity_constraints": ["保持同一只橘猫"],
            })
        for head, reply in design_replies.items():
            if message.startswith(head):
                return reply
        return "上游产物正文"

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    # 圆桌节点不走 nodes.run_agent（它自己调 roundtable._invoke）
    monkeypatch.setattr(roundtable, "_invoke", lambda role, model, msgs: "已锁定")
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    # 节点落盘目录不在本票题目里：别往仓库的 output/ 里写东西
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")

    state, _, _ = pipeline.run_stage1(
        _brief(),
        SimpleNamespace(roundtable_max_rounds=1, common_sense_qa=False),
    )
    return state, requests


# ---------------------------------------------------------------------------
# 1. 链上真的跑到这三个节点
# ---------------------------------------------------------------------------

def test_the_three_asset_prompt_nodes_run_on_the_stage1_chain(tmp_path, monkeypatch):
    """三个资产图节点各发一次请求，且**请求里带的是上游产物**。

    只断言「state 里有产物」不够：键可以由链条之外的任何东西写进去。这里断言的是
    **请求文本里含人物设计／道具设计／背景设计／美术统筹的正文**——那是「它跑在设计师与
    美术指导之后、读到的是它们这一轮的产出」这件事的物证（组合节点若排在美术指导之前，
    美术统筹那一块就是空的）。
    """
    state, requests = _run_stage1(tmp_path, monkeypatch)

    for node_name, kind, _design_field, design_text, out_field in _ASSETS:
        sent = [r for r in requests if _ASSET_HEAD in r and f"请为{kind}" in r]
        assert len(sent) == 1, f"{node_name} 没有（或不止一次）发出请求：{len(sent)} 次"
        request = sent[0]
        assert design_text in request, f"{node_name} 的请求里没有对应设计正文（读空了）"
        assert "美术统筹正文" in request, f"{node_name} 的请求里没有美术统筹（跑在美术指导之前了）"
        assert isinstance(state.get(out_field), dict), f"{node_name} 没有写回 {out_field}"


# ---------------------------------------------------------------------------
# 2. 资产图提示词不再回退到设计正文
# ---------------------------------------------------------------------------

def test_asset_prompts_are_not_the_design_text_anymore(tmp_path, monkeypatch):
    """下游取到的必须是生图提示词本身——回退到设计正文正是本票要消灭的形态。

    ``image_prompt_variants`` 里那句 ``item.get("positive_prompt") or state.get(设计字段)``
    是给**旧会话**留的兼容降级：链上没这个节点时它是唯一出路，于是一路把设计说明喂进
    ComfyUI。接通之后新跑的会话永远取不到那条分支，这里钉的就是它。
    """
    state, _ = _run_stage1(tmp_path, monkeypatch)

    variants = {(v.kind, v.model_family): v for v in image_prompt_variants(state, _brief())}
    assert set(variants) == {(k, f) for k in ("character", "prop", "scene") for f in ("zimage", "flux2")}

    for kind, zh in (("character", "人物"), ("prop", "道具"), ("scene", "场景")):
        for family in ("zimage", "flux2"):
            text = variants[(kind, family)].positive_prompt
            assert _MARK in text and zh in text, f"{kind}/{family} 取到的不是本资产的生图提示词：{text!r}"
            assert "设计正文" not in text, f"{kind}/{family} 又回退到设计正文了：{text!r}"


# ---------------------------------------------------------------------------
# 3. 请求里带【画布】块（生图词表的指针指着它）
# ---------------------------------------------------------------------------

def test_asset_prompt_request_carries_the_canvas_block(monkeypatch):
    """节点级：三个节点发出的请求都带【画布】块，**每个生图家族**的画幅都在里面。

    生图词表里写着「哪个模型是哪种画幅、尺寸是多少，一律以请求里的【画布】块为准」——
    角色提示词被加载到这些调用上，不带这块那条指针就是**悬空**的（#33 在帧节点踩过
    同一个坑）。

    尺寸与家族清单都从 ``IMAGE_PROFILE_DEFAULTS`` **现取**（经 ``image_canvas_size``）：
    测试里再抄一份数字就是第二份可写来源，换画布时它会静默失配——而且按家族清单遍历，
    将来加第三个生图家族时这条会自己跟着断言，不会漏。
    """
    requests: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: requests.append(message) or _asset_json("人物"))
    brief = _brief()
    agents = {"image_prompt_engineer": object()}

    for _node, kind, design_field, _design, out_field in _ASSETS:
        requests.clear()
        nodes._make_image_prompt_node(agents, kind, design_field, out_field)(
            {"brief": brief, design_field: "设计正文"})
        assert len(requests) == 1
        assert "【画布】" in requests[0], f"{kind} 的请求里没有【画布】块"

    assert IMAGE_PROFILE_DEFAULTS, "生图默认表是空的——【画布】块会退化成空块"
    for family in IMAGE_PROFILE_DEFAULTS:
        width, height = image_canvas_size(family)
        assert f"{width} × {height}" in requests[0], \
            f"【画布】块里没有 {family} 的画布尺寸"


# ---------------------------------------------------------------------------
# 4. 折叠节点清单与实际折叠一致
# ---------------------------------------------------------------------------

def test_folded_node_list_matches_the_real_folding():
    """折叠清单不许再说反话：**声明了折叠的组必须真的在链上跑**。

    这张清单曾经把三个资产图节点列为「已折叠」而没有任何组合节点跑它们（issue #36）。
    现在「折叠」只声明在 ``_PARALLEL_GROUPS`` 一处、``_FOLDED_NODES`` 由它现推，
    所以「清单 == 声明」是**定义**而不是待验证的性质（断言它等于把自己算一遍）；
    要盯的是声明与**链**的关系：

    - 每个声明了组合的组都得在链上——不在链上就是「声明了却没人跑」，与 #36 之前的
      形态同型（说折叠了、实际没接）；
    - 被折叠的子节点不许再作为独立图节点出现在链上——两处都跑等于同一个角色跑两遍。
    """
    for group, subs in pipeline._PARALLEL_GROUPS.items():
        assert group in pipeline._STAGE1_CHAIN, f"{group} 没接进阶段 1 链——它折叠的节点没人跑"
        for sub in subs:
            assert sub not in pipeline._STAGE1_CHAIN, f"{sub} 既在组合里又独立成节点（会跑两遍）"

    # 本票接的那三个：组合声明的就是这三个节点，且组合排在美术指导之后
    # （它要读美术统筹这一轮的产出）——「真的跑到」由上面那条真图用例另证。
    assert pipeline._PARALLEL_GROUPS["parallel_image_prompts"] == tuple(
        node_name for node_name, *_ in _ASSETS)
    assert pipeline._STAGE1_CHAIN.index("parallel_image_prompts") > \
        pipeline._STAGE1_CHAIN.index("art_director"), \
        "资产图提示词跑在美术指导之前——它要读美术统筹这一轮的产出"
