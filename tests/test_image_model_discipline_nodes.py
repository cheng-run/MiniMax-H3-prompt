"""资产图节点按模型取纪律块（issue #87 / 父 spec #84 D7）——节点与装配层。

与 `tests/test_image_model_discipline.py`（纪律文本与角色提示词加载口）分开：这里考的是
**三次出词请求真的带上了什么**。断言用记录型桩模型抓出站消息（含 system），因为它能证明
「模型的 system 消息里有什么」；打桩 ``run_agent`` 只能证明「节点调了谁」。

四条钉子：

1. 三档配成三个不同模型时，每次请求的 system 消息带**自己那一档**的纪律（交叉断言：
   也不许混进别档）；
2. 换模型不改 JSON 契约与锁死清单规则（system 里有、节点校验照样生效）；
3. 变异检验：删掉纪律块、或把模型换错，断言会红；
4. 装配不全时点名缺哪个模型，不静默回落到别档。
"""
from __future__ import annotations

import json
from typing import List

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from minimax_h3_prompt import agents as agents_module
from minimax_h3_prompt import image_config
from minimax_h3_prompt.agents import build_role_agents, image_prompt_agent_key
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.tools.image_prompt_discipline import (
    DEFAULT_IMAGE_MODEL,
    DISCIPLINE_MODELS,
    discipline_text,
)

TOPIC = "秋日庭院里的橘猫"

# 三个资产图节点：请求里的中文类别 → 上游设计 state 键 → 产物 state 键。
_ASSET_NODES = (
    ("人物", "character_design", "character_image_prompts"),
    ("道具", "prop_design", "prop_image_prompts"),
    ("场景", "background_design", "scene_image_prompts"),
)


class RecordingChatModel(BaseChatModel):
    """记录型桩模型：把每次调用的**真实出站消息**（含 system）存下来，再回固定 JSON。"""

    reply: str = "[]"
    seen: List[List[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "issue87-recording-stub"

    def _generate(self, messages: List[BaseMessage], stop=None,
                  run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _asset_reply(kind: str) -> str:
    """一条形状合规的资产回复：锁死清单条目逐字取自正文（节点侧不变式）。"""
    name = f"{kind}资产"
    return json.dumps([{
        "name": name,
        "positive_prompt": f"{name}站在纯色背景前，正面朝向镜头。",
        "negative_prompt": "",
        "locked_features": [name],
    }], ensure_ascii=False)


def _configure(tmp_path, monkeypatch, **models) -> None:
    """把生效配置换到临时 yaml 上（未给的档保持默认）。"""
    text = "".join(f"{kind}: {model}\n" for kind, model in models.items())
    path = tmp_path / "image.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)


def _ref_brief() -> Brief:
    # 资产图节点只在多参路线出词（issue #78）：走 base 的话三个节点直接早退。
    return Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot=TOPIC)


def _systems_by_kind(seen: List[List[BaseMessage]]) -> dict[str, str]:
    """把每次调用的出站消息拆成「按资产类别索引的 system 正文」。

    认类别靠**请求正文里的「请为X」**（节点自己的产物），不靠调用顺序——顺序一换，
    断言就会静默指错档。
    """
    systems: dict[str, str] = {}
    for messages in seen:
        system = next((str(m.content) for m in messages
                       if getattr(m, "type", "") == "system"), "")
        human = next((str(m.content) for m in messages
                      if getattr(m, "type", "") == "human"), "")
        for kind, _design, _out in _ASSET_NODES:
            if f"请为{kind}" in human:
                systems[kind] = system
    return systems


def _system_carries(system: str, model: str) -> bool:
    """系统消息是否带着该档纪律（到达性判据；变异检验也用它）。"""
    return discipline_text(model) in system


def _run_three_nodes(tmp_path, monkeypatch, **models) -> dict[str, str]:
    """按配置跑三个资产图节点，返回 {类别: 出站 system 消息}。"""
    _configure(tmp_path, monkeypatch, **models)
    model = RecordingChatModel()
    agents = build_role_agents(model, [], "ref", 5.0, "T2VA")
    brief = _ref_brief()
    systems: dict[str, str] = {}
    for kind, design_field, out_field in _ASSET_NODES:
        model.reply = _asset_reply(kind)
        model.seen = []
        node = nodes._make_image_prompt_node(agents, kind, design_field, out_field)
        node({"brief": brief, design_field: f"{kind}设计正文", "art_design": "美术统筹正文"})
        systems.update(_systems_by_kind(model.seen))
    return systems


def test_three_asset_requests_carry_their_own_model_discipline(tmp_path, monkeypatch):
    """三档配成三个不同模型：每次请求的 system 消息都带**自己那一档**的纪律。

    三档故意用三个不同模型（character=qwen / prop=z_image / scene=moss）——若实现是
    「一律用默认那档」这里必红；若实现成「按档取但取错」，交叉断言也必红。
    """
    systems = _run_three_nodes(tmp_path, monkeypatch,
                               character="qwen", prop="z_image", scene="moss")

    assert set(systems) == {"人物", "道具", "场景"}, f"没抓齐三次请求：{sorted(systems)}"
    expected = {"人物": "qwen", "道具": "z_image", "场景": "moss"}
    for kind, model in expected.items():
        assert _system_carries(systems[kind], model), \
            f"{kind} 档的请求没带上 {model} 的纪律"
        for other in DISCIPLINE_MODELS:
            if other == model:
                continue
            assert not _system_carries(systems[kind], other), \
                f"{kind} 档的请求里混进了 {other} 的纪律"


def test_every_asset_request_keeps_the_shared_json_contract(tmp_path, monkeypatch):
    """换模型不改输出格式：三档的 system 消息里都有同一份 JSON 契约与锁死清单规则。"""
    systems = _run_three_nodes(tmp_path, monkeypatch,
                               character="qwen", prop="z_image", scene="moss")

    for kind, system in systems.items():
        for key in ('"name"', '"positive_prompt"', '"negative_prompt"', '"locked_features"'):
            assert key in system, f"{kind} 档的 system 消息丢了输出字段 {key}"
        assert "逐字摘自" in system, f"{kind} 档的 system 消息丢了锁死清单规则"


def test_output_entries_are_identical_across_models(monkeypatch, tmp_path):
    """同一份模型回复在三档下归一出的条目**逐字段相同**：分支只影响写词，不改契约。

    issue #88 起条目多记了一个 ``image_model``（这条词按哪一档的纪律写成的），它当然
    随档而变——比的是**契约四键**，并且顺带钉住那份记录写的就是配置的那一档。
    """
    from minimax_h3_prompt import image_routing

    reply = _asset_reply("人物")
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: reply)
    agents = {image_prompt_agent_key(m): object() for m in DISCIPLINE_MODELS}

    entries = {}
    for model_key in DISCIPLINE_MODELS:
        _configure(tmp_path, monkeypatch, character=model_key)
        node = nodes._make_image_prompt_node(
            agents, "人物", "character_design", "character_image_prompts")
        entries[model_key] = node(
            {"brief": _ref_brief(), "character_design": "设计正文"})["character_image_prompts"]

    for model_key, value in entries.items():
        contract = [{k: v for k, v in entry.items() if k != image_routing.ENTRY_KEY}
                    for entry in value]
        baseline = [{k: v for k, v in entry.items() if k != image_routing.ENTRY_KEY}
                    for entry in entries[DEFAULT_IMAGE_MODEL]]
        assert contract == baseline, f"{model_key} 档归一出的条目与别档不同"
        assert image_routing.model_of(value[0]) == model_key, \
            f"{model_key} 档的条目记的模型不是它自己"


def test_locked_feature_invariant_still_bites_under_a_non_default_model(monkeypatch, tmp_path):
    """锁死清单不变式不因换模型而松：Qwen 档收到转述条目照样响。"""
    _configure(tmp_path, monkeypatch, character="qwen")
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: json.dumps([{
        "name": "甲", "positive_prompt": "甲站在庭院里，穿红裙",
        "locked_features": ["红裙女孩"],
    }], ensure_ascii=False))

    with pytest.raises(ValueError, match="原文"):
        nodes._make_image_prompt_node(
            {image_prompt_agent_key(m): object() for m in DISCIPLINE_MODELS},
            "人物", "character_design", "character_image_prompts")(
                {"brief": _ref_brief(), "character_design": "设计正文"})


