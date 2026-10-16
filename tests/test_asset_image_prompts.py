"""资产图生图提示词：N 资产 × Z-Image 单版（issue #40）。

issue #36 把三个资产图节点接进阶段 1 链；本票（#40，prefactor）把「固定 3 资产 × 2 模型」
泛化为「按剧本设计动态 N 资产 × 1 模型」——**Flux.2 版全链取消**（含测试口径：3×2 断言改
N×1），每个资产一版 Z-Image 提示词、带**语义名**（下游素材台账／注入卡要靠它对号）。

五层，都只断言**外部行为**（链上真跑了什么、请求里真有什么、下游真取到了什么）：

1. **链上**：真图跑一遍阶段 1，三个节点各跑一次、请求里带的是**上游产物**（设计／美术统筹）；
2. **N×1**：每资产一条提示词、带语义名；多角色资产按资产数出条数，不按模型数翻倍；
3. **不回退**：下游 ``image_prompt_variants`` 取到的是生图提示词本身，不是设计正文；
4. **请求**：三个节点发出的真实请求带【画布】块（生图词表的指针指着它）；
5. **折叠清单**：``_FOLDED_NODES``／组合节点声明／链三者一致（注释不再说反话）。
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
    render_asset_rows,
)
from minimax_h3_prompt.graph import nodes, pipeline, roundtable

TOPIC = "秋日庭院里的橘猫"

# 节点请求的开头（``nodes._make_image_prompt_node`` 里那句要求）。用它把资产图节点与
# 关键帧节点、设计节点区分开——本文件考的是「请求真的发出去了」，不是角色提示词。
_ASSET_HEAD = "各生成一条 Z-Image 可直接复制的生图提示词"

# 三类资产：节点名 → (请求里的资产名, 上游设计 state 键, 上游设计正文, 产物 state 键, 资产清单)
# 资产清单 = ((语义名, 正向提示词), ...)。人物给两条（多角色），道具／场景各一条（固定 3 资产场景）。
_ASSETS = (
    ("image_prompt_character", "人物", "character_design", "人物设计正文", "character_image_prompts",
     (("红裙女孩", "人物·红裙女孩专属生图提示词"), ("蓝衫少年", "人物·蓝衫少年专属生图提示词"))),
    ("image_prompt_prop", "道具", "prop_design", "道具设计正文", "prop_image_prompts",
     (("青铜钥匙", "道具·青铜钥匙专属生图提示词"),)),
    ("image_prompt_scene", "场景", "background_design", "背景设计正文", "scene_image_prompts",
     (("秋日庭院", "场景·秋日庭院专属生图提示词"),)),
)
# 产物标记：每个资产要认出**自己那条标记**，才能证明各资产路径没有互相串（都写进同一个键、
# 或各键写同一份产物，在「只断言产非空」的测试下都是绿的）。
_MARK = "专属生图提示词"


def _brief() -> Brief:
    return Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC)


def _assets_json(assets: tuple[tuple[str, str], ...]) -> str:
    """资产图节点的假回复：JSON **数组**，每个资产一项（issue #40 单版形态）。

    每项**故意带一个多余键**（``zimage``）：节点要把它归一成四键结构
    （name / positive_prompt / negative_prompt / instructions），多余的键不许进 state——
    夹具干净就咬不住「归一没做」的回归（变异检验实测过：不带脏键时放开归一照样绿）。
    """
    return json.dumps([
        {"name": name, "positive_prompt": prompt, "instructions": ["粘贴到节点 10"],
         "zimage": {"positive_prompt": "模型键残余不该进 state"}}
        for name, prompt in assets
    ])


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
        for _node, kind, _design_field, _design, _out, assets in _ASSETS:
            if _ASSET_HEAD in message and f"请为{kind}" in message:
                return _assets_json(assets)
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

    for node_name, kind, _design_field, design_text, out_field, assets in _ASSETS:
        sent = [r for r in requests if _ASSET_HEAD in r and f"请为{kind}" in r]
        assert len(sent) == 1, f"{node_name} 没有（或不止一次）发出请求：{len(sent)} 次"
        request = sent[0]
        assert design_text in request, f"{node_name} 的请求里没有对应设计正文（读空了）"
        assert "美术统筹正文" in request, f"{node_name} 的请求里没有美术统筹（跑在美术指导之前了）"
        stored = state.get(out_field)
        assert isinstance(stored, list), f"{node_name} 没有把资产列表写回 {out_field}"
        assert [item["name"] for item in stored] == [name for name, _prompt in assets], \
            f"{node_name} 写回的资产名与设计不符：{[item.get('name') for item in stored]}"


# ---------------------------------------------------------------------------
# 2. N×1：每资产一条 Z-Image 提示词、带语义名，不按模型数翻倍
# ---------------------------------------------------------------------------

def test_each_asset_gets_exactly_one_named_zimage_prompt(tmp_path, monkeypatch):
    """下游取到的是 N 条（每资产一条），不是 N×2——Flux.2 版已全链取消。

    语义名必须原样到达：素材台账／注入卡靠它对号（issue #40 的 N 化不止是条数，
    还要认得出「这条是谁的」）。同时钉住归一结构**只有四键**——模型键（zimage/flux2）
    不许再混进 state 形状里。
    """
    state, _ = _run_stage1(tmp_path, monkeypatch)

    # 预期是**字面量**（不从 _ASSETS 现算）：从夹具推导就与实现同构，形状错了也一起绿。
    expected = {
        ("character", "红裙女孩"), ("character", "蓝衫少年"),
        ("prop", "青铜钥匙"), ("scene", "秋日庭院"),
    }
    variants = image_prompt_variants(state)
    assert {(v.kind, v.name) for v in variants} == expected
    assert len(variants) == len(expected), "有资产出了多条（模型维度又回来了）"

    by_key = {(v.kind, v.name): v for v in variants}
    zh_to_kind = {"人物": "character", "道具": "prop", "场景": "scene"}
    for _node, zh, _d, _t, _out, assets in _ASSETS:
        for name, prompt in assets:
            item = by_key[(zh_to_kind[zh], name)]
            assert item.positive_prompt == prompt, f"{zh}/{name} 取到的不是本资产的提示词"
            assert item.profile_id == IMAGE_PROFILE_DEFAULTS["zimage"].profile_id, \
                f"{zh}/{name} 的 profile 不是 Z-Image 档"
    for _node, _k, _d, _t, out_field, _a in _ASSETS:
        for entry in state[out_field]:
            assert set(entry) == {"name", "positive_prompt", "negative_prompt", "instructions"}, \
                f"{out_field} 的条目混入了四键之外的字段：{sorted(entry)}"


# ---------------------------------------------------------------------------
# 3. 不回退到设计正文；旧 dict 形状 state 仍可读（读旧会话）
# ---------------------------------------------------------------------------

def test_asset_prompts_are_not_the_design_text_anymore(tmp_path, monkeypatch):
    """下游取到的必须是生图提示词本身——回退到设计正文正是 issue #36 要消灭的形态。

    ``image_prompt_variants`` 里那句 ``positive_prompt or state.get(设计字段)``
    是给**旧会话**留的兼容降级：链上没这个节点时它是唯一出路，于是一路把设计说明喂进
    ComfyUI。接通之后新跑的会话永远取不到那条分支，这里钉的就是它。
    """
    state, _ = _run_stage1(tmp_path, monkeypatch)

    variants = image_prompt_variants(state)
    for v in variants:
        assert _MARK in v.positive_prompt, f"{v.kind}/{v.name} 取到的不是生图提示词：{v.positive_prompt!r}"
        assert "设计正文" not in v.positive_prompt, f"{v.kind}/{v.name} 又回退到设计正文了"


def test_legacy_dict_state_still_reads_one_prompt_per_kind(monkeypatch):
    """旧 session-state 的 dict 形状（``{zimage:…, flux2:…}``）仍可读，但**只出 Z-Image 一条**。

    Flux.2 版全链取消连旧数据也一并降级：读旧会话不再吐出 Flux.2 行（那是已取消的产物）。
    语义名在旧形状里不存在，退化为类型标签——旧会话拿不到的东西不硬造。
    """
    legacy = {
        "character_image_prompts": {
            "zimage": {"positive_prompt": "旧人物提示词"},
            "flux2": {"positive_prompt": "旧人物提示词（Flux.2 版）"},
        },
        "prop_image_prompts": {"zimage": {"positive_prompt": "旧道具提示词"}},
        "scene_image_prompts": {"zimage": {"positive_prompt": "旧场景提示词"}},
    }
    variants = image_prompt_variants(legacy)

    assert [(v.kind, v.name) for v in variants] == [
        ("character", "人物"), ("prop", "道具"), ("scene", "场景"),
    ]
    assert [v.positive_prompt for v in variants] == ["旧人物提示词", "旧道具提示词", "旧场景提示词"]
    assert all(v.profile_id == IMAGE_PROFILE_DEFAULTS["zimage"].profile_id for v in variants)


# ---------------------------------------------------------------------------
# 4. 请求里带【画布】块（生图词表的指针指着它）
# ---------------------------------------------------------------------------

def test_asset_prompt_request_carries_the_canvas_block(monkeypatch):
    """节点级：三个节点发出的请求都带【画布】块，**每个生图家族**的画幅都在里面。

    生图词表里写着「哪个模型是哪种画幅、尺寸是多少，一律以请求里的【画布】块为准」——
    角色提示词被加载到这些调用上，不带这块那条指针就是**悬空**的（#33 在帧节点踩过
    同一个坑）。资产链只写 Z-Image，但【画布】块是词表指针的落点，行来源是
    ``IMAGE_PROFILE_DEFAULTS`` 整表（与帧节点共用一块），不按消费者裁剪。

    尺寸与家族清单都从 ``IMAGE_PROFILE_DEFAULTS`` **现取**（经 ``image_canvas_size``）：
    测试里再抄一份数字就是第二份可写来源，换画布时它会静默失配——而且按家族清单遍历，
    将来加第三个生图家族时这条会自己跟着断言，不会漏。
    """
    requests: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: requests.append(message) or _assets_json((("甲", "提示词"),)))
    brief = _brief()
    agents = {"image_prompt_engineer": object()}

    for _node, kind, design_field, _design, out_field, _assets in _ASSETS:
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


def _canvas_request(monkeypatch, canvases: dict) -> str:
    """跑一次资产图节点，返回它真正发出去的请求串（请求是生图侧的缝，断言打在串上）。"""
    captured: list[str] = []

    def fake_run_agent(agent, message):
        captured.append(message)
        return _assets_json((("甲", "提示词"),))

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    brief = Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC,
                  canvases=canvases)
    agents = {"image_prompt_engineer": object()}
    nodes._make_image_prompt_node(agents, "人物", "character_design", "character_image_prompts")(
        {"brief": brief, "character_design": "设计正文"})
    assert captured, "节点没有发出请求"
    return captured[0]


def test_canvas_block_carries_the_user_selected_canvas(monkeypatch):
    """选定值进请求【画布】块（AC1）：显示名与尺寸逐字同取 ``canvas_display``，分套指示随行。

    期望串是**字面量**（700×1300 约分为 7:13）：从实现函数现算就与实现同构，格式错了
    也一起绿。系统默认的 640 × 1280 不许再出现——「取代系统钦定的推荐尺寸」的可证面。
    """
    request = _canvas_request(monkeypatch, {"zimage": (700, 1300), "flux2": (1024, 1024)})

    assert "7:13 · 700 × 1300（近竖）" in request, "请求【画布】块没带 Z-Image 的选定画布"
    assert "1:1 · 1024 × 1024（近方）" in request, "请求【画布】块没带 Flux.2 的选定画布"
    assert "构图用竖幅套" in request, "Z-Image 行没有构图分套指示"
    assert "构图用正方套" in request, "Flux.2 行没有构图分套指示"
    assert "640 × 1280" not in request, "选定了画布却还带着系统钦定的推荐尺寸"


def test_canvas_block_maps_composition_set_by_aspect_family_not_model(monkeypatch):
    """Q21(c)：构图分套绑**画幅族**、不绑模型——给 Z-Image 选近方形比例，它就用正方套。

    这是起草裁定原话的可证面（「你给 Z-Image 选近方形比例时仍用正方套」）：改动前
    分套潜规则是「zimage=竖幅、flux2=正方」，用户换比例后再说反话。族外（此处 Flux.2
    选的 9:21 极端竖）必须降通用构图词，且**明说不用两套**——只写「通用」两个字模型
    仍可能硬套竖幅措辞。
    """
    request = _canvas_request(monkeypatch, {"zimage": (1024, 1024), "flux2": (540, 1260)})

    assert "构图用正方套" in request, "Z-Image 选了 1:1，构图却没落到正方套（还绑着模型旧形状）"
    assert "构图用竖幅套" not in request, "没人选竖幅族画布，请求里不该出现竖幅套"
    assert "通用构图词" in request, "9:21 极端比例没降通用构图词"
    assert "不用竖幅/正方任一套" in request, "族外降级没说清「不硬塞任一套」，模型会硬套"


# ---------------------------------------------------------------------------
# 5. 出站交付：向导展示 + 产物落盘两处都拿得到全部资产提示词（issue #40 AC2）
# ---------------------------------------------------------------------------

def test_wizard_delivers_all_asset_prompts_on_screen_and_on_disk(tmp_path, monkeypatch, capsys):
    """向导出站口：每条资产提示词都能在**打印与落盘**两处拿到（AC2）。

    这是「提示词产出没展示」那类缺陷的守卫（「规则在、接线不在」家族第 7 次同族的
    同型现场）：三个节点产了、用户拿不到＝白产。落盘文件名与非向导路径同名
    （character-prompt.md 等）——两处各起一个名字就是第二份可写来源，迟早漂开。
    """
    state, _ = _run_stage1(tmp_path, monkeypatch)
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    from minimax_h3_prompt.ui import wizard

    wizard._show_asset_prompts(state, generation_dir)

    out = capsys.readouterr().out
    for _node, _zh, _d, _t, _out, assets in _ASSETS:
        for name, prompt in assets:
            assert name in out, f"展示里没有资产「{name}」"
            assert prompt in out, f"展示里没有资产「{name}」的提示词"
    for filename in ("character-prompt.md", "prop-prompt.md", "scene-prompt.md"):
        path = generation_dir / filename
        assert path.is_file(), f"落盘缺 {filename}"
    for _node, _zh, _d, _t, _out, assets in _ASSETS:
        filename = {
            "character_image_prompts": "character-prompt.md",
            "prop_image_prompts": "prop-prompt.md",
            "scene_image_prompts": "scene-prompt.md",
        }[_out]
        text = (generation_dir / filename).read_text(encoding="utf-8")
        for name, prompt in assets:
            assert name in text and prompt in text, f"{filename} 里缺资产「{name}」"


def test_fl2va_result_keeps_asset_prompts_for_delivery(tmp_path, monkeypatch):
    """FL2VA 结果不许丢资产提示词（AC2「全部」）：帧变体也要拿得到资产图生图提示词。

    ``result_from_state`` 的 FL2VA 分支曾把 ``image_prompts`` 直接置空——三个节点产了、
    结果对象里却没有，下游谁也拿不到。两条渲染分支（有无首尾帧 bundle）都要带资产键。
    """
    state, _ = _run_stage1(tmp_path, monkeypatch)
    # result_from_state 要求 script/video 齐全——阶段 1 只产到设计层，这里补上阶段 2 的产物。
    state["final_prompt"] = "最终视频提示词"
    state["fl2va_prompt_bundle"] = {
        "scene_anchor": "秋日庭院",
        "first": {"zimage": {"positive_prompt": "首帧：橘猫蹲在青石上"}},
        "last": {"zimage": {"positive_prompt": "尾帧：橘猫抬头看红枫"}},
        "continuity_constraints": ["保持同一只橘猫"],
    }
    brief = Brief(mode="base", variant="FL2VA", duration=5.0, style="写实", plot=TOPIC)
    from minimax_h3_prompt.generation import render_generation, result_from_state

    result = result_from_state(
        state, brief, generation_id="GEN001", topic_id="t", project_id="p")
    assert result.image_prompts, "FL2VA 分支把资产提示词丢了"

    rendered = render_generation(result)
    for key in ("character", "prop", "scene"):
        assert key in rendered, f"FL2VA 渲染缺 {key} 键"
    for _node, _zh, _d, _t, _out, assets in _ASSETS:
        key = {"character_image_prompts": "character",
               "prop_image_prompts": "prop",
               "scene_image_prompts": "scene"}[_out]
        for name, prompt in assets:
            assert name in rendered[key] and prompt in rendered[key], \
                f"FL2VA 渲染的 {key} 里缺资产「{name}」"


# ---------------------------------------------------------------------------
# 6. 折叠节点清单与实际折叠一致
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

    # 本链上的那三个：组合声明的就是这三个节点，且组合排在美术指导之后
    # （它要读美术统筹这一轮的产出）——「真的跑到」由上面那条真图用例另证。
    assert pipeline._PARALLEL_GROUPS["parallel_image_prompts"] == tuple(
        node_name for node_name, *_ in _ASSETS)
    assert pipeline._STAGE1_CHAIN.index("parallel_image_prompts") > \
        pipeline._STAGE1_CHAIN.index("art_director"), \
        "资产图提示词跑在美术指导之前——它要读美术统筹这一轮的产出"


# ---------------------------------------------------------------------------
# 7. 产物画布行：选定值写进产物，显示串与请求同取（issue #42 AC1/AC3）
# ---------------------------------------------------------------------------

def test_products_show_the_user_selected_canvas(tmp_path, monkeypatch):
    """产物的画布行展示**用户选定**画布，显示串取 ``canvas_display``（与请求同源）。

    「推荐尺寸」措辞随本票退役（CONTEXT.md 画布词条的 Avoid 词：那是系统钦定时代
    的渲染层措辞）：改动前两处排版各写一份——请求块画「640 × 1280（竖幅）」、
    产物写「推荐尺寸：640 × 1280」——正是 AC3 点名的两个来源。
    """
    state, _ = _run_stage1(tmp_path, monkeypatch)

    variants = image_prompt_variants(state, {"zimage": (700, 1300)})
    assert variants, "资产产物是空的"
    for v in variants:
        assert (v.width, v.height) == (700, 1300), f"{v.name} 的产物尺寸不是选定值"

    rendered = render_asset_rows(list(variants), variants[0].kind)
    assert "画布：`7:13 · 700 × 1300（近竖）`" in rendered, "产物没有画布行（或显示串不是单一来源那份）"
    assert "推荐尺寸" not in rendered, "「推荐尺寸」措辞还活着——系统钦定时代的旧标签"


def test_entry_width_height_cannot_beat_the_selected_canvas(tmp_path, monkeypatch):
    """资产条目里混进的 width/height（四键契约外残留）压不过选定画布（issue #42 复审）。"""
    state, _ = _run_stage1(tmp_path, monkeypatch)
    for entry in state["character_image_prompts"]:
        entry["width"] = 111
        entry["height"] = 222

    variants = image_prompt_variants(state, {"zimage": (700, 1300)})
    assert all((v.width, v.height) == (700, 1300) for v in variants), \
        "条目残留的尺寸赢了用户选定"
