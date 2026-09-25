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
from minimax_h3_prompt.topic_generation import save_generation
from minimax_h3_prompt.user_revisions import LAYER_FRAME, render_baseline_block


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


def test_chinese_positive_prompt_passes_scene_guard():
    """中文提示词（language=Chinese 时协议要求中文）必须能通过地点校验。

    回归：地点词表要求词只有英文（forest/inn/...），中文管线输出的
    「森林深处…」被判 FL2VA_SCENE_ANCHOR_MISMATCH/PROMPT_MISMATCH，
    修复重试也拿不到可满足的中文词表，两次全挂（2026-09-24 用户实测）。
    """
    first = "远古森林深处，晨雾弥漫，一队冒险者围着一只正在睡觉的巨龙，写实电影感。"
    payload = {
        "scene_anchor": "远古森林深处，一队冒险者围着一只正在睡觉的巨龙",
        "first": frame_payload(first),
        "continuity_constraints": ["同一组人物、同一套服装"],
    }
    brief = Brief(variant="I2VA", duration=30, language="Chinese",
                  plot="一队冒险者在远古森林探险的途中发现了一只正在睡觉的巨龙")
    bundle = fl2va_bundle_from_dict(payload, brief)
    assert "森林" in bundle.first[0].positive_prompt
    assert validate_fl2va_bundle(bundle, duration=30, variant="I2VA") == []


def test_fl2va_generation_persists_prompt_files(tmp_path):
    result = make_fl2va_result()
    directory = save_generation(result, tmp_path / "GEN001")
    assert (directory / "fl2va-prompt.json").exists()
    assert (directory / "fl2va-prompt.md").exists()
    assert (directory / "first-frame-prompt.md").exists()
    assert (directory / "last-frame-prompt.md").exists()
    assert (directory / "generation.json").exists()
    first_frame = (directory / "first-frame-prompt.md").read_text(encoding="utf-8")
    assert "medieval tavern interior" in first_frame


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


def test_multi_scene_journey_bundle_passes():
    """穿越题材（森林→海边）：每组地点词只需被 anchor 或某个关键帧覆盖。"""
    brief = Brief(variant="FL2VA", duration=30, plot="黑夜森林徒步，黎明云海，傍晚海边日落")
    payload = {
        "scene_anchor": "traveller journeying through a misty forest at night, ending on a beach at sunset",
        "first": frame_payload("A tired traveller walks through a dark misty forest at night, headlumpon."),
        "last": frame_payload("The same traveller sits on coastal rocks at sunset, watching waves on the beach."),
        "continuity_constraints": ["Keep the traveller's appearance consistent."],
    }
    assert validate_fl2va_bundle(fl2va_bundle_from_dict(payload, brief), duration=30) == []


def test_multi_scene_group_uncovered_flagged():
    """任一组地点词完全没被任何帧/anchor 覆盖 → 仍然报错。"""
    brief = Brief(variant="FL2VA", duration=30, plot="黑夜森林徒步，黎明云海，傍晚海边日落")
    payload = {
        "scene_anchor": "misty forest at night",
        "first": frame_payload("A tired traveller walks through a dark misty forest."),
        "last": frame_payload("The traveller still deep in the misty forest."),  # 完全没提 beach/coast
        "continuity_constraints": ["Keep the traveller's appearance consistent."],
    }
    with pytest.raises(ValueError, match="FL2VA_SCENE_GROUP_UNCOVERED"):
        fl2va_bundle_from_dict(payload, brief)


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


def test_frame_node_injects_accumulated_user_revisions(monkeypatch):
    """累积的用户修订必须进重出请求（issue #23 的反死接线口径：新字段要看得见）。

    意见原本拼在 ``brief.plot`` 上走私（污染地点守卫与常识判官的判据），现在走
    独立真源 ``state["user_revisions"]``——它若没被节点读走，这一票就等于没接线。
    """
    captured = {}

    def fake_run_agent(agent, message):
        captured["message"] = message
        return json.dumps(bundle_payload())

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    node({
        "brief": brief,
        "shot_table": "start and end states",
        "user_revisions": [
            {"round": 1, "layer": LAYER_FRAME, "text": "天空改成黄昏，要暖金色"},
            {"round": 2, "layer": LAYER_FRAME, "text": "院中加几片红枫"},
        ],
    })
    message = captured["message"]
    assert "用户修订" in message
    assert "天空改成黄昏，要暖金色" in message
    assert "院中加几片红枫" in message, "只带了最后一条——正是『说了就忘』的形态"
    assert "每一条都必须落实" in message


