"""生图模型路由接进资产图节点（issue #88 / 父 spec #84 D8）。

**接缝**：资产图节点的外部行为——**出站请求**（记录型桩模型抓 system 消息，与 issue #87
同一手法）与**写回 state 的条目**。断言的是「这张真的按 MOSS 的纪律重出了吗」「未命中
是不是照旧只发一次」「人选的模型有没有被自动规则冲掉」；纯判定的规格在
``tests/test_image_model_routing.py``，这里只钉接线。

两条路线里只有多参跑资产图节点（issue #78），故全部用例走 ref。
"""
from __future__ import annotations

import json
from typing import List

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from minimax_h3_prompt import image_config, image_routing
from minimax_h3_prompt.agents import build_role_agents, image_prompt_agent_key
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.tools.image_prompt_discipline import DISCIPLINE_MODELS, discipline_text

TOPIC = "便利店冰柜里的橘子汽水"


class RoutingChatModel(BaseChatModel):
    """记录型桩模型：记下每次出站消息，按**请求是「多资产」还是「单条重出」**回不同的 JSON。"""

    assets: List[tuple[str, str]] = []
    seen: List[List[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "issue88-routing-stub"

    def _generate(self, messages: List[BaseMessage], stop=None,
                  run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        human = next((str(m.content) for m in messages
                      if getattr(m, "type", "") == "human"), "")
        # 单条重出（节点按路由结果补一次）：只回被点名的那一条；多资产请求回全部。
        picked = [(n, p) for n, p in self.assets if f"资产「{n}」" in human] or list(self.assets)
        reply = json.dumps([{
            "name": name,
            "positive_prompt": prompt,
            "negative_prompt": "",
            "locked_features": [name],
        } for name, prompt in picked], ensure_ascii=False)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])


def _configure(tmp_path, monkeypatch, **models) -> None:
    text = "".join(f"{kind}: {model}\n" for kind, model in models.items())
    path = tmp_path / "image.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)


def _ref_brief() -> Brief:
    return Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot=TOPIC)


def _prop_node(model: RoutingChatModel, assets: list[tuple[str, str]]):
    model.assets = assets
    agents = build_role_agents(model, [], "ref", 5.0, "T2VA")
    return nodes._make_image_prompt_node(agents, "道具", "prop_design", "prop_image_prompts")


def _system_of(messages: List[BaseMessage]) -> str:
    return next((str(m.content) for m in messages
                 if getattr(m, "type", "") == "system"), "")


def _human_of(messages: List[BaseMessage]) -> str:
    return next((str(m.content) for m in messages
                 if getattr(m, "type", "") == "human"), "")


# ---------------------------------------------------------------------------
# 自动路由：命中关键词的那一张换 MOSS 重出
# ---------------------------------------------------------------------------

def test_design_keyword_reroutes_that_asset_to_moss(tmp_path, monkeypatch, capsys):
    """设计正文提到「包装」：这一张按 MOSS 的纪律**重出**，并在终端说明命中词。"""
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [("橘子汽水", "橘子汽水：一瓶橘色汽水，白底")])

    out = node({"brief": _ref_brief(),
                "prop_design": "道具设计：玻璃汽水瓶，瓶身包装印着品牌色块"})

    entry = out["prop_image_prompts"][0]
    assert image_routing.assignment_of(entry) == {
        "model": "moss", "source": "auto", "keyword": "包装"}

    # 出站两次：先按配置档（Z-Image）出词，命中后按 MOSS 重出这一条。
    assert len(model.seen) == 2, f"期望「先出词、再按 MOSS 重出」，实际调用 {len(model.seen)} 次"
    assert discipline_text("z_image") in _system_of(model.seen[0])
    assert discipline_text("moss") in _system_of(model.seen[1])
    assert discipline_text("z_image") not in _system_of(model.seen[1]), \
        "重出用的是 MOSS 档，请求里却还带着 Z-Image 的纪律"
    # 重出请求只点名这一条资产（不许顺手把整类重出，那会冲掉别张的词与成本）
    assert "橘子汽水" in _human_of(model.seen[1])
    assert "命中『包装』" in capsys.readouterr().out


def test_prompt_keyword_alone_also_reroutes(tmp_path, monkeypatch):
    """设计正文没提，但**该资产提示词**里有英文针（``label``）→ 同样切 MOSS。

    这条钉的是「该资产提示词」也是判据：只扫设计正文时，第一轮就已经写进英文
    「label」的资产会被漏掉，而它恰恰是最需要文字能力的那一类。
    """
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [("橘子汽水", "橘子汽水：A glass bottle with a printed label on the body")])

    out = node({"brief": _ref_brief(), "prop_design": "道具设计：玻璃汽水瓶"})

    entry = out["prop_image_prompts"][0]
    assert image_routing.assignment_of(entry)["keyword"] == "标签"
    assert image_routing.model_of(entry) == "moss"


