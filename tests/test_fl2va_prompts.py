"""FL2VA 融合首尾帧提示词测试。"""
import json

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.generation import (
    FL2VAFramePrompt,
    FL2VAPromptBundle,
    GenerationResult,
    fl2va_bundle_from_dict,
    result_from_state,
    validate_fl2va_bundle,
)
from minimax_h3_prompt.graph import nodes, pipeline
from minimax_h3_prompt.project_store import ProjectStore


def frame_payload(prompt: str) -> dict:
    return {
        "zimage": {"positive_prompt": prompt, "instructions": ["paste to node 10"]},
        "flux2": {"positive_prompt": prompt + " Flux.2 cinematic rendering.", "instructions": ["paste to node 118"]},
    }


def bundle_payload(conflict: bool = False) -> dict:
    scene = "medieval tavern interior"
    first = "Three adventurers sit around a wooden table inside a medieval tavern interior, with mugs and quest gear visible."
    last = "The same three adventurers raise their mugs inside the same medieval tavern interior, with the same table and quest gear."
    if conflict:
        last = "The three adventurers celebrate in a forest clearing, an open field far from the tavern."
    return {
        "scene_anchor": scene,
        "first": frame_payload(first),
        "last": frame_payload(last),
        "continuity_constraints": ["Keep the same characters, clothing, props, tavern layout, and lighting direction."],
    }


def make_fl2va_result(generation_id="GEN001"):
    brief = Brief(variant="FL2VA", duration=5, style="live-action realism", plot="中世纪酒馆室内，冒险者团队庆祝")
    return result_from_state(
        {
            "script": "The adventurers celebrate in the tavern.",
            "final_prompt": "How the reference pictures align with the target video — Picture 1 aligns with 0.00 seconds; Picture 2 aligns with 5.00 seconds.\n\nintegrated_multimodal_description: [Shot 1] The group celebrates.\n\noverall_soundscape: Tavern ambience.\n\nnon_diegetic_music: N/A",
            "character_design": "three adventurers",
            "prop_design": "wooden mugs and quest gear",
            "background_design": "medieval tavern interior",
            "fl2va_prompt_bundle": bundle_payload(),
        },
        brief,
        generation_id=generation_id,
        topic_id="topic",
        project_id="project",
    )


def test_fl2va_bundle_roundtrip_and_scene_guard():
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(bundle_payload(), brief)
    assert isinstance(FL2VAPromptBundle.from_dict(bundle.to_dict()), FL2VAPromptBundle)
    assert bundle.first[0].time_seconds == 0
    assert bundle.last[0].time_seconds == 5
    assert validate_fl2va_bundle(bundle, duration=5) == []

    with pytest.raises(ValueError, match="FL2VA_SCENE_DRIFT"):
        fl2va_bundle_from_dict(bundle_payload(conflict=True), brief)


def test_window_view_of_forest_does_not_break_tavern_anchor():
    payload = bundle_payload()
    payload["last"] = frame_payload(
        "The same adventurers remain inside the same medieval tavern interior, seen through a window with a forest clearing outside."
    )
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    assert fl2va_bundle_from_dict(payload, brief).scene_anchor == "medieval tavern interior"


