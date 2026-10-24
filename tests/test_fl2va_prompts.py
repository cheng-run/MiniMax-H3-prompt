"""FL2VA 融合首尾帧提示词测试。"""
import json

import pytest

from minimax_h3_prompt import generation
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.generation import (
    FL2VAFramePrompt,
    FL2VAPromptBundle,
    GenerationResult,
    fl2va_bundle_from_dict,
    render_fl2va_frame_markdown,
    result_from_state,
    validate_fl2va_bundle,
)
from minimax_h3_prompt.graph import nodes, pipeline
from minimax_h3_prompt.topic_generation import save_generation
from minimax_h3_prompt.user_revisions import LAYER_FRAME, render_baseline_block


def frame_payload(prompt: str) -> dict:
    """一帧的模型载荷（现行形状）：只有 zimage 一份——ADR 0013 全链退役后单模型输出。"""
    return {
        "zimage": {"positive_prompt": prompt, "instructions": ["paste to node 10"]},
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


# 多返回的那张帧的内容标记：出现在任何产物/请求里都算没剥干净。
_PHANTOM = "PHANTOM FRAME the model volunteered"


@pytest.mark.parametrize(("variant", "required", "foreign"), [
    ("I2VA", "first", "last"),
    ("L2VA", "last", "first"),
])
def test_foreign_frame_is_stripped_from_the_bundle(variant, required, foreign):
    """模型多返回的关键帧不进 bundle（issue #38）。

    I2VA/L2VA 只认该变体自己的帧（``variant_frame_names``），但模型偶尔会多写一张
    ——#37 那次运行两轮都自己多写了 ``last``。多出来的那张既不被校验、也不该进产物：
    它会渲染成一份用户拿不到的提示词文件，还会被当成整片提示词的帧锚定（`_fl2va_frame_context`
    对 I2VA 照样看 ``last``）。
    """
    payload = _single_frame_payload(required, "Three adventurers inside a medieval tavern interior.")
    payload[foreign] = frame_payload(_PHANTOM)
    brief = Brief(variant=variant, duration=5, plot="中世纪酒馆室内")

    bundle = fl2va_bundle_from_dict(payload, brief)

    assert getattr(bundle, foreign) == (), f"{variant} 保留了模型多返回的 {foreign} 帧"
    assert getattr(bundle, required)


def test_result_from_state_strips_foreign_frame(tmp_path):
    """I2VA 落盘的产物里不得有模型多返回的那张尾帧（issue #38 票面复现的这条路径）。

    它不经 ``fl2va_bundle_from_dict``（非 FL2VA 支路为了兼容旧 state 走的是宽松解析），
    所以剥离得在支路里各做一次——不然票面那条复现照样能过。
    """
    payload = _single_frame_payload("first", "A woman stands on a terrace above the clouds.")
    payload["last"] = frame_payload(_PHANTOM)
    brief = Brief(variant="I2VA", duration=10, language="Chinese", plot="云上天宫的女子")
    result = result_from_state(
        {
            "script": "s",
            "final_prompt": "integrated_multimodal_description: [Shot 1] x.",
            "character_design": "c",
            "prop_design": "p",
            "background_design": "b",
            "fl2va_prompt_bundle": payload,
        },
        brief,
        generation_id="GEN001",
        topic_id="topic",
        project_id="project",
    )

    assert result.fl2va_prompt_bundle.last == ()
    directory = save_generation(result, tmp_path / "GEN001")
    for name in ("last-frame-prompt.md", "fl2va-prompt.json", "generation.json"):
        assert _PHANTOM not in (directory / name).read_text(encoding="utf-8"), \
            f"多返回的尾帧进了 {name}"


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
    )
    bad = FL2VAPromptBundle(scene_anchor="medieval tavern interior", first=(), last=bad_last,
                            continuity_constraints=("c",))
    assert "FL2VA_LAST_FRAME_TIME_EXCEEDS_DURATION" in validate_fl2va_bundle(bad, duration=5, variant="L2VA")