def test_untouched_asset_keeps_the_configured_model(tmp_path, monkeypatch):
    """未命中的照旧：只发一次请求，模型是配置里那一档（不白花第二次调用）。"""
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [("青铜钥匙", "青铜钥匙：一把纯色背景的旧钥匙")])

    out = node({"brief": _ref_brief(), "prop_design": "道具设计：一把旧钥匙"})

    entry = out["prop_image_prompts"][0]
    assert image_routing.assignment_of(entry) == {
        "model": "z_image", "source": "config", "keyword": ""}
    assert len(model.seen) == 1, "未命中的资产不该再发一次请求"


def test_only_the_hit_asset_is_rerouted_inside_one_kind(tmp_path, monkeypatch):
    """同一类里只切命中的那一张：出站顺序与条目各自对号，不整类连坐。"""
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [
        ("橘子汽水", "橘子汽水：A glass bottle with a printed label on the body"),
        ("青铜钥匙", "青铜钥匙：一把纯色背景的旧钥匙"),
    ])

    out = node({"brief": _ref_brief(), "prop_design": "道具设计：两件小道具"})

    by_name = {entry["name"]: entry for entry in out["prop_image_prompts"]}
    assert image_routing.model_of(by_name["橘子汽水"]) == "moss"
    assert image_routing.model_of(by_name["青铜钥匙"]) == "z_image"
    regenerated = [m for m in model.seen if "资产「橘子汽水」" in _human_of(m)]
    assert len(regenerated) == 1, "命中的那张应当只被重出一次"
    assert not any("资产「青铜钥匙」" in _human_of(m) for m in model.seen)


# ---------------------------------------------------------------------------
# 手动选择：优先于自动规则，且经得起阶段 1 重跑
# ---------------------------------------------------------------------------

def test_manual_choice_beats_the_keyword_rule(tmp_path, monkeypatch, capsys):
    """人选的模型压过自动规则：命中「包装」也照人选出词，终端说清是手动指定。"""
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [("橘子汽水", "橘子汽水：一瓶橘色汽水")])
    previous = [{
        "name": "橘子汽水", "positive_prompt": "旧词", "negative_prompt": "",
        "locked_features": ["橘子汽水"],
        "image_model": {"model": "qwen", "source": "manual", "keyword": ""},
    }]

    out = node({"brief": _ref_brief(),
                "prop_design": "道具设计：瓶身包装印着品牌色块",
                "prop_image_prompts": previous})

    entry = out["prop_image_prompts"][0]
    assert image_routing.assignment_of(entry) == {
        "model": "qwen", "source": "manual", "keyword": ""}
    # 重出用的是人选的 Qwen 档（而不是配置档、也不是自动规则给的 MOSS）
    assert discipline_text("qwen") in _system_of(model.seen[-1])
    assert "手动指定" in capsys.readouterr().out


def test_manual_choice_survives_a_stage1_rerun(tmp_path, monkeypatch):
    """重跑节点时人的选择不许被冲掉——否则一次设定级重跑就把菜单里的选择还原成自动规则。"""
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [("青铜钥匙", "青铜钥匙：一把纯色背景的旧钥匙")])
    previous = [{
        "name": "青铜钥匙", "positive_prompt": "旧词", "negative_prompt": "",
        "locked_features": ["青铜钥匙"],
        "image_model": {"model": "moss", "source": "manual", "keyword": ""},
    }]

    out = node({"brief": _ref_brief(), "prop_design": "道具设计：一把旧钥匙",
                "prop_image_prompts": previous})

    assert image_routing.model_of(out["prop_image_prompts"][0]) == "moss"
    assert discipline_text("moss") in _system_of(model.seen[-1])


# ---------------------------------------------------------------------------
# 单条重出（素材循环的「改这张的模型」与自动路由共用这一条路）
# ---------------------------------------------------------------------------

def test_regenerate_asset_prompt_returns_only_the_named_asset(tmp_path, monkeypatch):
    """单条重出只回被点名的那一条：整类重出会冲掉同一类别的其它资产。"""
    model = RoutingChatModel()
    model.assets = [("橘子汽水", "橘子汽水：一瓶橘色汽水")]
    agents = build_role_agents(model, [], "ref", 5.0, "T2VA")

    entry = nodes.regenerate_asset_prompt(
        agents, "prop", "橘子汽水",
        {"prop_design": "道具设计：玻璃汽水瓶"}, _ref_brief(), model="moss")

    assert entry["name"] == "橘子汽水"
    assert discipline_text("moss") in _system_of(model.seen[0])
    assert "资产「橘子汽水」" in _human_of(model.seen[0])