def test_fl2va_generation_persists_new_files_without_assets(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    store.init_project("topic", "project", "Tavern")
    result = make_fl2va_result()
    directory = store.save_generation_result("topic", "project", result)
    assert (directory / "fl2va-prompt.json").exists()
    assert (directory / "fl2va-prompt.md").exists()
    assert (directory / "first-frame-prompt.md").exists()
    assert (directory / "last-frame-prompt.md").exists()
    assert store.load_generation_result("topic", "project", "GEN001") == result
    shown = store.show_generation("topic", "project", "GEN001", kind="first-frame", raw=True)
    assert "medieval tavern interior" in shown["artifacts"]["first-frame"]
    registry = (store.project_directory("topic", "project") / "asset-registry.json").read_text(encoding="utf-8")
    assert '"assets": []' in registry


def test_pipeline_chain_places_frame_node_after_visual():
    assert pipeline._LINEAR_CHAIN.index("parallel_visual") < pipeline._LINEAR_CHAIN.index("fl2va_frame_prompts")
    assert pipeline._LINEAR_CHAIN.index("fl2va_frame_prompts") < pipeline._LINEAR_CHAIN.index("parallel_sound")


def test_frame_node_uses_shot_and_visual_context(monkeypatch):
    captured = {}
    payload = json.dumps(bundle_payload())

    def fake_run_agent(agent, message):
        captured["message"] = message
        return payload

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    output = node({
        "brief": brief,
        "script": "script",
        "character_design": "characters",
        "prop_design": "props",
        "background_design": "tavern interior",
        "art_design": "integrated tavern design",
        "shot_table": "start and end states",
        "shot_review_lock": "locked shot",
        "visual_design": "medium-wide composition",
    })
    assert "分镜表" in captured["message"]
    assert "画面细化" in captured["message"]
    assert output["fl2va_prompt_bundle"]["profile_id"] == "h3_fl2va_v2"


# ---------------------------------------------------------------------------
# I2VA / L2VA 单帧 bundle
# ---------------------------------------------------------------------------

def _single_frame_payload(frame_key: str, prompt: str) -> dict:
    return {
        "scene_anchor": "medieval tavern interior",
        frame_key: frame_payload(prompt),
        "continuity_constraints": ["Keep the same characters and props."],
    }


def test_single_frame_bundle_i2va():
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(
        _single_frame_payload("first", "Three adventurers inside a medieval tavern interior."), brief,
    )
    assert bundle.first
    assert bundle.last == ()
    assert validate_fl2va_bundle(bundle, duration=5) == []
    # roundtrip 保持单帧
    restored = FL2VAPromptBundle.from_dict(bundle.to_dict())
    assert restored.first and not restored.last


def test_single_frame_bundle_l2va():
    brief = Brief(variant="L2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(
        _single_frame_payload("last", "Three adventurers raise mugs inside a medieval tavern interior."), brief,
    )
    assert bundle.last
    assert bundle.first == ()
    assert validate_fl2va_bundle(bundle, duration=5) == []


def test_fl2va_bundle_still_requires_both_frames():
    """FL2VA 请求但只给单帧 → 必须 raise（回归强校验，不能因单帧化放宽）。"""
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    with pytest.raises(ValueError, match="必须同时包含首帧和尾帧"):
        fl2va_bundle_from_dict(
            _single_frame_payload("first", "Three adventurers inside a medieval tavern interior."), brief,
        )


def test_validate_fl2va_bundle_variant_aware():
    """I2VA 只校验首帧；L2VA 的 last.time <= duration 检查生效。"""
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(
        _single_frame_payload("first", "Three adventurers inside a medieval tavern interior."), brief,
    )
    assert validate_fl2va_bundle(bundle, duration=5, variant="I2VA") == []
    bad_last = (
        FL2VAFramePrompt(frame="last", model_family="zimage", positive_prompt="x",
                         time_seconds=9.0, scene_anchor="medieval tavern interior"),
        FL2VAFramePrompt(frame="last", model_family="flux2", positive_prompt="y",
                         time_seconds=9.0, scene_anchor="medieval tavern interior"),
    )
    bad = FL2VAPromptBundle(scene_anchor="medieval tavern interior", first=(), last=bad_last,
                            continuity_constraints=("c",))
    assert "FL2VA_LAST_FRAME_TIME_EXCEEDS_DURATION" in validate_fl2va_bundle(bad, duration=5, variant="L2VA")


def test_result_from_state_single_frame_roundtrip():
    brief = Brief(variant="I2VA", duration=5, style="live-action realism", plot="中世纪酒馆室内，冒险者团队庆祝")
    result = result_from_state(
        {
            "script": "The adventurers celebrate in the tavern.",
            "final_prompt": "video prompt",
            "character_design": "three adventurers",
            "prop_design": "wooden mugs and quest gear",
            "background_design": "medieval tavern interior",
            "fl2va_prompt_bundle": _single_frame_payload(
                "first", "Three adventurers inside a medieval tavern interior."),
        },
        brief,
        generation_id="GEN001",
        topic_id="topic",
        project_id="project",
    )
    assert result.variant == "I2VA"
    assert result.fl2va_prompt_bundle is not None
    assert result.fl2va_prompt_bundle.first and not result.fl2va_prompt_bundle.last
    assert GenerationResult.from_dict(result.to_dict()) == result


def test_frame_node_i2va_emits_first_only(monkeypatch):
    captured = {}
    payload = json.dumps(_single_frame_payload(
        "first", "Three adventurers inside a medieval tavern interior."))

    def fake_run_agent(agent, message):
        captured["message"] = message
        return payload

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    output = node({
        "brief": brief,
        "script": "script",
        "character_design": "characters",
        "prop_design": "props",
        "background_design": "tavern interior",
        "art_design": "integrated tavern design",
        "shot_table": "start and end states",
        "shot_review_lock": "locked shot",
        "visual_design": "medium-wide composition",
    })
    assert output["fl2va_prompt_bundle"]["first"]
    assert not output["fl2va_prompt_bundle"]["last"]
    assert "I2VA" in captured["message"]