def test_single_location_group_may_be_covered_by_the_scene_anchor():
    """主题含两个地点、词表只命中一个时，帧画面不必画着那个地点（issue #37 实测）。

    现场：主题「云上天宫的女子 → 人间高楼室内练琵琶的女孩」，「室内」由**后半句**命中，
    而 I2VA / FL2VA 的**首帧画的是天宫**——画面里根本没有室内。旧判据走「单组 = 逐帧
    严格」，要求每张帧提示词都含「室内」：模型两次都交不出（它的首帧实文是「云层之上的
    天宫高台临空围栏处…」，一个室内词都没有），阶段 1 跑满 4 分 37 秒后整条抛错。
    新判据：每组地点词被 scene_anchor 或任一帧覆盖即可。
    """
    brief = Brief(variant="I2VA", duration=10, language="Chinese",
                  plot="位于云上的天宫中，一位女人倚靠围栏吹风；随后镜头切至人间，"
                       "一位女孩在高楼的室内练习琵琶，透过打开的窗户看见的。")
    payload = {
        # 模型的改写重试**自己**就把锚点写成两地名对位（实测原文），所以锚点承担全片地点
        "scene_anchor": "两个明确地点对位：①云海之上的天宫高台临空围栏段；②人间高楼室内一间临窗房间",
        "first": frame_payload("竖幅全景写实摄影。云层之上的天宫高台临空围栏处，一位女性倚靠在栏板上。"),
        "continuity_constraints": ["同一组人物与服装"],
    }
    bundle = fl2va_bundle_from_dict(payload, brief)
    assert validate_fl2va_bundle(bundle, duration=10, variant="I2VA") == []


def test_single_location_group_still_flagged_when_nothing_covers_it():
    """覆盖式不是把守卫改没：整份产物（锚点 ＋ 全部帧）都不提该地点 → 仍然报错。

    这条复刻的是**首轮**那次失败（模型的主请求回复里锚点与首帧都没有室内词）——
    首轮该红，改写重试才有意义；改写重试现在拿到了词表（见请求侧用例），那条路才是可赢的。
    """
    brief = Brief(variant="I2VA", duration=10, language="Chinese",
                  plot="位于云上的天宫中，一位女人倚靠围栏吹风；随后镜头切至人间，"
                       "一位女孩在高楼的室内练习琵琶，透过打开的窗户看见的。")
    payload = {
        "scene_anchor": "云层之上的天宫高台临空围栏处，白昼正午前后；栏外是真实高空积云。",
        "first": frame_payload("竖幅全景写实摄影。云层之上的天宫高台临空围栏处，一位女性倚靠在栏板上。"),
        "continuity_constraints": ["同一组人物与服装"],
    }
    with pytest.raises(ValueError, match="FL2VA_SCENE_GROUP_UNCOVERED"):
        fl2va_bundle_from_dict(payload, brief)


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


@pytest.mark.parametrize(("variant", "kept", "foreign"), [
    ("I2VA", "first", "last"),
    ("L2VA", "last", "first"),
])
def test_json_repair_request_names_only_the_variants_frames(monkeypatch, variant, kept, foreign):
    """转 JSON 那条重试的「必须保留哪些帧」按变体现拼（复审指出别在节点里再抄一张表）。

    抄表的代价不只是重复：写死成「first、last」就等于让模型在 I2VA 里把多返回的尾帧
    **保留下来**，与剥离正好对撞。
    """
    replies = iter([
        "这不是 JSON",
        json.dumps(_single_frame_payload(kept, "Three adventurers inside a medieval tavern interior.")),
    ])
    messages: list[str] = []

    def fake_run_agent(agent, message):
        messages.append(message)
        return next(replies)

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant=variant, duration=5, plot="中世纪酒馆室内")

    node({"brief": brief, "shot_table": "start and end states"})

    repair_request = messages[1]
    assert f"必须保留 {kept}、" in repair_request
    assert f"必须保留 {kept}、{foreign}" not in repair_request


def test_frame_node_reports_the_stripped_foreign_frame(monkeypatch, capsys):
    """剥离必须**看得见**（issue #38：「绝不静默失败」）。

    静默丢弃模型产物在本仓是明文违规，所以多返回的帧被剥时节点要当场说清丢了哪张。
    """
    payload = json.dumps(_single_frame_payload(
        "first", "Three adventurers inside a medieval tavern interior."))
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: json.dumps({
        **json.loads(payload), "last": frame_payload(_PHANTOM),
    }))
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")

    output = node({"brief": brief, "shot_table": "start and end states"})

    assert not output["fl2va_prompt_bundle"]["last"]
    warning = capsys.readouterr().out
    assert "[警告]" in warning and "last" in warning, f"剥离没有告警：{warning!r}"


