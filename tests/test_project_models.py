"""项目化 ProjectBible、AssetRegistry 与 ShotPlan 测试。"""
import json

import pytest

from minimax_h3_prompt.project_models import (
    AssetRecord,
    AssetReference,
    AssetRegistry,
    ProfileReference,
    ProjectBible,
    ReviewRecord,
    Shot,
    ShotPlan,
    dumps_document,
    prompt_sha256,
)


def asset(asset_id="C01", status="planned"):
    return AssetRecord(asset_id, "character", "主角", version=f"{asset_id}-v001", status=status)


def test_project_bible_roundtrip_and_reference_gate():
    bible = ProjectBible(
        "project-001", "topic-001", "雨夜短片", global_style="cinematic",
        characters=(AssetReference("C01", "主角"),), asset_ids=("C01",),
        workflow_profiles=(ProfileReference("zimage_t2i_v1", "1", "a" * 64),),
    )
    restored = ProjectBible.from_dict(bible.to_dict())
    assert restored == bible
    assert bible.validate_references(AssetRegistry("topic-001", "project-001", (asset(),))) == []
    assert "ASSET_MISSING" in bible.validate_references(AssetRegistry("topic-001", "project-001"))[0]


def test_asset_status_requires_media_approval_and_preserves_review():
    candidate = asset().with_status("candidate")
    review = ReviewRecord("visual", "info", "VISUAL_ACCEPTED", "人工验收通过", "C01", outcome="approved")
    approved = candidate.with_status("approved", review)
    assert approved.status == "approved"
    assert approved.review_records == (review,)
    with pytest.raises(ValueError, match="视觉/听觉"):
        asset().with_status("candidate").with_status("approved")


def test_registry_rejects_overwrite():
    registry = AssetRegistry("topic-001", "project-001").add(asset())
    with pytest.raises(ValueError, match="禁止覆盖"):
        registry.add(asset())


def test_shot_requires_previous_end_state_derivation():
    first = Shot("SH001", 1, 4, "人物站在门外", "推门", "人物进入房间")
    second = Shot("SH002", 2, 4, "人物进入房间", "回头", "人物看向门", previous_shot_id="SH001", start_state_derived_from="SH001")
    plan = ShotPlan("plan-001", "project-001", "fl2va", 8, (first, second))
    assert plan.validate() == []
    invalid = Shot("SH002", 2, 4, "重新设定位置", "回头", "人物看向门", previous_shot_id="SH001")
    assert any("START_STATE_NOT_DERIVED" in error for error in ShotPlan("p", "project-001", "fl2va", 8, (first, invalid)).validate())


def test_shot_plan_rejects_duplicate_ids_numbers_and_order():
    first = Shot("SH001", 1, 2, "起点", "动作", "终点")
    duplicate_id = Shot("SH001", 2, 2, "终点", "动作", "结束")
    errors = ShotPlan("p", "project-001", "fl2va", 4, (first, duplicate_id)).validate()
    assert any(error.startswith("DUPLICATE_SHOT_ID") for error in errors)

    duplicate_number = Shot("SH002", 1, 2, "终点", "动作", "结束")
    errors = ShotPlan("p", "project-001", "fl2va", 4, (first, duplicate_number)).validate()
    assert any(error.startswith("DUPLICATE_SHOT_NUMBER") for error in errors)

    out_of_order = Shot("SH002", 3, 2, "终点", "动作", "结束", previous_shot_id="SH001", start_state_derived_from="SH001")
    errors = ShotPlan("p", "project-001", "fl2va", 4, (first, out_of_order)).validate()
    assert any(error.startswith("SHOT_NUMBER_ORDER") for error in errors)


    shot = Shot("SH001", 1, 4, "起点", "动作", "终点")
    with pytest.raises(ValueError, match="approved"):
        ShotPlan("p", "project-001", "fl2va", 4, (shot,)).lock()


def test_profile_and_asset_status_are_independent_and_api_keys_are_not_serialized():
    profile = ProfileReference("h3_fl2va_v2", "2", "b" * 64, "candidate")
    record = asset("C01").to_dict()
    record["metadata"] = {"api_key": "must-not-be-stored"}
    # 模型不接受任意 metadata，且标准序列化不会自动携带外部配置。
    assert "api_key" not in dumps_document(profile)
    assert "DASHSCOPE_API_KEY" not in dumps_document(record)
    with pytest.raises(ValueError, match="不能把"):
        ProfileReference("h3_fl2va_v2", "2", "b" * 64, "approved")


def test_prompt_hash_is_stable():
    assert prompt_sha256("提示词") == prompt_sha256("提示词")
    assert len(prompt_sha256("提示词")) == 64
    assert json.loads(dumps_document(ProjectBible("p", "t", "标题"))) ["project_id"] == "p"
