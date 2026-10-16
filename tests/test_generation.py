"""剧本与四类提示词生成结果测试。"""
import json

import pytest

from minimax_h3_prompt.generation import (
    GenerationResult,
    PromptArtifact,
    ref_asset_registry,
    result_from_state,
)
from minimax_h3_prompt.brief_parser import Brief, RefItem
from minimax_h3_prompt.topic_generation import save_generation


def make_result(generation_id="GEN001"):
    brief = Brief(plot="雨夜街角", duration=5, style="cinematic", language="Chinese")
    return result_from_state(
        {
            "script": "第一场：雨夜街角。",
            "final_prompt": "video prompt",
            "character_design": "character prompt",
            "prop_design": "prop prompt",
            "background_design": "scene prompt",
        },
        brief,
        generation_id=generation_id,
        topic_id="topic",
        project_id="project",
    )


def test_generation_result_maps_four_prompt_kinds_and_roundtrips():
    result = make_result()
    restored = GenerationResult.from_dict(result.to_dict())
    assert restored == result
    assert result.script == "第一场：雨夜街角。"
    assert {item.kind for item in result.artifacts} == {"video", "character", "prop", "scene"}
    assert result.artifact("video").content == "video prompt"


def test_generation_result_rejects_missing_output_or_bad_hash():
    with pytest.raises(ValueError, match="缺少"):
        result_from_state(
            {"script": "剧本", "final_prompt": "视频"},
            Brief(plot="主题"), generation_id="GEN001", topic_id="topic", project_id="project",
        )
    artifact = PromptArtifact("a", "video", "正文", "final_prompt")
    with pytest.raises(ValueError, match="sha256"):
        PromptArtifact("a", "video", "正文", "final_prompt", sha256="0" * 64)
    assert len(artifact.sha256) == 64


def test_save_generation_writes_files_and_rejects_changed_without_overwrite(tmp_path):
    result = make_result()
    directory = save_generation(result, tmp_path / "GEN001")
    assert (directory / "script.md").read_text(encoding="utf-8").strip() == result.script
    assert (directory / "video-prompt.md").read_text(encoding="utf-8").strip() == "video prompt"
    assert "api_key" not in json.dumps(result.to_dict())

    changed = GenerationResult(**{**result.__dict__, "script": "不同剧本"})
    with pytest.raises(FileExistsError):
        save_generation(changed, directory)


# ---------------------------------------------------------------------------
# 参考素材登记（多参路线，issue #41）：<Picture N> 编号按剧本出场顺序分配
# ---------------------------------------------------------------------------

def test_ref_asset_registry_numbers_by_script_appearance_order():
    """编号按**剧本出场顺序**分配（编号即叙事顺序），不按资产枚举顺序。"""
    state = {
        "script": "红裙女孩走进庭院，把青铜钥匙交给蓝衫少年。",
        "character_image_prompts": [
            {"name": "蓝衫少年", "positive_prompt": "少年·正向"},
            {"name": "红裙女孩", "positive_prompt": "女孩·正向"},
        ],
        "prop_image_prompts": [
            {"name": "青铜钥匙", "positive_prompt": "钥匙·正向"},
        ],
    }
    registry = ref_asset_registry(state)
    assert [(r.picture, r.name) for r in registry] == [
        (1, "红裙女孩"), (2, "青铜钥匙"), (3, "蓝衫少年"),
    ]


def test_ref_asset_registry_description_is_image_prompt():
    """描述主体取**生图提示词**（素材画面事实规则：它细节最全）。"""
    state = {
        "script": "红裙女孩在庭院。",
        "character_image_prompts": [
            {"name": "红裙女孩", "positive_prompt": "红裙、黑发、银项链的生图提示词正文"},
        ],
    }
    registry = ref_asset_registry(state)
    assert registry[0].description == "红裙、黑发、银项链的生图提示词正文"


def test_ref_asset_registry_prefers_brief_refs():
    """brief.refs 非空时以用户登记为准：用户自己声明的编号不许被出场顺序改写。"""
    state = {
        "script": "蓝衫少年先出场，红裙女孩后出场。",
        "character_image_prompts": [
            {"name": "蓝衫少年", "positive_prompt": "少年·正向"},
            {"name": "红裙女孩", "positive_prompt": "女孩·正向"},
        ],
    }
    brief = Brief(mode="ref", refs=[
        RefItem(picture=2, name="用户自编号", description="用户写的描述"),
    ])
    registry = ref_asset_registry(state, brief)
    assert [(r.picture, r.name, r.description) for r in registry] == [
        (2, "用户自编号", "用户写的描述"),
    ]


def test_ref_asset_registry_tail_keeps_unmatched_stable():
    """语义名在剧本里找不到出场的资产排最后（稳定次序：人物→道具→场景枚举序）。

    编号要钉的是叙事顺序，找不到出场就没有叙事位次；它们仍进登记（图照生、槽照接），
    不静默丢弃——丢掉的资产会在别处以「换版不可复测」的形态回来（LoRA 教训）。
    """
    state = {
        "script": "红裙女孩在庭院。",
        "character_image_prompts": [
            {"name": "未出场少年", "positive_prompt": "a"},
            {"name": "红裙女孩", "positive_prompt": "b"},
        ],
        "prop_image_prompts": [{"name": "未出场钥匙", "positive_prompt": "c"}],
        "scene_image_prompts": [{"name": "未出场场景", "positive_prompt": "d"}],
    }
    registry = ref_asset_registry(state)
    assert [(r.picture, r.name) for r in registry] == [
        (1, "红裙女孩"), (2, "未出场少年"), (3, "未出场钥匙"), (4, "未出场场景"),
    ]


def test_ref_asset_registry_does_not_leak_into_base_mode():
    """base/帧变体不派生资产登记（issue #41 复审 F1）。

    帧路线的 Picture 1/2 是首尾帧：派生资产登记会让 frame agent 挂上
    list_reference_assets、报资产级编号——AC1「首尾帧路线不受影响」的软性回归。
    """
    state = {
        "script": "红裙女孩在庭院。",
        "character_image_prompts": [{"name": "红裙女孩", "positive_prompt": "b"}],
        "prop_image_prompts": [{"name": "青铜钥匙", "positive_prompt": "c"}],
    }
    assert ref_asset_registry(state, Brief(mode="base", variant="FL2VA")) == []
    # brief.refs 非空时照旧以用户登记为准（帧路线的首/尾帧登记不走派生）。
    refs = [RefItem(picture=2, name="尾帧", description="")]
    assert [(r.picture, r.name) for r in ref_asset_registry(
        state, Brief(mode="base", variant="FL2VA", refs=refs))] == [(2, "尾帧")]


def test_result_from_state_ref_mode_keeps_four_artifacts():
    """mode=ref 的结构化产物形状：video + 三类设计产物，mode 如实记录（issue #41）。"""
    brief = Brief(mode="ref", variant="T2VA", plot="红裙女孩走进庭院", duration=5)
    result = result_from_state(
        {
            "script": "剧本",
            "final_prompt": "subject_definitions: x\n\nsummary: [reference generation] t\n\n"
                            "retention_analysis: x\n\ndetailed_description: x\n\n"
                            "overall_soundscape: N/A\n\nnon_diegetic_music: N/A",
            "character_design": "character",
            "prop_design": "prop",
            "background_design": "scene",
        },
        brief,
        generation_id="GEN001",
        topic_id="topic",
        project_id="project",
    )
    assert result.mode == "ref"
    assert [item.kind for item in result.artifacts] == ["video", "character", "prop", "scene"]
    assert result.fl2va_prompt_bundle is None