def test_stripped_foreign_frame_never_anchors_the_video_prompt(monkeypatch):
    """被剥的帧不许当整片提示词的帧锚定（issue #38 的第二个后果）。

    真实运行里这条链是连着的：帧节点产出 bundle → 提示词工程师节点读它渲染
    「关键帧锚定」块。而 `_fl2va_frame_context` 对 I2VA **照样读 last**——帧节点剥不干净
    的话，正文会去收束到一张用户根本不会生成的图（这一危害不进任何票面清单，只有把两个
    节点串起来跑才看得见）。
    """
    payload = json.dumps(_single_frame_payload(
        "first", "Three adventurers inside a medieval tavern interior."))
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: json.dumps({
        **json.loads(payload), "last": frame_payload(_PHANTOM),
    }))
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    state = {"brief": brief, "script": "script"}
    state.update(nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})(state))

    captured: dict[str, str] = {}

    def capture(agent, message):
        captured["message"] = message
        return "video prompt"

    monkeypatch.setattr(nodes, "run_agent", capture)
    nodes._make_prompt_engineer_node({"prompt_engineer": object()})(state)

    assert _PHANTOM not in captured["message"], "多返回的帧当上了整片正文的帧锚定"


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


# ---------------------------------------------------------------------------
# 画布接线与构图/光线字段（issue #33 生图侧）
#
# 帧节点是链上唯一真跑的生图节点，所以它的**真实请求字符串**就是生图侧的那条缝。
# 断言一律打在请求串上，不打在 helper 的返回值上——本仓的祖训（`test_segment_prompts`
# 那条到达性用例）：常量对了、请求组装时漏掉，测 helper 照样绿。
# ---------------------------------------------------------------------------

def _frame_node_request(monkeypatch, variant: str = "I2VA",
                        plot: str = "中世纪酒馆室内", canvases: dict | None = None) -> str:
    """跑一次帧节点，返回它真正发出去的那条用户请求串。"""
    captured: dict[str, str] = {}
    payload = json.dumps(_single_frame_payload(
        "first", "Three adventurers inside a medieval tavern interior."))

    def fake_run_agent(agent, message):
        captured["message"] = message
        return payload

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant=variant, duration=5, plot=plot, canvases=canvases or {})
    node({"brief": brief, "shot_table": "start and end states"})
    return captured["message"]


def test_frame_bundle_roundtrips_composition_and_lighting():
    """构图/光线字段能被解析器读进来、也能写回去（issue #33 AC-3 的括号那半）。

    这两个字段一直存在却没人写；顺带确认「解析器已支持读取与写回」这句成立——
    不成立的话，「让它们有值」也就没地方落。
    """
    payload = bundle_payload()
    payload["first"]["zimage"]["composition"] = "竖幅三段式，主体偏下、天空留白在上"
    payload["first"]["zimage"]["lighting"] = "暖光自右侧门缝低角度溢出，光比温和"
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(payload, brief)

    def zimage_row(b):
        return next(r for r in b.first if r.model_family == "zimage")

    row = zimage_row(bundle)
    assert row.composition == "竖幅三段式，主体偏下、天空留白在上"
    assert row.lighting == "暖光自右侧门缝低角度溢出，光比温和"
    restored = FL2VAPromptBundle.from_dict(bundle.to_dict())
    assert (zimage_row(restored).composition, zimage_row(restored).lighting) \
        == (row.composition, row.lighting), "写回再读时两个字段丢了"


def test_frame_node_request_names_the_required_scene_terms(monkeypatch):
    """帧节点的请求里必须**点名**主题要求的地点词组（issue #37）。

    校验器做的是字面子串判定，而请求里此前只有一句「主题地点是硬约束」——模型无从知道要的
    是哪几个字面。实测该次运行的请求串：主请求与改写请求里既无「场景词」也无 indoor/inside，
    两轮都交不出「室内」，改写重试那句「使其通过主题地点校验」在信息上**不可赢**。
    整片路径早有这条接线（`pipeline._theme_repair_injection` → `theme_requirements_text`），
    帧路径缺的正是它。
    """
    message = _frame_node_request(monkeypatch)  # plot = 中世纪酒馆室内 → 命中酒馆组 + 室内组
    assert "【场景词】" in message, "帧节点请求里没有场景词块（角色提示词会指向一个不存在的块）"
    for term in ("酒馆", "室内", "tavern", "indoor"):
        assert term in message, f"请求里没点名必需地点词：{term}"


def test_scene_term_block_is_present_even_without_a_lexicon_hit(monkeypatch):
    """主题没命中词表时【场景词】块也必须在场，否则角色提示词指向一个不存在的块（#37 复审）。

    角色提示词写着「主题要求的地点词见输入里的【场景词】块」；而 `_ctx` 会跳过空值，
    无约束时（实测 plot＝「深夜便利店一只橘猫推门跳上柜台」）请求里根本没有这块——**悬空
    指针**，正是本票在另外两处点名要避免的形态。同 #33 的【画布】块：那块是**恒在**的。
    """
    message = _frame_node_request(monkeypatch, plot="深夜便利店一只橘猫推门跳上柜台")
    assert "【场景词】" in message, "无约束时请求里缺了场景词块（角色提示词的指针悬空）"
    assert "主题未命中" in message, "块里没说清「本片无地点硬约束」，模型会以为漏看了词表"


