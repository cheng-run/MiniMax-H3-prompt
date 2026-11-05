"""设计端资产清单与出词集合校验（issue #173 / spec #166 D）。

问题陈述（2026-11-04 r2v 实跑）：设计端说「本轮不新增人物母版」时，出词节点照样按
人物设计正文凑条目，人物类拿场景建置图充数；清单与产出不一致时谁都不报，错位一路
带进台账，直到用户交回第一张图才发现。

本文件的判据只打在**外部行为**上：节点返回的 state 键、真正发给模型的请求串、终端
打印的那一行、抛出的错误。清单块的解析与集合比较都是纯函数，但测试不直接调它们——
它们只通过设计节点与出词节点露出。
"""
from __future__ import annotations

import json

import pytest

from minimax_h3_prompt import image_config
from minimax_h3_prompt.agents import image_prompt_agent_key
from minimax_h3_prompt.asset_manifest import MANIFEST_FENCE
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.kinds import ASSET_MANIFEST_FIELDS, ASSET_MANIFEST_KEYS
from minimax_h3_prompt.session_store import load_session, save_session

TOPIC = "秋日庭院里的橘猫"
MANIFEST_KEY = ASSET_MANIFEST_FIELDS["character"]


def _ref_brief() -> Brief:
    """多参路线：产资产图提示词（spec #166 D 只在这条硬约束上成立）。"""
    return Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot=TOPIC)


def _base_brief() -> Brief:
    """首尾帧路线：不产资产图提示词，设计端不该被要求附清单。"""
    return Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC)


def _manifest_block(names: list[str]) -> str:
    # 围栏引用常量而不是重抄字面量：标签一改，提示、解析与这份夹具一起动
    # （CODING_STANDARDS §四；``asset_manifest._BLOCK_RE`` 同样从它派生）。
    return "\n```" + MANIFEST_FENCE + "\n" + json.dumps(
        {"assets": names}, ensure_ascii=False) + "\n```\n"


def _design_reply(body: str, names: list[str]) -> str:
    return body + _manifest_block(names)


def _character_design_node(monkeypatch, replies: list[str]):
    """按调用顺序返回假回复，返回 ``(节点, 全部出站请求)``。"""
    calls: list[str] = []

    def fake_run_agent(agent, message):
        calls.append(message)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_design_node(
        {"character_designer": object()}, "character_designer",
        "character_design", "人物形象设计", "character")
    return node, calls


def _image_agents() -> dict:
    """三档生图模型的占位 agent：节点按 config/image.yaml 取其中一档。"""
    return {image_prompt_agent_key(key): object() for key in image_config.IMAGE_MODEL_KEYS}


def _entries_json(names: list[str]) -> str:
    return json.dumps([
        {"name": name, "positive_prompt": f"{name}：站在庭院里",
         "negative_prompt": "", "locked_features": [f"{name}：站在庭院里"]}
        for name in names
    ], ensure_ascii=False)


def _run_image_node(monkeypatch, state: dict):
    """跑一次人物资产图出词节点，返回 ``(节点返回值, 出站请求)``。"""
    calls: list[str] = []
    names = list(state.get(MANIFEST_KEY) or [])

    def fake_run_agent(agent, message):
        calls.append(message)
        return _entries_json(names)

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_image_prompt_node(
        _image_agents(),
        "人物", "character_design", "character_image_prompts")
    return node, calls


# ---------------------------------------------------------------------------
# 1. 设计节点：清单块与正文分开落键
# ---------------------------------------------------------------------------

def test_design_node_splits_the_manifest_out_of_the_body(monkeypatch):
    """清单块进清单键、正文进设计键——下游写词 agent 看不到块。

    块混进设计正文会被下游当设定读：写词请求把整段「对应设计」交给模型，块里的 JSON
    与围栏出现在设定里，模型会照着「asset-manifest」这个词去写画面。
    """
    node, _calls = _character_design_node(monkeypatch, [
        _design_reply("青年男人：短发，灰夹克。\n老妇人：银发，深色长衣。",
                      ["青年男人｜综合板", "老妇人｜综合板"]),
    ])

    out = node({"brief": _ref_brief()})

    assert out["character_design"].strip() == "青年男人：短发，灰夹克。\n老妇人：银发，深色长衣。"
    assert MANIFEST_FENCE not in out["character_design"], "清单块混进了设计正文"
    assert out[MANIFEST_KEY] == ["青年男人｜综合板", "老妇人｜综合板"], \
        "清单没有按张落进清单键"


