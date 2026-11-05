"""清单级跨类语义名重名守卫（issue #175 / spec #165 E）。

问题陈述（2026-11-04 r2v 实跑）：人物设计写了一个名字、场景设计也写了同一个名字，
两份清单各自合法、阶段 1 一路跑到交回第一张图，才在台账入库处报「语义名重复」——
那时用户已经为这张图付过生成成本，而且报错点离设计合并处很远，看不出是哪两位
设计师撞了名。

守卫挂在**设计师并行组的组合节点外层**（spec E 的第三条测试缝）：三份清单合并之后、
进入美术统筹之前，当场失败并点名两个类别与那个名字。

**类内重复不归这里管**（#178 裁定）：解析器拿到单份清单时就判「不可解析」并点名重问，
合并处再判一次的话那条分支在真实路径上永远走不到（「永不为红＝没有」）。本文件只钉
跨类重名，以及「类内重复不被这层接手」这条边界。

本文件的判据只打在外部行为上：组合节点抛出的错误、原样返回的 patch。纯函数
``duplicate_asset_names`` 不单独测——它只通过组合节点露出（与 #173 对解析器/比较器的
测法一致）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.kinds import ASSET_MANIFEST_FIELDS

TOPIC = "秋日庭院里的橘猫"


def _ref_brief() -> Brief:
    """多参路线：产资产图提示词，设计师必须附清单（spec #166 D）。"""
    return Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot=TOPIC)


def _base_brief() -> Brief:
    """首尾帧路线：不产资产图提示词，没有清单这回事。"""
    return Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC)


def _designers_node(brief: Brief, designers: dict[str, list[str] | None]):
    """造一个「设计师并行组」组合节点；``designers`` 是节点名 → 清单名字。

    值为 ``None`` 的节点只返回设计正文（模拟旧会话／不附清单的回复）。
    设计正文写成名字的 join，方便断言 patch 原样返回时正文也在。
    """
    def make(names: list[str] | None, field: str):
        def node(state):
            patch = {field: f"{field}正文"}
            if names is not None:
                patch[ASSET_MANIFEST_FIELDS[{
                    "character_design": "character",
                    "background_design": "scene",
                    "prop_design": "prop",
                }[field]]] = names
            return patch
        return node

    fields = {
        "character_designer": "character_design",
        "background_designer": "background_design",
        "prop_designer": "prop_design",
    }
    nodes = {
        name: make(designers[name], field) for name, field in fields.items()
    }
    return pipeline._combined_node(
        "parallel_designers", nodes, None, None, None, None, brief)


# ---------------------------------------------------------------------------
# 1. 两类同名 → 设计合并处当场失败并点名两个类别
# ---------------------------------------------------------------------------

def test_a_name_shared_by_two_categories_fails_at_the_design_merge():
    """人物与场景同名 → 组合节点抛错，点出名字与「人物」「场景」两个类别。"""
    node = _designers_node(_ref_brief(), {
        "character_designer": ["青年男人｜综合板"],
        "background_designer": ["青年男人｜综合板"],
        "prop_designer": [],
    })

    with pytest.raises(ValueError) as excinfo:
        node({"brief": _ref_brief()})

    message = str(excinfo.value)
    assert "青年男人｜综合板" in message, f"报错没点名重名的语义名：{message}"
    assert "人物" in message and "场景" in message, \
        f"报错没点出撞名的两个类别：{message}"
    assert "同时出现在" in message, f"报错没说清是跨类重名：{message}"


def test_a_name_shared_by_three_categories_names_all_of_them():
    """三类同时撞名时三个类别都要点名，不能只报最先遇到的两个。"""
    node = _designers_node(_ref_brief(), {
        "character_designer": ["红裙女孩"],
        "background_designer": ["红裙女孩"],
        "prop_designer": ["红裙女孩"],
    })

    with pytest.raises(ValueError) as excinfo:
        node({"brief": _ref_brief()})

    message = str(excinfo.value)
    for label in ("人物", "场景", "道具"):
        assert label in message, f"三类撞名漏报了「{label}」：{message}"


# ---------------------------------------------------------------------------
# 2. 类内重复不归合并处守卫管（由解析器在拿到单份清单时报）
# ---------------------------------------------------------------------------

def test_in_category_repeats_are_left_to_the_parser():
    """人物清单里同一个名字出现两次 → 合并处放行，不在这里报。

    真实路径上这种 patch 根本到不了这里：设计节点先经 ``split_manifest_reply``，
    重复名判不可解析、重问时就点名了。守卫若也判一份，就等于在生产路径上留一条
    永不执行的分支——看起来多一层保险，实际没有任何用例能证明它被走到。
    """
    node = _designers_node(_ref_brief(), {
        "character_designer": ["青年男人｜综合板", "青年男人｜综合板"],
        "background_designer": [],
        "prop_designer": [],
    })

    out = node({"brief": _ref_brief()})

    assert out["character_asset_manifest"] == ["青年男人｜综合板", "青年男人｜综合板"], \
        "合并处接手了类内重复——这条分支在真实路径上不可达（#178）"


# ---------------------------------------------------------------------------
# 3. 清单唯一时放行；非产资产路线行为不变
# ---------------------------------------------------------------------------

def test_unique_manifests_pass_through_unchanged():
    """三类各自唯一 → 组合节点原样返回 patch，不误报、不改写。"""
    node = _designers_node(_ref_brief(), {
        "character_designer": ["青年男人｜综合板"],
        "background_designer": ["秋日庭院"],
        "prop_designer": ["长椅", "橘猫玩偶"],
    })

    out = node({"brief": _ref_brief()})

    assert out["character_asset_manifest"] == ["青年男人｜综合板"]
    assert out["scene_asset_manifest"] == ["秋日庭院"]
    assert out["prop_asset_manifest"] == ["长椅", "橘猫玩偶"]
    assert out["character_design"] == "character_design正文"


def test_non_asset_route_ignores_manifests_entirely():
    """非产资产路线不执行守卫：设计师回复里就算带着清单，也原样返回不报错。

    首尾帧与导演台非 r2v 模式根本没有「逐张出母版」这一步，在这里拦是拦一份本路线
    不存在的契约——而路线判定必须只住 ``route_emits_asset_prompts`` 一处。
    """
    node = _designers_node(_base_brief(), {
        "character_designer": ["同名"],
        "background_designer": ["同名"],
        "prop_designer": ["同名"],
    })

    out = node({"brief": _base_brief()})

    assert out["character_asset_manifest"] == ["同名"]
    assert out["character_design"] == "character_design正文"


def test_old_session_without_manifests_is_not_blocked():
    """产资产路线但清单键整键缺失（旧会话）→ 无清单可查，照旧放行。"""
    node = _designers_node(_ref_brief(), {
        "character_designer": None,
        "background_designer": None,
        "prop_designer": None,
    })

    out = node({"brief": _ref_brief()})

    assert out["character_design"] == "character_design正文"
    assert not [key for key in ASSET_MANIFEST_FIELDS.values() if key in out]