def test_frame_node_request_carries_the_canvas_without_flux2(monkeypatch):
    """帧节点的请求里必须带**画布尺寸**，且**不得出现 Flux.2 行**（issue #48 AC3）。

    模型不知道自己在给哪个画幅写构图，就会按一种画幅的直觉安排主体位置——同一条
    构图指令在不同画幅上语义不同。Flux.2 已全链退役（ADR 0013）：【画布】块只渲染
    现存家族，请求里连「Flux.2」字样都不该再有。变异检验：把 flux2 行放回家族表，
    这条会红（已实测）。
    """
    message = _frame_node_request(monkeypatch)
    # 期望串是**字面量**（zimage 默认画布）：从 `image_canvas_size` 现算就与实现同构，
    # 组装断线也一起绿——「真的从表里取数」由下面的 profile 表用例钉。
    assert "640 × 1280" in message, "帧节点请求里没有 Z-Image 的画布尺寸"
    assert "Flux.2" not in message and "flux2" not in message, \
        "Flux.2 已退役，请求里不得再出现 Flux.2 行"


def test_frame_node_canvas_follows_the_image_profile_table(monkeypatch):
    """画布取自**生图 profile 默认表**，不是请求组装里另抄的一份常量（issue #33 AC-2）。

    那张表以前只在渲染层被读（给人看「推荐尺寸」），从没进过任何 LLM 请求；现在它同时喂着
    请求里的画布，两处各写一份数就会漂。做法：把表改成不可能巧合命中的尺寸，看请求跟不跟。
    """
    monkeypatch.setitem(generation.IMAGE_PROFILE_DEFAULTS, "zimage",
                        generation.ImageProfile("zimage_t2i_v1", "10", 111, 222, "candidate"))
    message = _frame_node_request(monkeypatch)
    assert "111 × 222" in message, "请求里的画布不是从 profile 默认表取的（另抄了一份常量）"


def test_frame_node_request_carries_the_user_selected_canvases(monkeypatch):
    """帧节点同样吃用户选定画布（issue #42）：选定值进请求，分套按画幅族给指示。

    期望串是字面量（960×1152 约分为 5:6）：Z-Image 选了近方 → 正方套——分套绑
    **画幅族**、不绑模型身份。帧链与资产链共用【画布】块的组装口，这条钉的是
    「用户选定辐射到帧节点」——它与上面那条默认表回落用例互补。族外降通用的指示
    由 ``tests/test_asset_image_prompts.py`` 的同名用例在资产侧钉住（同一组装口）。
    """
    message = _frame_node_request(monkeypatch, canvases={"zimage": (960, 1152)})
    assert "5:6 · 960 × 1152（近方）" in message, "帧节点请求里没有 Z-Image 的选定画布"
    assert "构图用正方套" in message, "近方画布没拿到正方套指示"
    assert "640 × 1280" not in message, "选定了画布却还带着系统钦定的推荐尺寸"


def test_frame_products_carry_the_user_selected_canvas():
    """帧产物行吃用户选定画布（issue #42 AC1/AC3）：行尺寸即选定值，画布行与请求同源。

    bundle 的行尺寸在解析处就换成选定值（``fl2va_bundle_from_dict`` 认识 brief），
    于是 state 往返、向导渲染、GenerationResult 渲染拿到的都是同一份数字；渲染的
    画布行取 ``canvas_display``，与请求【画布】块逐字同源。
    """
    brief = Brief(variant="FL2VA", duration=5, style="live-action realism",
                  plot="中世纪酒馆室内，冒险者团队庆祝",
                  canvases={"zimage": (700, 1300)})
    state = {"script": "分场剧本", "final_prompt": "视频提示词正文",
             "fl2va_prompt_bundle": bundle_payload()}
    result = result_from_state(state, brief, generation_id="GEN001",
                               topic_id="topic", project_id="project")

    rows = {r.model_family: r for r in result.fl2va_prompt_bundle.first}
    assert set(rows) == {"zimage"}, f"产物该只出 Z-Image 一版，实际行：{sorted(rows)}"
    assert (rows["zimage"].width, rows["zimage"].height) == (700, 1300)

    rendered = render_fl2va_frame_markdown(result, "first")
    assert "画布：`7:13 · 700 × 1300（近竖）`" in rendered, "产物没有 Z-Image 的选定画布行"
    assert "Flux.2" not in rendered and "flux2" not in rendered, \
        "Flux.2 已退役，产物展示不得再出现 Flux.2 行"
    assert "推荐尺寸" not in rendered, "「推荐尺寸」措辞还活着——系统钦定时代的旧标签"