def test_regenerate_asset_prompt_is_loud_when_the_name_comes_back_wrong():
    """模型回了别的资产 → 响：写进这张的模型记录会属于另一张词，静默接受就是串词。"""
    agents = {image_prompt_agent_key(key): object() for key in DISCIPLINE_MODELS}

    def fake_run_agent(agent, message):
        return json.dumps([{"name": "青铜钥匙", "positive_prompt": "钥匙",
                            "locked_features": ["钥匙"]}], ensure_ascii=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(nodes, "run_agent", fake_run_agent)
        with pytest.raises(ValueError, match="橘子汽水"):
            nodes.regenerate_asset_prompt(
                agents, "prop", "橘子汽水",
                {"prop_design": "道具设计"}, _ref_brief(), model="z_image")


# ---------------------------------------------------------------------------
# 变异检验：把路由来源拆掉，上面的断言必须红
# ---------------------------------------------------------------------------

def test_routing_assertions_depend_on_the_keyword_source(tmp_path, monkeypatch):
    """把关键词命中函数换成恒 None → 命中用例那一条就不再是 MOSS。

    永不为红的守卫等于没有守卫：这条证明上面的断言真的依赖词表与判定，而不是碰巧
    因为配置写成 moss 才绿。
    """
    _configure(tmp_path, monkeypatch, prop="z_image")
    monkeypatch.setattr(image_routing, "text_capability_hit", lambda *a, **k: None)
    model = RoutingChatModel()
    node = _prop_node(model, [("橘子汽水", "橘子汽水：一瓶橘色汽水")])

    out = node({"brief": _ref_brief(),
                "prop_design": "道具设计：瓶身包装印着品牌色块"})

    assert image_routing.model_of(out["prop_image_prompts"][0]) == "z_image"
    assert len(model.seen) == 1


def test_kind_to_state_field_mappings_stay_aligned():
    """两张「类别 → state 键」的映射必须对得上：路由按它读上一次的条目与设计正文。

    ``_IMAGE_PROMPTS_FIELD_BY_KIND`` 是 ``IMAGE_KINDS`` 与 ``ASSET_PROMPT_KEYS`` **按位置
    配对**出来的：任一侧改了顺序，配对就会静默错位——路由会去读另一类资产的旧条目，
    于是人的手动选择落在错的张上、或者干脆读不到（两者都不报错）。
    """
    assert nodes._IMAGE_PROMPTS_FIELD_BY_KIND == {
        "character": "character_image_prompts",
        "prop": "prop_image_prompts",
        "scene": "scene_image_prompts",
    }
    assert nodes.ASSET_DESIGN_FIELDS == {
        "character": "character_design",
        "prop": "prop_design",
        "scene": "background_design",
    }


def test_kind_level_design_hit_switches_every_asset_of_that_kind(tmp_path, monkeypatch):
    """设计正文是**整类共用**的一段文字：它命中时，这一类的每一张都切 MOSS。

    这条钉的是规则的**粒度**，不是它的意外（nodes 的注释与 config/image.yaml 都这么写）：
    拿不到「设计里的哪一句属于哪一张」，所以设计正文侧的命中按类生效；只有**提示词里**
    命中的那一张才单张切换（见 ``test_only_the_hit_asset_is_rerouted_inside_one_kind``）。
    粒度是真事实——把它写死，将来改判据时不会悄悄改掉用户看到的成本（每张多一次调用）。
    """
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [
        ("橘子汽水", "橘子汽水：一瓶橘色汽水"),
        ("青铜钥匙", "青铜钥匙：一把纯色背景的旧钥匙"),
    ])

    out = node({"brief": _ref_brief(),
                "prop_design": "道具设计：汽水瓶（瓶身包装印着色块）与一把旧钥匙"})

    assert {entry["name"]: image_routing.model_of(entry)
            for entry in out["prop_image_prompts"]} == {"橘子汽水": "moss", "青铜钥匙": "moss"}
    assert len(model.seen) == 3, "首轮一次 + 两张各重出一次"


def test_base_route_never_routes_or_calls(tmp_path, monkeypatch):
    """路线守卫（父 spec #77）：首尾帧路线连路由都不该走到——不出词、不写条目。"""
    _configure(tmp_path, monkeypatch, prop="z_image")
    model = RoutingChatModel()
    node = _prop_node(model, [("橘子汽水", "一瓶橘色汽水")])

    base_brief = Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC)
    out = node({"brief": base_brief, "prop_design": "道具设计：瓶身包装印着品牌色块"})

    assert out == {}
    assert model.seen == []