def test_empty_manifest_is_a_valid_answer_without_an_extra_call(monkeypatch):
    """「本轮不新增人物母版」写空清单：合法答案，不重问。"""
    node, calls = _character_design_node(monkeypatch, [
        _design_reply("本轮不新增人物母版。", []),
    ])

    out = node({"brief": _ref_brief()})

    assert out[MANIFEST_KEY] == []
    assert len(calls) == 1, f"空清单被当成失败重问了：{len(calls)} 次调用"


def test_one_person_with_two_sheets_keeps_both_names(monkeypatch):
    """粒度到「张」：一个人两张板写两个名字，不许合并成一条。"""
    sheets = ["青年男人｜全身板", "青年男人｜表情板"]
    node, _calls = _character_design_node(monkeypatch, [
        _design_reply("青年男人：短发，灰夹克。", sheets),
    ])

    out = node({"brief": _ref_brief()})

    assert out[MANIFEST_KEY] == sheets


def test_names_absent_from_the_body_only_print_a_hint(monkeypatch, capsys):
    """清单名字没出现在正文里 → 只提示、不拦截（spec D：不硬校验两者一致）。

    正文是给人读的叙述、清单是机器口径；硬拦会让合法的简写正文整条失败，
    但完全不出声又会让「正文写了、清单漏了」没人发现。
    """
    sheets = ["青年男人｜综合板", "老妇人｜综合板"]
    node, _calls = _character_design_node(monkeypatch, [
        _design_reply("青年男人：短发，灰夹克。", sheets),
    ])

    out = node({"brief": _ref_brief()})

    assert out[MANIFEST_KEY] == sheets, "正文与清单不一致被硬拦了"
    printed = capsys.readouterr().out
    assert "老妇人｜综合板" in printed, "正文里没有的清单名字没有被提示出来"


# ---------------------------------------------------------------------------
# 2. 设计节点：块缺失／不可解析 → 重问一次
# ---------------------------------------------------------------------------

def test_missing_block_is_asked_once_more(monkeypatch):
    """偶发的格式抖动多花一次调用即可，不必重跑整段阶段 1。"""
    node, calls = _character_design_node(monkeypatch, [
        "人物设计正文（漏了清单块）",
        _design_reply("人物设计正文", ["青年男人｜综合板"]),
    ])

    out = node({"brief": _ref_brief()})

    assert len(calls) == 2, f"缺块应当恰好重问一次：{len(calls)} 次调用"
    assert MANIFEST_FENCE in calls[1], "重问请求没有点名要补的清单块，模型无从照做"
    assert out[MANIFEST_KEY] == ["青年男人｜综合板"]


@pytest.mark.parametrize("bad_reply", [
    "人物设计正文（没有块）",
    f"人物设计正文\n```{MANIFEST_FENCE}\n这不是 JSON\n```\n",
    f"人物设计正文\n```{MANIFEST_FENCE}\n{{\"sheets\": [\"x\"]}}\n```\n",
])
def test_unparseable_manifest_fails_after_one_more_ask(monkeypatch, bad_reply):
    """重问一次仍拿不到可解析的清单 → 报错并点名角色，不带着坏数据往下走。"""
    node, calls = _character_design_node(monkeypatch, [bad_reply, bad_reply])

    with pytest.raises(ValueError, match="人物形象设计"):
        node({"brief": _ref_brief()})
    assert len(calls) == 2, f"重问次数不是一次：{len(calls)} 次调用"


def test_base_route_designer_is_never_asked_for_a_manifest(monkeypatch, capsys):
    """非产资产路线：设计端回复照旧整段落设计键，不解析、不重问、不多键。

    首尾帧路线把设计融合成关键帧，没有「逐张出母版」这回事——在这里要清单就是让
    模型产出没人读的块，缺块还会让整条阶段 1 失败。
    """
    node, calls = _character_design_node(monkeypatch, ["人物设计正文"])

    out = node({"brief": _base_brief()})

    assert out == {"character_design": "人物设计正文"}
    assert len(calls) == 1
    assert MANIFEST_FENCE not in capsys.readouterr().out