def test_model_hallucinated_size_cannot_beat_the_selected_canvas():
    """模型 JSON 里幻觉出的 width/height **压不过**用户选定（issue #42 复审）。

    `FL2VAFramePrompt.from_dict` 认这两个键；解析处若「有值就用」，产物尺寸就与请求
    【画布】块漂开——画布是请求下发的数据，不是模型的自由发挥。
    """
    payload = bundle_payload()
    payload["first"]["zimage"]["width"] = 111
    payload["first"]["zimage"]["height"] = 222
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内，冒险者团队庆祝",
                  canvases={"zimage": (700, 1300)})
    bundle = fl2va_bundle_from_dict(payload, brief)
    zrow = next(r for r in bundle.first if r.model_family == "zimage")
    assert (zrow.width, zrow.height) == (700, 1300), \
        f"模型幻觉尺寸赢了：{(zrow.width, zrow.height)}"


def test_frame_level_fallback_shape_also_obeys_the_selected_canvas():
    """帧级回退形状（帧 dict 无模型子键）同样被挡住——复审实测它绕过了家族子键覆盖。

    该形状走 `FL2VAPromptBundle.from_dict` 的整帧展开回退，帧级 width/height 曾原样
    直通产物：请求【画布】块写 700×1300、渲染出 111×222，全程无报错。尺寸权威解析
    在 ``from_dict`` 层（传入 canvases 即无条件取读取口），任何形状都从同一层过。
    """
    payload = {
        "scene_anchor": "medieval tavern interior",
        "first": {"positive_prompt": "Three adventurers inside a medieval tavern interior.",
                  "width": 111, "height": 222},
        "continuity_constraints": ["Keep the same characters and tavern layout."],
    }
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内，冒险者团队庆祝",
                  canvases={"zimage": (700, 1300)})
    bundle = fl2va_bundle_from_dict(payload, brief)
    assert len(bundle.first) == 1
    row = bundle.first[0]
    assert (row.width, row.height) == (700, 1300), \
        f"帧级回退形状的幻觉尺寸赢了：{(row.width, row.height)}"


def test_result_from_state_threads_canvases_into_prompts_and_bundle():
    """``result_from_state`` 两支路都把选定画布送进产物（复审：删掉递参后全套照绿）。

    CLI/主题路径的产物全走这里：`image_prompt_variants(state, brief.canvases)` 与
    非 FL2VA 支路的 bundle 解析一旦被删参，产物画布行静默回落默认、与同份
    generation.json 的 brief_snapshot.canvases 自相矛盾（AC1/AC3 可静默破坏）。
    """
    # 资产图提示词只属于多参路线（issue #78）：本用例的「提示词」那一半必须走 ref，
    # base 下它按路线返回空元组；同一份 state 用 base brief 再跑一次是本文件的成对断言。
    brief = Brief(mode="ref", variant="I2VA", duration=5, plot="漂浮的水母群与光斑",
                  canvases={"zimage": (700, 1300)})
    state = {
        "script": "分场剧本", "final_prompt": "视频提示词正文",
        "character_design": "人物设计", "prop_design": "道具设计", "background_design": "背景设计",
        "character_image_prompts": [{"name": "红裙女孩", "positive_prompt": "人物提示词"}],
        "fl2va_prompt_bundle": {
            "scene_anchor": "光斑",
            "first": {"positive_prompt": "水母群与光斑的第一帧画面", "width": 111, "height": 222},
            "continuity_constraints": ["保持同一片水母群"],
        },
    }
    result = result_from_state(state, brief, generation_id="GEN001",
                               topic_id="topic", project_id="project")

    assert result.image_prompts, "资产产物为空，断言落空"
    assert all((v.width, v.height) == (700, 1300) for v in result.image_prompts), \
        "result_from_state 的资产产物没带选定画布（那两个递参之一被删了）"
    row = result.fl2va_prompt_bundle.first[0]
    assert (row.width, row.height) == (700, 1300), \
        "非 FL2VA 支路的 bundle 没按选定画布解析（幻觉尺寸赢了）"

    # 成对断言（issue #78）：同一份 state 换 base 路线，资产产物必须为空，
    # 而关键帧 bundle 的画布透传照旧——路线只决定资产集，不拿走帧链的能力。
    base_brief = Brief(variant="I2VA", duration=5, plot="漂浮的水母群与光斑",
                       canvases={"zimage": (700, 1300)})
    base_result = result_from_state(state, base_brief, generation_id="GEN001",
                                    topic_id="topic", project_id="project")
    assert base_result.image_prompts == (), "首尾帧路线的结果对象仍带着资产图提示词"
    base_row = base_result.fl2va_prompt_bundle.first[0]
    assert (base_row.width, base_row.height) == (700, 1300)