# ---------------------------------------------------------------------------
# 变异检验：删掉纪律块、或把模型换错，断言必须红
# ---------------------------------------------------------------------------

def test_arrival_check_bites_when_the_discipline_block_is_dropped(tmp_path, monkeypatch):
    """删掉纪律块（装配处取空串）→ 到达性判据必须为假。

    永不为红的守卫等于没有守卫：这里直接破坏来源（``agents`` 模块里的 ``discipline_text``
    ——``load_role_prompt`` 用的就是它），证明上面的断言真的依赖接线，而不是只依赖配置。
    """
    monkeypatch.setattr(agents_module, "discipline_text", lambda *a, **k: "")
    systems = _run_three_nodes(tmp_path, monkeypatch,
                               character="qwen", prop="z_image", scene="moss")

    for kind in ("人物", "道具", "场景"):
        assert systems[kind], f"{kind} 档没有出站 system 消息"
        for model in DISCIPLINE_MODELS:
            assert not _system_carries(systems[kind], model), \
                "纪律块被删了，到达性判据却仍然为真"


def test_arrival_check_bites_when_the_model_is_swapped(tmp_path, monkeypatch):
    """把模型换错（配置写 z_image 而期望 qwen）→ 断言必须为假。

    这条防的是「按档取」写成「一律取默认」：配置改了对不上，断言立刻红。
    """
    systems = _run_three_nodes(tmp_path, monkeypatch,
                               character="z_image", prop="z_image", scene="moss")

    assert _system_carries(systems["人物"], "z_image")
    assert not _system_carries(systems["人物"], "qwen"), \
        "配置写成 z_image，却仍然带着 qwen 的纪律"


def test_missing_agent_variant_is_loud(monkeypatch, tmp_path):
    """只有部分变体时，节点要**点名缺哪个模型**，不能静默回落到别的档。

    静默回落正是本仓反复要防的形态：出的词按另一家的写法写，产物照样生成、不报错。
    """
    _configure(tmp_path, monkeypatch, character="moss")
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _asset_reply("人物"))
    node = nodes._make_image_prompt_node(
        {image_prompt_agent_key("z_image"): object()},
        "人物", "character_design", "character_image_prompts")

    with pytest.raises(ValueError, match="moss"):
        node({"brief": _ref_brief(), "character_design": "设计正文"})