def test_only_the_asset_route_request_asks_for_the_manifest_block(monkeypatch):
    """清单块是**按路线**要的：产资产路线的请求末尾要块，非产资产路线一个字都不提。

    写进角色提示词（三条路线共用）会让首尾帧的设计正文里多出一段 JSON——
    帧提示词节点把整段设计当设定读，块会一路进帧词。
    """
    asset_node, asset_calls = _character_design_node(
        monkeypatch, [_design_reply("人物设计正文", [])])
    asset_node({"brief": _ref_brief()})

    base_node, base_calls = _character_design_node(monkeypatch, ["人物设计正文"])
    base_node({"brief": _base_brief()})

    assert MANIFEST_FENCE in asset_calls[0], "产资产路线的设计请求没要清单块"
    assert MANIFEST_FENCE not in base_calls[0], "非产资产路线的设计请求要了清单块"


# ---------------------------------------------------------------------------
# 3. 出词节点：空清单不调用模型；非空按清单校验名字集合
# ---------------------------------------------------------------------------

def test_empty_manifest_produces_nothing_and_skips_the_model(monkeypatch, capsys):
    """「本轮不新增人物母版」→ 人物类产出 0 条、不调用生图词模型，并打印一行说明。"""
    node, calls = _run_image_node(monkeypatch, {
        "brief": _ref_brief(), "character_design": "本轮不新增人物母版。",
        MANIFEST_KEY: [],
    })

    out = node({"brief": _ref_brief(), "character_design": "本轮不新增人物母版。",
                MANIFEST_KEY: []})

    assert out == {"character_image_prompts": []}
    assert calls == [], "清单为空仍然调用了生图词模型"
    assert "人物资产清单为空，本次不产母版" in capsys.readouterr().out


def test_image_request_lists_the_manifest_names_and_accepts_exactly_those(monkeypatch):
    """出词请求带清单名字（模型才有东西可照抄），产物名字集合与清单相等。"""
    sheets = ["青年男人｜综合板", "老妇人｜综合板"]
    node, calls = _run_image_node(monkeypatch, {
        "brief": _ref_brief(), "character_design": "人物设计正文",
        MANIFEST_KEY: sheets,
    })

    out = node({"brief": _ref_brief(), "character_design": "人物设计正文",
                MANIFEST_KEY: sheets})

    assert len(calls) == 1
    for name in sheets:
        assert name in calls[0], f"出词请求没列出清单名字 {name}"
    assert [e["name"] for e in out["character_image_prompts"]] == sheets


def test_more_entries_than_the_manifest_fails_and_lists_the_extras(monkeypatch):
    """清单 2 条而产出 3 条 → 当场报错并列出多出来的名字。"""
    sheets = ["青年男人｜综合板", "老妇人｜综合板"]
    node = nodes._make_image_prompt_node(
        _image_agents(),
        "人物", "character_design", "character_image_prompts")
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _entries_json(
        [*sheets, "场景建置图"]))

    with pytest.raises(ValueError) as excinfo:
        node({"brief": _ref_brief(), "character_design": "人物设计正文",
              MANIFEST_KEY: sheets})

    message = str(excinfo.value)
    assert "场景建置图" in message, f"报错没列出多出来的名字：{message}"


def test_renamed_entry_fails_and_lists_both_sides(monkeypatch):
    """换名 → 既报多出（新名）也报缺少（旧名），不靠人猜差在哪。"""
    sheets = ["青年男人｜综合板", "老妇人｜综合板"]
    node = nodes._make_image_prompt_node(
        _image_agents(),
        "人物", "character_design", "character_image_prompts")
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _entries_json(
        ["青年男人｜综合板", "老妇人｜正面板"]))

    with pytest.raises(ValueError) as excinfo:
        node({"brief": _ref_brief(), "character_design": "人物设计正文",
              MANIFEST_KEY: sheets})

    message = str(excinfo.value)
    assert "老妇人｜正面板" in message and "老妇人｜综合板" in message, \
        f"换名的两边没有都列出来：{message}"