def test_frame_node_request_asks_for_composition_and_lighting(monkeypatch):
    """请求里点名 composition / lighting 两个字段（issue #33 AC-3）。

    解析器一直支持读写这两个字段，却**没人写**：实测仓库里 9 份真实产物（5 个会话）的
    18 行全是空串。字段名进请求是「有生产者」的第一步——请求里不提、规格里没位置，模型
    只能把构图与光线全塞进 positive_prompt 的自然语言里，复跑同一张图就做不到
    「只改一个维度」。
    """
    message = _frame_node_request(monkeypatch)
    assert "composition" in message, "帧节点请求里没点名 composition 字段"
    assert "lighting" in message, "帧节点请求里没点名 lighting 字段"


def test_rewrite_retry_also_carries_the_canvas(monkeypatch):
    """bundle 过不了校验后的**改写**重试请求同样带画布（issue #33）。

    那条路只在首轮产物过不了校验时走，正常路径走不到——本仓最容易漏接线的一处
    （澄清、#25 的基线与修订都漏在这里过）。重写整份 JSON 却不提画布，等于让模型按
    自己的直觉重新安排构图。

    相对的，composition / lighting 的**字段要求**不必在这里重复：它住在角色系统提示词里
    （每次调用都在场），而画布是**本次请求的数据**——同「起步澄清」「用户修订」，不在
    系统提示词里，只能跟着请求走。
    """
    replies = iter([
        json.dumps({"scene_anchor": "medieval tavern interior",
                    "first": {"zimage": {"positive_prompt": "三人围坐"}}}),
        json.dumps(_single_frame_payload(
            "first", "Three adventurers inside a medieval tavern interior.")),
    ])
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or next(replies))
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    node({"brief": brief})

    assert len(seen) == 2, "首轮产物没触发改写重试，这条用例没测到重试路径"
    width, height = generation.image_canvas_size("zimage")
    assert f"{width} × {height}" in seen[1], "改写请求丢了画布尺寸"


def _frame_prompt_spec() -> str:
    """帧提示词工程师的**角色提示词正文**（不含拼接上去的生图半真源）。

    为什么读 .md 而不走 `load_role_prompt`：那条路会把生图半真源一并拼进来，而真源的
    「结构化字段」一节同样提到这两个字段名——用加载口断言就分不清「规格里有没有」。
    输出格式与既有禁令都归角色提示词管（真源只讲选词与写法，见 ADR 0006）。
    """
    from minimax_h3_prompt.agents import PROMPTS_DIR

    return (PROMPTS_DIR / "frame_prompt_engineer.md").read_text(encoding="utf-8")


def test_frame_prompt_engineer_spec_owns_the_two_json_fields():
    """输出 JSON 的字段位置写在角色提示词正文里（issue #33 AC-3）。"""
    spec = _frame_prompt_spec()
    for field in ('"composition"', '"lighting"'):
        assert field in spec, f"帧提示词工程师的输出规格里没有 {field} 的位置"


def test_frame_prompt_engineer_keeps_its_existing_static_prohibitions():
    """AC-4 的**既有**那半：角色提示词里原本就有的静态侧禁令不许被这次改动挤掉（#33 复审）。

    该改动的 net 效果是「只增不删」，但这只靠 diff 恰好如此——没有东西拦得住后来者
    在加/改构图光线规则时把这几条禁令顺手删掉。
    """
    spec = _frame_prompt_spec()
    for prohibition in ("不写 camera movement", "剪辑", "时间码", "质量标签", "中文"):
        assert prohibition in spec, f"帧提示词工程师丢了既有禁令：{prohibition}"


