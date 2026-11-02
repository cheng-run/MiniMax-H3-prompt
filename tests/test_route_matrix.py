"""三路线矩阵（体检 §5 盲区 2 / P4）。

体检结论：base / ref / director 三条路线各写各的测试，没有「同一功能三路线都覆盖」
的矩阵——这正是 #141、#142、#139 都「全量绿着漏过去」的原因之一。

这里把三条路线放进同一张表，对**同一组共同行为**逐条断言：

1. 路线判定与能力判据（``validate_route_mode`` / ``route_emits_asset_prompts``）；
2. 会话往返与路线过滤（资产三键：多参/导演台 r2v 保留，其余路线丢掉）；
3. 整片校验器入口（每条路线都有明确入口，叙事路线不炸、导演台给 warning）。

加第四条路线或改判据时，两张表必须一起动；只动一处就是「另一条路线没人管」。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.brief_parser import (
    Brief,
    route_emits_asset_prompts,
    validate_route_mode,
)
from minimax_h3_prompt.kinds import ASSET_PROMPT_KEYS
from minimax_h3_prompt.session_store import load_session, save_session
from minimax_h3_prompt.tools.h3_validator import validate_prompt

PLOT = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# (用例名, brief, 是否产资产图提示词)
CASES = (
    ("base", Brief(mode="base", variant="FL2VA", duration=10.0, plot=PLOT), False),
    ("ref", Brief(mode="ref", variant="T2VA", duration=10.0, plot=PLOT), True),
    ("director-t2v",
     Brief(mode="director", variant="T2VA", director_mode="t2v", duration=10.0, plot=PLOT),
     False),
    ("director-r2v",
     Brief(mode="director", variant="T2VA", director_mode="r2v", duration=10.0, plot=PLOT),
     True),
)

_IDS = [case[0] for case in CASES]


@pytest.mark.parametrize(("name", "brief", "emits_assets"), CASES, ids=_IDS)
def test_route_is_recognised_and_capability_matches(name, brief, emits_assets):
    assert validate_route_mode(brief.mode) == brief.mode
    assert route_emits_asset_prompts(brief) is emits_assets


@pytest.mark.parametrize(("name", "brief", "emits_assets"), CASES, ids=_IDS)
def test_session_round_trip_filters_asset_keys_by_route(tmp_path, name, brief, emits_assets):
    """同一份 stage_state 存进去，读回来时按路线过滤——三条路线同一条规则。"""
    stage_state = {
        "script": "剧本正文",
        "frame_images": [{"role": "首帧", "path": "a.png"}],
        **{key: [{"name": "红裙女孩", "positive_prompt": "提示词"}] for key in ASSET_PROMPT_KEYS},
    }
    save_session(tmp_path / name, brief, stage_state)

    loaded = load_session(tmp_path / name)
    assert loaded is not None
    assert loaded.brief.mode == brief.mode
    assert loaded.brief.variant == brief.variant
    # 导演台模式必须在往返里活着（漏登记就是静默丢，重启后按模式分派会拿到空值）
    assert loaded.brief.director_mode == brief.director_mode
    for key in ASSET_PROMPT_KEYS:
        assert (key in loaded.stage_state) is emits_assets, (
            f"{name} 路线读回的资产键 {key} 与路线能力不符："
            "多留着会喂错产物，丢掉了会中断续跑")
    # 非资产产物任何路线都不许丢
    assert loaded.stage_state["script"] == "剧本正文"
    assert loaded.stage_state["frame_images"][0]["path"] == "a.png"


@pytest.mark.parametrize(("name", "brief", "emits_assets"), CASES, ids=_IDS)
def test_whole_prompt_validator_has_an_entry_for_every_route(name, brief, emits_assets):
    """整片校验器对每条路线都**有入口**：叙事路线按段落判，导演台给 warning 指路。

    导演台分支若退化成 ``else``，它会静默掉进 base 分支被按三节判红——那正是
    issue #103 修掉的形态。
    """
    text = "一段随便写的内容。"
    issues = validate_prompt(text, brief.mode, duration=brief.duration,
                             variant=brief.variant, ref_meta=None)
    assert isinstance(issues, list)
    if brief.mode == "director":
        assert [issue.code for issue in issues] == ["DIRECTOR_WHOLE_PROMPT_UNUSED"]
        assert issues[0].severity == "warning"
    else:
        assert issues, f"{name} 路线对空壳提示词应当报错，实际全绿"