def test_duplicate_entries_fail(monkeypatch):
    """同一张出两遍也算不一致——一个人两张板是**两个名字**，不是同一个名字重复。"""
    sheets = ["青年男人｜综合板"]
    node = nodes._make_image_prompt_node(
        _image_agents(),
        "人物", "character_design", "character_image_prompts")
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _entries_json(
        ["青年男人｜综合板", "青年男人｜综合板"]))

    with pytest.raises(ValueError, match="重复"):
        node({"brief": _ref_brief(), "character_design": "人物设计正文",
              MANIFEST_KEY: sheets})


def test_legacy_session_without_a_manifest_keeps_working(monkeypatch, capsys):
    """旧会话没有清单：打印一行说明后按现状出词，不硬拦。"""
    node = nodes._make_image_prompt_node(
        _image_agents(),
        "人物", "character_design", "character_image_prompts")
    captured: list[str] = []

    def fake_run_agent(agent, message):
        captured.append(message)
        return _entries_json(["红裙女孩"])

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)

    out = node({"brief": _ref_brief(), "character_design": "人物设计正文"})

    assert "本会话无资产清单，未校验类别" in capsys.readouterr().out
    assert [e["name"] for e in out["character_image_prompts"]] == ["红裙女孩"]


# ---------------------------------------------------------------------------
# 4. 单条重出：名字必须在清单内
# ---------------------------------------------------------------------------

def test_single_regeneration_refuses_a_name_outside_the_manifest(monkeypatch):
    """单条重出只接受清单内的名字——清单外的名字是错位，不是待办。"""
    sheets = ["青年男人｜综合板"]
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _entries_json(
        ["老妇人｜综合板"]))

    with pytest.raises(ValueError, match="青年男人｜综合板|清单"):
        nodes.regenerate_asset_prompt(
            _image_agents(), "character", "老妇人｜综合板",
            {"character_design": "人物设计正文", MANIFEST_KEY: sheets},
            _ref_brief(), model="qwen")


def test_single_regeneration_accepts_a_name_inside_the_manifest(monkeypatch):
    sheets = ["青年男人｜综合板", "老妇人｜综合板"]
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _entries_json(
        ["老妇人｜综合板"]))

    entry = nodes.regenerate_asset_prompt(
        _image_agents(), "character", "老妇人｜综合板",
        {"character_design": "人物设计正文", MANIFEST_KEY: sheets},
        _ref_brief(), model="qwen")

    assert entry["name"] == "老妇人｜综合板"


# ---------------------------------------------------------------------------
# 5. 会话持久化：清单键进白名单，且随资产键一起按路线丢弃
# ---------------------------------------------------------------------------

def _manifest_state() -> dict:
    state = {"shot_table": "[Shot 1] 庭院空镜"}
    for kind, key in ASSET_MANIFEST_FIELDS.items():
        state[key] = [f"{kind}-母版"]
    return state


def test_ref_session_round_trips_the_manifest_keys(tmp_path):
    """多参会话读回后清单还在：丢一个键，续跑后的出词节点就退回「本会话无资产清单」。"""
    generation_dir = tmp_path / "GEN_ref"
    save_session(generation_dir, _ref_brief(), _manifest_state())

    loaded = load_session(generation_dir)

    assert loaded is not None
    for key in ASSET_MANIFEST_KEYS:
        assert loaded.stage_state[key] == [f"{key.split('_')[0]}-母版"], \
            f"多参会话读回丢了清单键 {key}"


def test_base_session_drops_the_manifest_keys_on_load(tmp_path):
    """非产资产路线读回时清单键**必须**丢掉（与资产三键同一批产物）。

    留着它不只碍眼：同一次运行里路线判定一旦被别的东西改写，出词节点会按一份
    本路线根本不存在的清单去校验。
    """
    generation_dir = tmp_path / "GEN_base"
    save_session(generation_dir, _base_brief(), _manifest_state())

    loaded = load_session(generation_dir)

    assert loaded is not None
    assert not [key for key in ASSET_MANIFEST_KEYS if key in loaded.stage_state], \
        "首尾帧会话读回仍带着资产清单键"
    assert loaded.stage_state["shot_table"].startswith("[Shot 1]"), \
        "路线过滤把无关产物也删了"