def test_frame_prompt_engineer_outputs_a_single_zimage_version():
    """帧链角色提示词只出 Z-Image 一版（issue #48 AC2）：无 flux2 输出键、无双模型纪律。

    「两模型分别重写」的纪律随 Flux.2 全链退役（ADR 0013）从角色提示词移除——
    变异检验：把 flux2 输出块或纪律句放回 .md，这条会红（已实测）。
    """
    spec = _frame_prompt_spec()
    assert '"zimage"' in spec, "输出规格里应保留 zimage 版本层"
    assert "flux2" not in spec.lower() and "flux.2" not in spec.lower(), \
        "帧链角色提示词不得再有 flux2 输出键"
    assert "分别重写" not in spec, "「两模型分别重写」纪律应随 Flux.2 退役删除"


def test_legacy_flux2_keys_in_payloads_are_read_as_absent():
    """旧载荷的 flux2 子键按弃读惯例处理（issue #48，ADR 0013）：仍可读，只出 Z-Image 一条。

    不做静默兼容——不是把 flux2 当 zimage 改名续用，而是当它不存在：产物里没有任何
    flux2 痕迹。
    """
    payload = {
        "scene_anchor": "medieval tavern interior",
        "first": {
            "zimage": {"positive_prompt": "Three adventurers inside a medieval tavern interior."},
            "flux2": {"positive_prompt": "旧 Flux.2 版首帧（不该进产物）"},
        },
        "continuity_constraints": ["Keep the same characters and tavern layout."],
    }
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(payload, brief)

    assert [r.model_family for r in bundle.first] == ["zimage"]
    assert "旧 Flux.2 版" not in bundle.first[0].positive_prompt
    assert "Flux.2" not in json.dumps(bundle.to_dict(), ensure_ascii=False)


def test_legacy_flux2_rows_in_list_shape_are_dropped():
    """旧产物（to_dict 列表形）里的 flux2 行弃读：载入不报错、不进产物（issue #48）。

    会话/落盘产物的帧是列表、每项带 model_family（节点落盘即 ``bundle.to_dict()``）；
    旧会话里躺着 flux2 行，续接重展示走的就是这条读法。
    """
    raw = {
        "scene_anchor": "medieval tavern interior",
        "first": [
            {"frame": "first", "model_family": "zimage",
             "scene_anchor": "medieval tavern interior",
             "positive_prompt": "Three adventurers inside a medieval tavern interior."},
            {"frame": "first", "model_family": "flux2",
             "scene_anchor": "medieval tavern interior",
             "positive_prompt": "旧 Flux.2 版首帧（不该进产物）"},
        ],
        "continuity_constraints": ["Keep the same characters and tavern layout."],
    }
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    bundle = fl2va_bundle_from_dict(raw, brief)

    assert [r.model_family for r in bundle.first] == ["zimage"]
    assert "旧 Flux.2 版" not in bundle.first[0].positive_prompt


def test_canvas_block_covers_every_model_in_the_profile_table(monkeypatch):
    """【画布】块的行数跟着**生图 profile 默认表**走（issue #33 复审）。

    家族清单若在本模块里另抄一份，将来加第三个生图家族时【画布】块会**静默少一行**
    ——模型就不知道那张画布该按什么构图写。做法：往表里加一个第三家族，看块里有没有它。
    """
    monkeypatch.setitem(generation.IMAGE_PROFILE_DEFAULTS, "zimage_new",
                        generation.ImageProfile("zimage_new_v1", "12", 768, 1344, "candidate"))
    monkeypatch.setitem(generation.IMAGE_FAMILY_LABELS, "zimage_new", "Z-Image New")
    message = _frame_node_request(monkeypatch)
    assert "768 × 1344" in message, "加了第三个生图家族，【画布】块却没跟着多一行"


def test_json_repair_retry_also_carries_the_canvas_and_the_two_fields(monkeypatch):
    """首轮不是 JSON 时那条**转换**重试请求同样带画布与两个字段（issue #33 复审）。

    它常被当成「纯格式转换」而豁免，但它的正文里有一句**内容清单**（要保留哪些键），
    而且它用的是同一份角色系统提示词——提示词现在写着「画幅见输入里的【画布】块」，
    这条请求不带那块就是一个**悬空指针**。
    """
    replies = iter([
        "不是 JSON，随手写的一段话",
        json.dumps(_single_frame_payload(
            "first", "Three adventurers inside a medieval tavern interior.")),
    ])
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or next(replies))
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    node({"brief": brief})

    assert len(seen) == 2, "首轮坏 JSON 没触发转换重试，这条用例没测到重试路径"
    width, height = generation.image_canvas_size("zimage")
    assert f"{width} × {height}" in seen[1], "转换重试请求没带画布（角色提示词会指向一个不存在的块）"
    assert "composition" in seen[1] and "lighting" in seen[1], "转换重试请求的内容清单里没有这两个字段"


