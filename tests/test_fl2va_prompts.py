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


# ---------------------------------------------------------------------------
# 画布接线与构图/光线字段（issue #33 生图侧）
#
# 帧节点是链上唯一真跑的生图节点，所以它的**真实请求字符串**就是生图侧的那条缝。
# 断言一律打在请求串上，不打在 helper 的返回值上——本仓的祖训（`test_segment_prompts`
# 那条到达性用例）：常量对了、请求组装时漏掉，测 helper 照样绿。
# ---------------------------------------------------------------------------

def _frame_node_request(monkeypatch, variant: str = "I2VA") -> str:
    """跑一次帧节点，返回它真正发出去的那条用户请求串。"""
    captured: dict[str, str] = {}
    payload = json.dumps(_single_frame_payload(
        "first", "Three adventurers inside a medieval tavern interior."))

    def fake_run_agent(agent, message):
        captured["message"] = message
        return payload

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_fl2va_frame_prompt_node({"frame_prompt_engineer": object()})
    brief = Brief(variant=variant, duration=5, plot="中世纪酒馆室内")
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


def test_frame_node_request_carries_both_canvases(monkeypatch):
    """帧节点的请求里必须带**画布尺寸**（issue #33 AC-2）。

    模型不知道自己在给哪个画幅写构图，就会按一种画幅的直觉安排主体位置；而 Z-Image 是
    竖幅、Flux.2 是正方，同一条构图指令在两种画布上语义不同——这是既有纪律「两模型必须
    分别重写」的物理依据，不只是纪律要求。

    **两行各一条断言**，不合并：它们现在同住生图 profile 默认表，将来可能分叉，
    合并成一条会在分叉时一起绿。
    """
    message = _frame_node_request(monkeypatch)
    width, height = generation.image_canvas_size("zimage")
    assert f"{width} × {height}" in message, "帧节点请求里没有 Z-Image 的画布尺寸（竖幅）"
    width, height = generation.image_canvas_size("flux2")
    assert f"{width} × {height}" in message, "帧节点请求里没有 Flux.2 的画布尺寸（正方）"


def test_frame_node_canvas_follows_the_image_profile_table(monkeypatch):
    """画布取自**生图 profile 默认表**，不是请求组装里另抄的一份常量（issue #33 AC-2）。

    那张表以前只在渲染层被读（给人看「推荐尺寸」），从没进过任何 LLM 请求；现在它同时喂着
    请求里的画布，两处各写一份数就会漂。做法：把表改成不可能巧合命中的尺寸，看请求跟不跟。
    """
    monkeypatch.setitem(generation.IMAGE_PROFILE_DEFAULTS, "zimage",
                        generation.ImageProfile("zimage_t2i_v1", "10", 111, 222, "candidate"))
    message = _frame_node_request(monkeypatch)
    assert "111 × 222" in message, "请求里的画布不是从 profile 默认表取的（另抄了一份常量）"


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