def test_frame_node_omits_revision_block_without_revisions(monkeypatch):
    """没有修订请求逐字不变：自动质检循环不设该键，其替换语义不受影响。"""
    captured = {}

    def fake_run_agent(agent, message):
        captured["message"] = message
        return json.dumps(bundle_payload())

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    node({"brief": brief, "shot_table": "start and end states"})
    assert "用户修订" not in captured["message"]


def test_frame_node_injects_revision_baseline(monkeypatch):
    """修订基线必须进重出请求（issue #25）：它是「上一版产物」的独立入参通道。

    只喂正文文本不够——scene_anchor 与 continuity_constraints 是首尾帧连续性闸门校验的
    字段，所以基线块由完整产物渲染（渲染质量由 tests/test_user_revisions.py 承担，这里
    只证节点确实把它读走：不读，这一票就是没接线）。
    """
    captured = {}

    def fake_run_agent(agent, message):
        captured["message"] = message
        return json.dumps(bundle_payload())

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    previous = {
        "scene_anchor": "中世纪酒馆室内的橡木长桌与壁炉",
        "first": [{"model_family": "zimage", "positive_prompt": "首帧：三人围坐，桌上有酒杯"}],
        "last": [{"model_family": "zimage", "positive_prompt": "尾帧：三人举杯"}],
        "continuity_constraints": ["同一张橡木长桌与同一组任务装备"],
    }
    node({
        "brief": brief,
        "shot_table": "start and end states",
        "user_revisions": [{"round": 1, "layer": LAYER_FRAME, "text": "天空改成黄昏"}],
        nodes.FRAME_BASELINE_KEY: render_baseline_block(previous),
    })
    message = captured["message"]
    assert "修订基线" in message
    assert "橡木长桌与壁炉" in message, "场景锚没进请求——基线在被校验的字段上失锚"
    assert "首帧：三人围坐，桌上有酒杯" in message, "上一版的画面细节没进请求"
    assert "以用户修订为准" in message, "没声明冲突时修订优先（AC-2）"


def test_frame_node_omits_baseline_without_previous_product(monkeypatch):
    """尚无上一版产物（首轮重出 / 自动质检循环）→ 请求里没有基线块，也不报错（AC-5）。"""
    captured = {}

    def fake_run_agent(agent, message):
        captured["message"] = message
        return json.dumps(bundle_payload())

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    node({"brief": brief, "shot_table": "start and end states"})
    assert "修订基线" not in captured["message"]


def test_rewrite_retry_also_carries_the_baseline_and_revisions(monkeypatch):
    """bundle 过不了校验后的**改写**重试请求同样带基线与修订（issue #25）。

    那条路只在首轮产物过不了校验时走，正常路径走不到——正是最容易漏接线的一处
    （澄清当年就漏在这里，见 tests/test_wizard_clarification.py 的同形用例）。
    首轮回复用合法 JSON 但缺尾帧与连续性约束，才落到**重写整份 JSON** 的外层分支；
    坏 JSON 落到内层「转成 JSON」重试，那条只管格式转换，原始结果已在请求里。
    """
    replies = iter([
        json.dumps({"scene_anchor": "medieval tavern interior",
                    "first": {"zimage": {"positive_prompt": "三人围坐"}}}),
        json.dumps(bundle_payload()),
    ])
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or next(replies))
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    node({
        "brief": brief,
        "user_revisions": [{"round": 1, "layer": LAYER_FRAME, "text": "天空改成黄昏"}],
        nodes.FRAME_BASELINE_KEY: render_baseline_block({
            "scene_anchor": "上一版的酒馆：橡木长桌与壁炉",
            "first": [{"model_family": "zimage", "positive_prompt": "首帧：桌上有酒杯"}],
        }),
    })

    assert len(seen) == 2, "首轮产物没触发改写重试，这条用例没测到重试路径"
    assert "修订基线" in seen[1] and "橡木长桌与壁炉" in seen[1], "改写请求丢了基线"
    assert "用户修订" in seen[1] and "天空改成黄昏" in seen[1], "改写请求丢了用户修订"


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