def test_rewrite_retry_also_carries_the_scene_terms(monkeypatch):
    """bundle 过不了校验后的**改写**重试请求同样要点名地点词组（issue #37）。

    这条重试此前是最不可赢的一处：指令写着「使其通过主题地点校验」，却从不告诉模型校验器
    要哪几个字面（实测该次运行两轮都没过）。与画布同理——词表是**本次请求的数据**，
    不在角色系统提示词里，只能跟着请求走。
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
    node({"brief": brief})

    assert len(seen) == 2, "首轮产物没触发改写重试，这条用例没测到重试路径"
    assert "【场景词】" in seen[1], "改写请求丢了场景词块"
    assert "indoor" in seen[1], "改写请求没点名必需地点词"


def test_json_repair_retry_also_carries_the_scene_terms(monkeypatch):
    """首轮不是 JSON 时那条**转换**重试请求同样要点名地点词组（issue #37）。

    同画布那次修正的理由：这条请求用的也是同一份角色系统提示词，而提示词现在指向
    输入里的【场景词】块——不带那块就是一个**悬空指针**。
    """
    replies = iter([
        "不是 JSON，随手写的一段话",
        json.dumps(_single_frame_payload(
            "first", "Three adventurers inside a medieval tavern interior.")),
    ])
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or next(replies))
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant="I2VA", duration=5, plot="中世纪酒馆室内")
    node({"brief": brief})

    assert len(seen) == 2, "首轮坏 JSON 没触发转换重试，这条用例没测到重试路径"
    assert "【场景词】" in seen[1], "转换重试请求没带场景词块（角色提示词会指向一个不存在的块）"
    assert "indoor" in seen[1], "转换重试请求没点名必需地点词"


def test_frame_prompt_engineer_points_at_the_scene_term_block():
    """角色提示词的**指针**必须与节点实际发出去的块键一致（issue #37）。

    原先规格里写的是「如果主题明确说『酒馆』或『室内』，必须直接写」，只举了两个族、
    且读作「写进每个正向提示词」——正是误报的源头（天宫那一帧被要求含「室内」）。
    改成指向输入里的块之后，规格不再复述词表（词表是请求里的数据，同画布）。
    """
    spec = _frame_prompt_spec()
    assert "【场景词】" in spec, "角色提示词没有指向输入里的场景词块"
    assert '必须直接写"酒馆"/"室内"' not in spec, "规格里仍留着「每个提示词都必须直接写」的旧口径"


def test_frame_markdown_shows_composition_and_lighting(tmp_path):
    """两个字段要**送到人看的文件里**（issue #33 复审）。

    票面那句目的——「复跑同一张图时只改一个维度」——只有在 `first-frame-prompt.md` 里
    看得见这两行时才成立；只落在 session-state JSON 里等于又要人去挖。
    """
    payload = bundle_payload()
    payload["first"]["zimage"]["composition"] = "竖幅三段式，主体偏下"
    payload["first"]["zimage"]["lighting"] = "暖光自右侧门缝低角度溢出"
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")
    result = result_from_state(
        {
            "script": "The adventurers celebrate in the tavern.",
            "final_prompt": "How the reference pictures align with the target video — Picture 1 aligns with 0.00 seconds; Picture 2 aligns with 5.00 seconds.\n\nintegrated_multimodal_description: [Shot 1] The group celebrates.\n\noverall_soundscape: Tavern ambience.\n\nnon_diegetic_music: N/A",
            "character_design": "three adventurers",
            "prop_design": "wooden mugs and quest gear",
            "background_design": "medieval tavern interior",
            "fl2va_prompt_bundle": payload,
        },
        brief, generation_id="GEN001", topic_id="topic", project_id="project",
    )
    markdown = render_fl2va_frame_markdown(result, "first")
    assert "竖幅三段式，主体偏下" in markdown, "构图字段没进人看的首帧提示词文件"
    assert "暖光自右侧门缝低角度溢出" in markdown, "光线字段没进人看的首帧提示词文件"


def test_frame_markdown_is_unchanged_when_the_two_fields_are_empty(tmp_path):
    """两个字段为空时渲染结果与改动前逐字一致（老产物的 18 行都是空串）。

    不做这个条件渲染的话，每份老产物都会多出两行空标题——那是噪声，也让「改了没有」
    这件事在 diff 里变得不可读。
    """
    result = make_fl2va_result()
    markdown = render_fl2va_frame_markdown(result, "first")
    assert "构图：" not in markdown and "光线：" not in markdown
