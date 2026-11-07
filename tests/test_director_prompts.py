"""导演台提示词方言（issue #103）：按模式分派、绝对时间轴、路线能力判据。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. 路线能力判据（``brief_parser`` 的两个纯函数）与 ``_brief_block`` 送模式；
2. 校验器/组装器**不许把 director 当 base**（#103 硬约束：不许靠「反正不走那条路」）；
3. ``director/prompts.py`` 的方言产物：r2v 六节外壳＋时序写法＋无状态句，
   t2v/i2v/fl2v 单段正文，已取消的 v2v/rv2v/mixed 报模式无效；时间轴注记按帧重算成绝对位置；
4. 向导接线：逐段落 ``GENxxx/director/segment-NN.md`` 并出站段号/类型/给图/路径。

断言只碰产物与返回值，不断言私有调用顺序。
"""
from __future__ import annotations

import re

import pytest

from minimax_h3_prompt.brief_parser import (
    Brief,
    RefItem,
    director_frame_variant,
    route_emits_asset_prompts,
    route_emits_frame_prompts,
)


# ---------------------------------------------------------------------------
# 路线能力判据（设计 §4「唯一真源」）：不许只写 or brief.is_director
# ---------------------------------------------------------------------------

def test_route_capabilities_are_derived_from_route_and_mode():
    """真值表：ref 全产资产图；director 只有 r2v 产资产图、只有 i2v/fl2v 产帧图。

    为什么必须按模式分派：``t2v`` 没有参考素材可出词，``i2v`` 只要首帧。
    一句 ``or brief.is_director`` 会让每个导演台模式都白跑三个资产图节点（设计 §4）。
    """
    cases = [
        # (mode, director_mode, variant, 产资产图, 产帧图)
        ("base", "", "T2VA", False, False),
        ("base", "", "I2VA", False, True),
        ("base", "", "FL2VA", False, True),
        ("base", "", "L2VA", False, True),
        ("ref", "", "T2VA", True, False),
        ("director", "t2v", "T2VA", False, False),
        ("director", "i2v", "T2VA", False, True),
        ("director", "fl2v", "T2VA", False, True),
        ("director", "r2v", "T2VA", True, False),
    ]
    for mode, director_mode, variant, assets, frames in cases:
        brief = Brief(mode=mode, variant=variant, director_mode=director_mode)
        assert route_emits_asset_prompts(brief) is assets, f"{mode}/{director_mode} 资产图判据错"
        assert route_emits_frame_prompts(brief) is frames, f"{mode}/{director_mode} 帧图判据错"


def test_director_frame_variant_maps_modes_not_the_placeholder_variant():
    """导演台的 ``brief.variant`` 是 T2VA 占位（ADR 0024），有效变体必须从模式映射。

    读 ``brief.variant`` 出词 = 每条导演台会话都按 T2VA 走，i2v 拿不到首帧词而**不报错**。
    """
    assert director_frame_variant(Brief(mode="director", director_mode="i2v")) == "I2VA"
    assert director_frame_variant(Brief(mode="director", director_mode="fl2v")) == "FL2VA"
    assert director_frame_variant(Brief(mode="director", director_mode="t2v")) == ""
    assert director_frame_variant(Brief(mode="director", director_mode="r2v")) == ""
    # 其余路线原样返回自己的变体（base 的帧链不能因此改口径）
    assert director_frame_variant(Brief(mode="base", variant="FL2VA")) == "FL2VA"
    assert director_frame_variant(Brief(mode="ref", variant="T2VA")) == "T2VA"


def test_brief_block_sends_director_mode_to_the_request():
    """方言分派要拿到用户选过的模式：请求里没有它，所有模式会拿到同一份口径（设计 §3.3）。"""
    from minimax_h3_prompt.graph.nodes import _brief_block

    text = _brief_block(Brief(mode="director", director_mode="i2v", plot="雨夜旧信"))
    assert "导演台模式" in text and "i2v" in text

    # base/ref 的请求逐字不变：空值被 _ctx 跳过，不出现这个标签。
    assert "导演台模式" not in _brief_block(Brief(mode="base", variant="I2VA"))


def test_session_route_filter_keeps_asset_prompts_only_where_they_belong():
    """`_route_filtered_state` 的过滤判据换成路线能力判据（#103 点名的接线点）。

    director t2v 不该带着资产图提示词续跑；director r2v 必须保留（本段要用 <Picture N>）。
    """
    from minimax_h3_prompt import session_store
    from minimax_h3_prompt.graph.state import ASSET_PROMPT_KEYS

    raw = {key: [{"name": "甲"}] for key in ASSET_PROMPT_KEYS}
    raw["shot_table"] = "分镜表"

    kept = session_store._route_filtered_state(
        raw, Brief(mode="director", director_mode="r2v"))
    assert all(key in kept for key in ASSET_PROMPT_KEYS)

    dropped = session_store._route_filtered_state(
        raw, Brief(mode="director", director_mode="t2v"))
    assert all(key not in dropped for key in ASSET_PROMPT_KEYS)
    assert dropped["shot_table"] == "分镜表"


# ---------------------------------------------------------------------------
# 校验器与组装器：director 有显式分支，不许落进 base 的三节口径
# ---------------------------------------------------------------------------

def test_validator_does_not_validate_director_as_base():
    """``validate_prompt`` 是 ``mode == "ref"`` 的二元分叉，director 会静默按 base 验。

    导演台的产物是**逐段方言**（``director.prompts``），整片提示词只是阶段 1 的占位：
    这里既不能按三节判红，也不能假装它通过了 base 校验——给出显式提示、指回正确的口。
    """
    from minimax_h3_prompt.tools.h3_validator import validate_prompt

    issues = validate_prompt("A rainy night outside a convenience store.", "director")
    assert not [i for i in issues if i.severity == "error"], "导演台整片占位不许按 base 判红"
    assert any(i.code == "DIRECTOR_WHOLE_PROMPT_UNUSED" for i in issues), \
        "必须有显式分支说明整片提示词不适用，而不是静默走 base"


def test_assembler_does_not_append_base_sections_for_director():
    """``assemble_and_repair`` 不许给 directer 补齐 base 三节（AC 硬条）。"""
    from minimax_h3_prompt.output.assembler import assemble_and_repair

    prompt = "A rainy night. [Shot 1] She stops at the storefront."
    repaired, report = assemble_and_repair(prompt, "director")
    assert repaired.strip() == prompt.strip(), "导演台提示词被改了"
    assert "integrated_multimodal_description" not in repaired
    assert "导演台" in report, "跳过补齐这件事必须说出来"

    # 两条老路线的补齐行为一字不变
    base_out, _ = assemble_and_repair("body", "base")
    assert "integrated_multimodal_description" in base_out
    ref_out, _ = assemble_and_repair("body", "ref")
    assert "subject_definitions" in ref_out


def test_reference_consistency_request_carries_the_registry_for_director_r2v(monkeypatch):
    """H6：导演台 r2v 的 reference_consistency 请求必须带【参考资产】登记。

    判据原先读 ``brief.mode == "ref"``，导演台 r2v 恒得空串 → 节点拿到的参考资产是
    「（无）」，六节外壳的 subject_definitions 只能凭空编，指称与挂图对不上。
    判据必须走能力函数 ``route_emits_asset_prompts``（与资产图节点同一真源）。
    """
    from minimax_h3_prompt.graph import nodes

    captured: dict = {}

    def fake_run_agent(agent, message):
        captured["request"] = message
        return "草稿"

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    node = nodes._make_reference_consistency_node({"reference_consistency": object()})
    state = {
        "brief": Brief(mode="director", director_mode="r2v"),
        "script": "林小满推开门",
        "character_image_prompts": [
            {"name": "林小满", "positive_prompt": "a young woman in a grey coat"}],
    }

    assert node(state) == {"subject_defs": "草稿"}
    request = captured["request"]
    assert "<Picture 1>：林小满" in request
    assert "a young woman in a grey coat" in request, "描述没取生图提示词"
    assert "生图提示词" in request


def test_frame_routes_keep_their_own_refs_block_without_the_director_judge(monkeypatch):
    """H6 的另一半：base 的帧登记与导演台 i2v 的行为**一点不变**。

    base/FL2VA 的 refs 是首尾帧登记：仍由 ``_ref_assets_block`` 的第二支渲染，但
    **不许**套多参的「语义名指称」纪律（那会把两套指称体系搅在一起）。这个节点对
    base 本来就早退（``route_emits_asset_prompts`` 恒假），所以直接测那个渲染口；
    导演台 i2v 走节点，必须与改动前一样给出空定义。
    """
    from minimax_h3_prompt.graph import nodes

    base_brief = Brief(mode="base", variant="FL2VA", refs=[
        RefItem(picture=1, name="首帧", description="", path="first.png")])
    block = nodes._ref_assets_block({"brief": base_brief}, base_brief)
    assert "<Picture 1>" in block
    assert "编号按剧本出场顺序" not in block, "帧路线被套上了多参的语义名指称纪律"

    node = nodes._make_reference_consistency_node({"reference_consistency": object()})
    assert node({"brief": Brief(mode="director", director_mode="i2v")}) == {"subject_defs": ""}


def test_director_r2v_registers_asset_prompts_for_the_subject_definitions():
    """r2v 的 `<Picture N>` 登记从资产图提示词现算（`ref_asset_registry` 同一判据）。

    不接这一处：reference_consistency 节点拿到的参考资产是「（无）」，六节外壳的
    subject_definitions 只能凭空编——写段能用，但指称与挂图对不上；director t2v
    反过来不许登记（它根本不产资产图）。
    """
    from minimax_h3_prompt.generation import ref_asset_registry

    state = {"script": "林小满推开门",
             "character_image_prompts": [{"name": "林小满", "positive_prompt": "a young woman",
                                          "negative_prompt": ""}]}
    refs = ref_asset_registry(state, Brief(mode="director", director_mode="r2v"))
    assert [(item.picture, item.name) for item in refs] == [(1, "林小满")]
    assert ref_asset_registry(state, Brief(mode="director", director_mode="t2v")) == []


# ---------------------------------------------------------------------------
# 段表 fixture：帧为真源（ADR 0025 的 107 + 4×153 = 719）
# ---------------------------------------------------------------------------

def _segment(index, segment_type, start, end, declared, export, guided):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=end,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=(),
    )


def _plan(mode: str = "r2v"):
    from minimax_h3_prompt.director.frames import DirectorPlan

    segments = (
        _segment(1, "opening", 0, 107, 107, 107, False),
        _segment(2, "continue", 107, 260, 141, 153, True),
        _segment(3, "continue", 260, 413, 141, 153, True),
        _segment(4, "continue", 413, 566, 141, 153, True),
        _segment(5, "ending", 566, 719, 141, 153, True),
    )
    return DirectorPlan(mode=mode, total_frames=719, segments=segments)


_R2V_BODY = """subject_definitions:
<Picture 1> is a woman in a grey trench coat holding a black umbrella.

summary:
[reference generation] A woman waits outside a convenience store in the rain.

retention_analysis:
<Picture 1>: fully_preserved.

detailed_description:
{note}
[Shot 1] <Picture 1> steps under the shop awning out of the rain.
[Shot 2] At {cut}, the camera pushes in until her face fills the frame.

overall_soundscape:
Rain drums on the metal awning; a shop chime rings once.

non_diegetic_music:
N/A
"""

# #130：t2v/i2v/fl2v 的正文是官方三字段外壳（字段名逐字、顺序固定、节间空行）。
# 旧形态（单段自由文本 + 行内 Ambience）已不再合法——形状闸从「出现段头即报错」
# 翻成「缺节／乱序报错」，两类用例必须一起改，否则闸与写法互相判死。
_SINGLE_BODY = """{note}
integrated_multimodal_description:
[Shot 1] A woman in a grey trench coat steps under the shop awning.
[Shot 2] At {cut}, the camera pushes in until her face fills the frame.

overall_soundscape:
Rain drums on the metal awning; footsteps on wet concrete; a faint shop chime.

non_diegetic_music:
N/A
"""


def _r2v_reply(segment, plan):
    """模型**该写的**形态：注记可有可无、时间戳相对本段起点（组装器负责换算）。"""
    from minimax_h3_prompt.director.prompts import render_timeline_note

    return _R2V_BODY.format(note=render_timeline_note(plan, segment), cut="00:02.500")


def _r2v_text(segment, plan):
    """组装后的**期望**形态：注记在、时间戳已是成片坐标。"""
    from minimax_h3_prompt.director.prompts import render_timeline_note

    note = render_timeline_note(plan, segment)
    return _R2V_BODY.format(note=note, cut=_absolute_cut(segment.start_frame, 2.5))


def _single_reply(segment, plan):
    from minimax_h3_prompt.director.prompts import render_timeline_note

    return _SINGLE_BODY.format(note=render_timeline_note(plan, segment), cut="00:02.500")


def _single_text(segment, plan):
    from minimax_h3_prompt.director.prompts import render_timeline_note

    note = render_timeline_note(plan, segment)
    return _SINGLE_BODY.format(note=note, cut=_absolute_cut(segment.start_frame, 2.5))


def _absolute_cut(start_frame: int, offset_s: float) -> str:
    """测试侧独立算绝对时间戳（不复用被测函数）：start_frame/24 + 本段内相对秒。"""
    total = start_frame / 24 + offset_s
    minutes = int(total // 60)
    seconds = int(total % 60)
    millis = int(round((total - int(total)) * 1000))
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


# ---------------------------------------------------------------------------
# 绝对时间轴：秒一律从帧现算（成片坐标），不沿用「相对本段起点」的旧口径
# ---------------------------------------------------------------------------

def test_timeline_note_is_recomputed_from_frames_in_absolute_coordinates():
    """段 2 的注记 = 107/24 → 4.458s 到 260/24 → 10.833s，全片 719/24 → 29.958s。

    期望值是测试侧用帧现算的**字面量**（独立来源），不是拿被测函数回读一遍。
    """
    from minimax_h3_prompt.director.prompts import render_timeline_note

    plan = _plan()
    assert render_timeline_note(plan, plan.segments[1]) == (
        "Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video.")
    assert render_timeline_note(plan, plan.segments[0]) == (
        "Segment 1 of 5, covering 0.000s-4.458s of the full 29.958s video.")


def test_timestamps_are_rebased_to_film_coordinates_from_segment_start_frame():
    """`At` 时间戳 = start_frame/24 + 本段内相对秒（票面公式）；段 1 恒等。"""
    from minimax_h3_prompt.director.prompts import absolutize_timestamps

    assert absolutize_timestamps("[Shot 2] At 00:00.500, the camera moves.",
                                 0) == "[Shot 2] At 00:00.500, the camera moves."
    assert absolutize_timestamps("[Shot 2] At 00:00.500, the camera moves.",
                                 107) == "[Shot 2] At 00:04.958, the camera moves."
    assert absolutize_timestamps("[Shot 2] At 00:01.000, x.", 24) == "[Shot 2] At 00:02.000, x."


# ---------------------------------------------------------------------------
# 确定性校验：方言契约（r2v 六节、其余单段）＋ 时间轴与段表一致
# ---------------------------------------------------------------------------

def test_validate_r2v_requires_the_six_section_shell():
    """r2v 保留六节外壳（唯一含音频段的模式）；缺段/乱序都是 error。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan()
    segment = plan.segments[1]
    assert validate_director_segment(_r2v_text(segment, plan), "r2v",
                                     plan=plan, segment=segment) == []

    missing = _r2v_text(segment, plan).replace("non_diegetic_music:", "music_notes:")
    codes = {i.code for i in validate_director_segment(missing, "r2v", plan=plan, segment=segment)}
    assert "DIRECTOR_SECTION_MISSING" in codes

    no_picture = _r2v_text(segment, plan).replace("<Picture 1>", "the woman")
    codes = {i.code for i in validate_director_segment(no_picture, "r2v",
                                                       plan=plan, segment=segment)}
    assert "DIRECTOR_PICTURE_MISSING" in codes

    ordered = _r2v_text(segment, plan)
    swapped = ordered.replace("overall_soundscape:\n", "__TMP__").replace(
        "non_diegetic_music:\n", "overall_soundscape:\n").replace(
        "__TMP__", "non_diegetic_music:\n")
    codes = {i.code for i in validate_director_segment(swapped, "r2v", plan=plan, segment=segment)}
    assert "DIRECTOR_SECTION_ORDER" in codes


def test_validate_r2v_rejects_state_annotations_and_base_align_lines():
    """导演台不写起止状态句、不写 base 的图片对齐指令行（设计 §3.3）。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment
    from minimax_h3_prompt.tools.h3_validator import ALIGN_TEMPLATES

    plan = _plan()
    segment = plan.segments[1]
    text = _r2v_text(segment, plan)
    with_state = text.replace(
        "[Shot 1]", "This segment begins with <Picture 1> under the awning.\n[Shot 1]")
    codes = {i.code for i in validate_director_segment(with_state, "r2v",
                                                       plan=plan, segment=segment)}
    assert "DIRECTOR_STATE_ANNOTATION" in codes

    with_align = text.replace("[Shot 1]", ALIGN_TEMPLATES["I2VA"] + "\n[Shot 1]")
    codes = {i.code for i in validate_director_segment(with_align, "r2v",
                                                       plan=plan, segment=segment)}
    assert "DIRECTOR_ALIGN_LINE" in codes


def test_validate_single_body_modes_require_the_three_field_shell():
    """t2v/i2v/fl2v 的正文是官方三字段（#130）：**缺节／乱序**报 error。

    这条闸的判据 2026-11-01 翻了正向：从前「出现段头即报错」，现在段头是**必需**的
    格式（官方 base 的三字段外壳：画面描述 → 环境声 → 配乐）。判据复用 ``BASE_SECTIONS``
    与节查找，不整体调 ``validate_base``——那会顺带查切点、说话人、edge-stability，
    全是导演台不用的东西，整调会造一批假 error（#122 Q2）。
    """
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan("t2v")
    segment = plan.segments[1]
    text = _single_text(segment, plan)
    for mode in ("t2v", "i2v", "fl2v"):
        assert validate_director_segment(text, mode, plan=plan, segment=segment) == []

        missing = text.replace("non_diegetic_music:\n", "")
        codes = {i.code for i in validate_director_segment(missing, mode,
                                                           plan=plan, segment=segment)}
        assert "DIRECTOR_SECTION_MISSING" in codes, mode

        swapped = (text.replace("overall_soundscape:\n", "__TMP__")
                   .replace("non_diegetic_music:\n", "overall_soundscape:\n")
                   .replace("__TMP__", "non_diegetic_music:\n"))
        codes = {i.code for i in validate_director_segment(swapped, mode,
                                                           plan=plan, segment=segment)}
        assert "DIRECTOR_SECTION_ORDER" in codes, mode


def test_validate_does_not_duplicate_the_whole_prompt_validator():
    """只判节齐全与顺序：``validate_base`` 的切点／说话人／edge-stability 不进导演台。

    三字段外壳里 ``[Shot 1]`` 不写时间戳（导演台的时间基是注记给的），``validate_base``
    的切点判据要求后续镜头带时刻——整调会在**格式完全正确**的段上报假错。
    """
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan("t2v")
    segment = plan.segments[1]
    text = _single_text(segment, plan).replace("At 00:06.958, ", "")
    issues = validate_director_segment(text, "t2v", plan=plan, segment=segment)
    assert [i.code for i in issues] == [], f"形状正确的段被别的判据判红了：{issues}"


def test_validate_checks_the_timeline_note_against_the_frame_grid():
    """注记缺失 → error；时间戳落在本段帧窗口之外 → error（相对口径漏改的信号）。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan()
    segment = plan.segments[1]
    text = _r2v_text(segment, plan)

    no_note = text.replace("Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video.", "")
    codes = {i.code for i in validate_director_segment(no_note, "r2v", plan=plan, segment=segment)}
    assert "DIRECTOR_TIMELINE_MISSING" in codes

    relative = text.replace("At 00:06.958", "At 00:02.500")
    codes = {i.code for i in validate_director_segment(relative, "r2v",
                                                       plan=plan, segment=segment)}
    assert "DIRECTOR_TIMESTAMP_OUT_OF_RANGE" in codes


def test_cancelled_modes_are_refused_loudly():
    """v2v/rv2v/mixed 已取消（#170）：必须报「模式无效」且只列四种可用值。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan()
    segment = plan.segments[1]
    for mode in ("v2v", "rv2v", "mixed"):
        with pytest.raises(ValueError, match="模式无效.*t2v/i2v/fl2v/r2v"):
            validate_director_segment("x", mode, plan=plan, segment=segment)


def test_note_and_section_guards_follow_their_source_constants(monkeypatch):
    """变异检验（票面硬条）：改掉帧换算的来源、或改掉节列表，守卫必须变红。

    守卫从常量**派生**（不是把形态重抄一份）：``FPS`` 一改，按 24fps 写出的注记就对不上
    段表；``REF_SECTIONS`` 一改，同一段正文就缺段。永不为红的守卫等于没有。
    """
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    segment = plan.segments[1]
    text = _r2v_text(segment, plan)

    monkeypatch.setattr(prompts, "FPS", 25)
    codes = {i.code for i in prompts.validate_director_segment(
        text, "r2v", plan=plan, segment=segment)}
    assert "DIRECTOR_TIMELINE_MISSING" in codes, "帧换算改了口径，注记守卫没响"

    monkeypatch.undo()
    monkeypatch.setattr(prompts, "REF_SECTIONS",
                        [*prompts.REF_SECTIONS[:-1], "audio_room_tone"])
    codes = {i.code for i in prompts.validate_director_segment(
        text, "r2v", plan=plan, segment=segment)}
    assert "DIRECTOR_SECTION_MISSING" in codes, "节列表改了口径，六节守卫没响"

    # 三字段闸同样从常量派生（#130）：往 BASE_SECTIONS 里加一节，同一段正文立刻缺节。
    monkeypatch.undo()
    single = _plan("t2v")
    monkeypatch.setattr(prompts, "BASE_SECTIONS",
                        [*prompts.BASE_SECTIONS, "diegetic_music_cue"])
    codes = {i.code for i in prompts.validate_director_segment(
        _single_text(single.segments[1], single), "t2v",
        plan=single, segment=single.segments[1])}
    assert "DIRECTOR_SECTION_MISSING" in codes, "三字段口径改了，形状闸没响"


# ---------------------------------------------------------------------------
# #197 写段期：引导段的首镜必须接上上一段
# ---------------------------------------------------------------------------

def _first_shot(text: str, line: str) -> str:
    """把段正文的 ``[Shot 1]`` 那一镜换成给定句子，其余字节不动。"""
    return re.sub(r"\[Shot 1\][^\n]*", f"[Shot 1] {line}", text, count=1)


def test_guided_segment_first_shot_with_no_overlap_is_flagged():
    """引导段首镜与上一段末尾**毫无交集** → 判红，用**自己的**错误码。

    参照物是上一段**实际写出的段词**，不是 plan 里的 ``end_hook``：``end_hook`` 是
    中文规划、段词是英文，跨语言取二元组必然恒零重叠（A 臂第 9 段实测），拿它当
    参照物会把已知合格的段判红。承诺判据仍是启发式——零重叠只证明「完全没交集」，
    不证明「承接得好」。
    """
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan("t2v")
    segment = plan.segments[1]  # continue：引导段
    text = _first_shot(_single_text(segment, plan), "Man punches wall.")

    codes = {i.code for i in validate_director_segment(
        text, "t2v", plan=plan, segment=segment,
        previous_text="[Shot 2] Fog drifts.")}
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" in codes, \
        "首镜与上一段毫无交集，守卫没响"


def test_guided_segment_sharing_any_bigram_passes():
    """只要有一个共享二元组就放行——门槛刻意做低，避免把正常承接判死。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan("t2v")
    segment = plan.segments[1]
    text = _first_shot(_single_text(segment, plan), "The fog thickens.")

    codes = {i.code for i in validate_director_segment(
        text, "t2v", plan=plan, segment=segment,
        previous_text="[Shot 2] Fog drifts.")}
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" not in codes


def test_camera_cut_segment_is_never_judged():
    """硬切段本来就不承接：对它判「首镜要接上」是假错。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _fl2v_plan("continue", "camera_cut")
    segment = plan.segments[1]
    text = _first_shot(_single_text(segment, plan), "Man punches wall.")

    codes = {i.code for i in validate_director_segment(
        text, "fl2v", plan=plan, segment=segment,
        previous_text="[Shot 2] Fog drifts.")}
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" not in codes, \
        "硬切段被当成承接段判红了"


def test_first_segment_and_missing_previous_text_are_never_judged():
    """第 1 段没有上一段；上一段文本为空、或本段取不到 ``[Shot 1]`` 时都放过。

    宁可放过不可错杀：这条判据只是「完全没交集」的信号，不是语义检查。
    """
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan("t2v")
    opening = plan.segments[0]  # opening：非引导段，也没有上一段
    first_text = _first_shot(_single_text(opening, plan), "Man punches wall.")
    codes = {i.code for i in validate_director_segment(
        first_text, "t2v", plan=plan, segment=opening,
        previous_text="[Shot 2] Fog drifts.")}
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" not in codes, "第 1 段被误判"

    segment = plan.segments[1]
    text = _first_shot(_single_text(segment, plan), "Man punches wall.")
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" not in {
        i.code for i in validate_director_segment(
            text, "t2v", plan=plan, segment=segment, previous_text="")}
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" not in {
        i.code for i in validate_director_segment(
            text, "t2v", plan=plan, segment=segment, previous_text="   \n")}

    # 取不到 [Shot 1] 时同样不判（信号不够，不臆造）
    no_shot_one = text.replace("[Shot 1]", "[Opening beat]")
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" not in {
        i.code for i in validate_director_segment(
            no_shot_one, "t2v", plan=plan, segment=segment,
            previous_text="[Shot 2] Fog drifts.")}


def test_disconnected_first_shot_goes_through_the_rewrite_loop(tmp_path):
    """批量写段时，首镜完全另起的第一版被退回重写；重写请求点名新错误码。

    断言的是**模型收到的出站请求**（第 2 段的第二次请求）与最终落盘文本——不测私有
    调用顺序。上一段的段词由假模型自己写出，守卫拿到的就是 ``written[-1].text``。
    """
    from minimax_h3_prompt.director.prompts import write_director_segments

    plan = _plan("t2v")
    fresh_start = _first_shot(_single_reply(plan.segments[1], plan),
                              "Man punches wall.")
    retry_marker = "上一次产出被校验器判为不合格"

    def behaviour(call, request):
        index = int(re.search(r"Segment (\d+)/", request).group(1))
        segment = plan.segments[index - 1]
        reply = _single_reply(segment, plan)
        if index == 1:
            # 上一段的最后一镜故意与 "Man punches wall." 零重叠
            return reply.replace(
                "[Shot 2] At 00:02.500, the camera pushes in until her face fills the frame.",
                "[Shot 2] Fog drifts.")
        if index == 2 and retry_marker not in request:
            return fresh_start
        return reply

    llm = _DialectLLM(None, behaviour=behaviour)
    written = write_director_segments(plan, llm=llm, session_dir=tmp_path)

    assert len(llm.requests) == 6, "第 2 段应恰好被退回重写一次（5 段 + 1 次重写）"
    assert "DIRECTOR_FIRST_SHOT_DISCONNECTED" in llm.requests[2], \
        "重写请求没点名「首镜没接上」"
    assert written[1].text.strip() == _single_text(plan.segments[1], plan).strip()


# ---------------------------------------------------------------------------
# 组装：定义块由代码给定、注记由代码注入、时间戳由代码重算
# ---------------------------------------------------------------------------

_SUBJECT_DEFS = """subject_definitions:
<Picture 1> is a woman in a grey trench coat holding a black umbrella.

retention_analysis:
<Picture 1>: fully_preserved."""


def test_normalize_r2v_replaces_model_written_definitions_and_note():
    """定义块与注记是**代码性质**、不靠模型服从（同 ``assemble_ref_segment`` 的理由）。"""
    from minimax_h3_prompt.director.prompts import (
        normalize_segment_output,
        split_ref_sections,
    )

    plan = _plan()
    segment = plan.segments[1]
    model_output = _R2V_BODY.format(
        note="Segment 2 of 5, covering 4s-11s of the full 30s video.",  # 模型抄错的注记
        cut="00:02.500",  # 相对本段起点（组装器要加成 6.958）
    ).replace("<Picture 1> is a woman in a grey trench coat holding a black umbrella.",
              "<Picture 9> is a tall stranger with a red scarf.")

    text = normalize_segment_output(
        model_output, plan=plan, segment=segment,
        sections=split_ref_sections(_SUBJECT_DEFS))

    detailed = split_ref_sections(text)["detailed_description"]
    assert detailed.startswith(
        "Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video."), \
        "注记必须是组装器按帧注入的那一句，不是模型抄的那一句"
    assert "At 00:06.958" in detailed and "At 00:02.500" not in detailed
    # 定义块取请求给定那份，模型改写不采信
    assert "<Picture 1> is a woman" in text and "<Picture 9>" not in text


def test_normalize_single_body_prepends_absolute_note_and_rebases():
    """t2v/i2v/fl2v 单段正文：注记置顶、时间戳重算、不补任何段头。"""
    from minimax_h3_prompt.director.prompts import normalize_segment_output

    plan = _plan("i2v")
    segment = plan.segments[1]
    text = normalize_segment_output(
        _SINGLE_BODY.format(note="", cut="00:01.000"), plan=plan, segment=segment)
    assert text.splitlines()[0] == (
        "Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video.")
    assert "At 00:05.458" in text, "1.000s 相对本段 + 107/24 应为 5.458"
    assert "subject_definitions:" not in text, "单段正文不许出现段头"


# ---------------------------------------------------------------------------
# 写段请求的两块新前提（#130）：本段输出外壳、（本段给图）
# ---------------------------------------------------------------------------

def _give_image_segment(mode: str):
    """带真实给图清单的段：``_plan`` 的 fixture 把 ``give_images`` 置空，是别处的契约。"""
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment

    segment = DirectorSegment(
        index=1, type="opening", guided=False, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, summary="段 1 剧情",
        end_hook="段 1 落定画面", give_images=("first",))
    return DirectorPlan(mode=mode, total_frames=107, segments=(segment,)), segment


def test_segment_request_carries_the_output_shell_and_the_give_images_line():
    """出站请求必须带两块（#130）：本段输出外壳、（本段给图）。

    为什么（b）是**前提不是修辞**：L2-06／L2-07／L3-02 全依赖「本段有没有首尾帧图」，
    而改动前请求里一个字都没有，模型只能猜。
    """
    from minimax_h3_prompt.director.planning import render_give_images
    from minimax_h3_prompt.director.prompts import build_segment_request
    from minimax_h3_prompt.tools.h3_validator import BASE_SECTIONS

    plan, segment = _give_image_segment("fl2v")
    request = build_segment_request(plan, segment)

    assert "【本段输出外壳】" in request
    # 字段名逐字、顺序固定：三条都在，且按官方顺序出现
    offsets = [request.index(f"{header}:") for header in BASE_SECTIONS]
    assert offsets == sorted(offsets), f"三字段在请求里的顺序不是 {BASE_SECTIONS}"
    assert f"【本段给图】{render_give_images(segment, plan.mode)}" in request


def test_give_images_line_is_rendered_by_the_segment_table_renderer(monkeypatch):
    """给图那行取自 ``render_give_images``（与段表清单同一份），不另写一份 token 翻译。

    变异检验：把那个渲染函数换成哨兵，请求里必须跟着变——否则这里只是把同一段
    字面量抄了两遍，「同一份」是嘴上说的。
    """
    from minimax_h3_prompt.director import prompts

    plan, segment = _give_image_segment("fl2v")
    monkeypatch.setattr(prompts, "render_give_images",
                        lambda item, mode, **kwargs: f"哨兵给图:{item.index}/{mode}")
    assert "【本段给图】哨兵给图:1/fl2v" in prompts.build_segment_request(plan, segment)


def test_r2v_request_does_not_get_the_three_field_shell():
    """r2v 维持官方 ref 六节（#122 Q1）：它自成一格，不套三字段外壳。"""
    from minimax_h3_prompt.director.prompts import build_segment_request, split_ref_sections

    plan = _plan()
    request = build_segment_request(plan, plan.segments[1],
                                    sections=split_ref_sections(_SUBJECT_DEFS))
    assert "【本段输出外壳】" not in request
    assert "detailed_description" in request


def test_r2v_instruction_uses_subjects_for_reusable_content():
    """r2v 正文指称：可复用内容用 ``<Subject N>``，只有具体帧／独立图才用 ``<Picture N>``。

    以前正文「以 ``<Picture N>`` 为准」，同一个角色的多张母版在正文里被拆成三个主体；
    定义块却按官方合并成一个 Subject（#134/#155），指称体系于是两套。
    """
    from minimax_h3_prompt.director.prompts import load_instruction

    r2v = load_instruction("r2v")
    assert "<Subject N>" in r2v, "r2v 指令没给 Subject 指称"
    assert "可复用" in r2v, "没说哪些内容用 Subject"
    assert "具体帧" in r2v or "独立图" in r2v, "没说 Picture 只留给具体帧/独立图"
    assert "指称以 `<Picture N>` 为准" not in r2v, "旧的 Picture 优先措辞还在"


def test_mode_instructions_teach_the_official_three_field_shell():
    """三条单段模式的角色提示词里写着三字段外壳（字段名逐字、顺序固定、无「音频并入正文」）。"""
    from minimax_h3_prompt.director.prompts import load_instruction
    from minimax_h3_prompt.tools.h3_validator import BASE_SECTIONS

    for mode in ("t2v", "i2v", "fl2v"):
        text = load_instruction(mode)
        offsets = [text.index(f"{header}:") for header in BASE_SECTIONS]
        assert offsets == sorted(offsets), f"{mode} 的三字段顺序不对"
        assert "音频描述并入正文" not in text, f"{mode} 还留着「音频并入正文」的旧口径"
        assert "英文" in text, f"{mode} 没写正文语言（三字段全英文）"
    # r2v 六节不动，但要追加「参考图不得当首帧／落点」
    r2v = load_instruction("r2v")
    assert "不得" in r2v and "Picture" in r2v


def test_i2v_and_fl2v_instructions_carry_the_official_path_structure():
    """i2v／fl2v 的路径结构落**角色提示词**（模式方言不进共享真源，#122 Q3）。"""
    from minimax_h3_prompt.director.prompts import load_instruction

    i2v = load_instruction("i2v")
    for step in ("首帧锚点", "动作起始", "连续发展", "结果或反应"):
        assert step in i2v, f"i2v 缺官方 I2VA 路径的一段：{step}"

    fl2v = load_instruction("fl2v")
    for step in ("首帧状态", "可观察", "收窄", "尾帧状态"):
        assert step in fl2v, f"fl2v 缺官方 FL2VA 路径的一段：{step}"
    assert "单镜" in fl2v, "fl2v 没写「优先单镜」"


def test_fl2v_endpoint_composition_sentence_is_conditional():
    """终点构图句**条件触发**：有尾帧才要求，没尾帧落到 end_hook，不许凭空点名一张图。

    两条必须写在一起：只写「有尾帧要写终点构图」会逼模型在没有尾帧的段上也点名一张
    不存在的图（AC：「无尾帧的段不被要求」）。
    """
    from minimax_h3_prompt.director.prompts import load_instruction

    fl2v = load_instruction("fl2v")
    blocks = [block for block in re.split(r"\n\s*\n", fl2v)
              if "composition established by" in block]
    assert blocks, "终点构图句不在角色提示词里"
    # 条件必须与句式**同一段**：分开写等于给了模型半条规则
    assert any("尾帧" in block
               and any(word in block for word in ("没给", "没有", "未给"))
               for block in blocks), "终点构图句没有写条件触发（没有尾帧时不许点名）"
    assert "end_hook" in fl2v, "没有尾帧时落哪一句没写"


# ---------------------------------------------------------------------------
# #147 fl2v：本段起点事实块与四条给图分支
# ---------------------------------------------------------------------------

def _fl2v_plan(*types):
    """按给定类型序列造一张 fl2v 段表；帧数只为可读，写段请求不按总帧判事。"""
    from dataclasses import replace

    from minimax_h3_prompt.director.frames import DirectorPlan, give_images_for

    segments = []
    for index, segment_type in enumerate(types, 1):
        segment = _segment(index, segment_type, (index - 1) * 107, index * 107,
                           107, 107, segment_type not in ("opening", "camera_cut"))
        segments.append(replace(segment,
                                give_images=give_images_for(segment_type, "fl2v")))
    return DirectorPlan(mode="fl2v", total_frames=107 * len(types),
                        segments=tuple(segments))


def _give_line(request: str) -> str:
    return next(line for line in request.splitlines()
                if line.startswith("【本段给图】"))


def test_fl2v_request_states_the_segment_start_by_type():
    """引导段带上一段落定画面；切镜段带「本段首帧／硬切／不承接」；开场带全片开场。

    为什么起点要进请求：fl2v 的段类型只决定给哪几张图，而「第一拍从哪接」既不是图、
    也不是 end_hook（那是终点）——不写进请求，模型只能按类型名猜，`continue` 会被写成
    从一张不存在的首帧图开始、`camera_cut` 会被写成本段首帧与上一段同景。
    """
    from minimax_h3_prompt.director.prompts import build_segment_request

    plan = _fl2v_plan("opening", "continue", "camera_same", "camera_cut", "ending")

    opening = build_segment_request(plan, plan.segments[0])
    assert "【本段起点】" in opening and "全片开场" in opening

    for position in (1, 2, 4):  # continue / camera_same / ending 都是引导段
        segment = plan.segments[position]
        request = build_segment_request(plan, segment)
        previous = plan.segments[position - 1]
        assert "【本段起点】" in request, f"{segment.label} 没有起点事实块"
        assert previous.end_hook in request, f"{segment.label} 没带上一段落定画面"

    cut = build_segment_request(plan, plan.segments[3])
    assert "本段首帧" in cut, "切镜段没写「本段第一帧来自本段首帧图」"
    assert "硬切" in cut, "切镜段没写硬切"
    assert "不承接" in cut, "切镜段没写不承接上一段"


def test_fl2v_opening_tail_image_only_for_single_segment_with_film_end():
    """单段开场且有全片尾帧 → 首+尾；单段无尾帧 / 多段开场 → 仅首。

    全片尾帧在多段片子里属于**最后一段**，挂到开场等于让第 1 段在几秒里跑完整片
    （pack._plan_media 的同一条裁定）。所以给图行必须按段数收口，不能一律写「可选尾帧」。
    """
    from minimax_h3_prompt.director.prompts import build_segment_request

    single = _fl2v_plan("opening")
    with_end = _give_line(build_segment_request(single, single.segments[0], film_end=True))
    assert "首帧" in with_end and "尾帧" in with_end, with_end

    without_end = _give_line(build_segment_request(single, single.segments[0]))
    assert "尾帧" not in without_end, without_end

    multi = _fl2v_plan("opening", "continue")
    multi_line = _give_line(build_segment_request(multi, multi.segments[0], film_end=True))
    assert "尾帧" not in multi_line, multi_line


def test_i2v_and_fl2v_teach_the_soundscape_rule_in_instruction_and_request():
    """L2-02（#144 A）：``overall_soundscape`` 不重复对白、唱歌与角色能听到的音乐。

    两处都要写：角色提示词（纪律）和请求的【本段输出外壳】（离产出最近的格式说明）。
    只写一处时模型挑一处相信——把对白抄进 soundscape 的那一版仍然「看起来合规」。
    """
    from minimax_h3_prompt.director.prompts import (
        build_segment_request,
        load_instruction,
        split_ref_sections,
    )

    rule = "不重复对白、唱歌与角色能听到的音乐"
    for mode in ("i2v", "fl2v"):
        assert rule in load_instruction(mode), f"{mode} 的写段指令缺 L2-02 声场规则"
        plan, segment = _give_image_segment(mode)
        assert rule in build_segment_request(plan, segment), \
            f"{mode} 的请求【本段输出外壳】缺 L2-02 声场规则"

    plan = _plan()
    r2v = build_segment_request(plan, plan.segments[1],
                                sections=split_ref_sections(_SUBJECT_DEFS))
    assert rule not in r2v, "r2v 六节外壳带上了三字段的声场规则"


def test_common_guidance_wording_splits_guided_and_cut_segments():
    """common 的段间衔接按段型分叉（#144 A）：引导段靠潜空间引导，切镜段无引导。

    以前 common 无条件写「段间衔接靠潜空间引导」，``camera_cut`` 的包内
    ``continuityFromPrev=false`` 与这句相反——模型照抄就会写成续接。用 t2v 断言，
    因为 t2v 小节本身不提段型：这里出现的「切镜段」只可能来自 common。
    """
    from minimax_h3_prompt.director.prompts import load_instruction

    for mode in ("t2v", "fl2v"):
        text = load_instruction(mode)
        assert "引导段" in text and "潜空间引导" in text, f"{mode} 缺引导段分支"
        assert "切镜段" in text and "无引导" in text, f"{mode} 缺切镜段无引导的分叉"
        assert "不承接上一段" in text, f"{mode} 没写切镜段不承接"


def test_music_rule_continues_one_cue_across_segments():
    """#194：跨段配乐写「同一支 cue 延续」，不按本段各写各的。

    旧的「无声写 `N/A`」让实测 10 段里 7 段配乐写 N/A，接缝处出现 −9.8 / +26.6 dB
    电平台阶。引导段的音画已被段间引导钉在上一段上，文字侧必须延续同一支 cue；
    硬切段两侧也写同一支 cue，只许改力度与织体。

    分叉只认请求里已有的段类型标签（``段类型：{segment.label}``），不新增事实块；
    所以断言的是**模型实际收到的切片文本**（``load_instruction``），而不是文件字节。
    """
    from minimax_h3_prompt.director.frames import GUIDED_SEGMENT_TYPES, segment_type_label
    from minimax_h3_prompt.director.prompts import build_segment_request, load_instruction

    old_phrase = "无声写 `N/A`"
    for mode in ("r2v", "t2v", "i2v", "fl2v"):
        text = load_instruction(mode)
        assert old_phrase not in text, f"{mode} 还留着「无声写 N/A」的旧配乐纪律"
        # 引导段分支：点名请求里会出现的段类型标签，并给出「延续同一支 cue」的可执行要点
        for segment_type in GUIDED_SEGMENT_TYPES:
            label = segment_type_label(segment_type)
            assert label in text, f"{mode} 的配乐纪律没点名引导段类型「{label}」"
        assert segment_type_label("camera_cut") in text, f"{mode} 的配乐纪律没点名切镜段"
        for point in ("同一支 cue", "配器", "力度", "乐器", "速度", "织体"):
            assert point in text, f"{mode} 的配乐纪律缺可执行要点 {point!r}"

    # 分叉判据能在请求里直接对上：每一种段类型都带着同名中文标签出站；
    # 纪律本身也要随指令进请求（「规则在、接线不在」是本仓反复踩的坑）
    plan = _fl2v_plan("opening", "continue", "camera_same", "camera_cut", "ending")
    for segment in plan.segments:
        request = build_segment_request(plan, segment)
        assert f"段类型：{segment_type_label(segment.type)}" in request, \
            f"{segment.type} 的请求没有段类型标签，配乐分叉无从执行"
        assert "同一支 cue" in request, f"{segment.type} 的写段请求缺配乐延续纪律"


def test_common_gate_and_prop_ownership_rules_reach_the_writer_request(tmp_path):
    """#159：门槛里外与「一件物品一处归属」两条纪律随 common 进入真实写段请求。

    断言的是**模型收到的请求**（记录型桩模型抓到的出站字符串），不是
    ``load_instruction`` 的返回值——本仓踩过「规则在、接线不在」：加载口返回了，
    模型那侧却没有。两条纪律住在 common，所以四条模式都要能看见；到达性用 fl2v
    实测（#137 验收里出问题的正是 fl2v 段词）。
    """
    from minimax_h3_prompt.director.prompts import (
        load_instruction,
        write_director_segments,
    )

    for mode in ("t2v", "i2v", "fl2v", "r2v"):
        instruction = load_instruction(mode)
        assert "门内" in instruction and "门外" in instruction, \
            f"{mode} 的 common 缺「门内门外逐人写清」纪律"
        assert "只写一处归属" in instruction, f"{mode} 的 common 缺「一件物品一处归属」纪律"

    plan = _plan("fl2v")
    llm = _DialectLLM(lambda index: _single_reply(plan.segments[index - 1], plan))
    write_director_segments(plan, llm=llm, session_dir=tmp_path)

    request = llm.requests[0]
    assert "门内" in request and "门外" in request, "门槛里外纪律没到模型眼前"
    assert "移动方向" in request, "门槛纪律没写清「要写移动方向」"
    assert "already inside the house" in request, "门槛纪律的英文正例没到模型眼前"
    assert "只写一处归属" in request, "物品归属纪律没到模型眼前"
    assert "no second helmet" in request, "物品归属的英文正例没到模型眼前"


def test_fl2v_instruction_teaches_the_four_give_image_branches():
    """写段指令按「首+尾 / 仅首 / 仅尾 / 无图」四条分支写，不再一套首尾帧口径通吃。"""
    from minimax_h3_prompt.director.prompts import load_instruction

    fl2v = load_instruction("fl2v")
    for branch in ("首帧 + 尾帧", "仅首帧", "仅尾帧", "无图"):
        assert branch in fl2v, f"fl2v 写段指令缺分支：{branch}"
    assert "硬切" in fl2v and "不承接" in fl2v, "指令没把切镜段写成硬切/不承接"


# ---------------------------------------------------------------------------
# #192 r2v：本段起点事实块（同一份措辞，切镜段不指向不存在的首帧图）
# ---------------------------------------------------------------------------

def _r2v_plan(*types):
    """按给定类型序列造一张 r2v 段表：每段都挂参考素材（r2v 没有帧槽）。"""
    from dataclasses import replace

    from minimax_h3_prompt.director.frames import DirectorPlan, give_images_for

    segments = []
    for index, segment_type in enumerate(types, 1):
        segment = _segment(index, segment_type, (index - 1) * 107, index * 107,
                           107, 107, segment_type not in ("opening", "camera_cut"))
        segments.append(replace(segment,
                                give_images=give_images_for(segment_type, "r2v")))
    return DirectorPlan(mode="r2v", total_frames=107 * len(types),
                        segments=tuple(segments))


def _start_line(request: str) -> str:
    """请求里的【本段起点】块首行（块内事实都在这一行，引导段的 end_hook 也在）。"""
    line = next((row for row in request.splitlines()
                 if row.startswith("【本段起点】")), "")
    assert line, "请求里没有【本段起点】块"
    return line


def test_r2v_guided_and_opening_requests_state_the_segment_start(tmp_path):
    """r2v 引导段拿得到上一段 end_hook 原文；开场段写明没有上一段可承接。

    为什么起点要进 r2v 请求：六节外壳只教「引导段靠潜空间引导承接上一段落定画面」，
    那句话的内容从来不在请求里——模型只被告知「去承接」，却没被告知承接什么。
    """
    from minimax_h3_prompt.director.prompts import write_director_segments
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections

    plan = _plan("r2v")
    llm = _DialectLLM(lambda index: _r2v_reply(plan.segments[index - 1], plan))
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            sections=split_ref_sections(_SUBJECT_DEFS))

    opening = _start_line(llm.requests[0])
    assert "全片开场" in opening and "没有上一段可承接" in opening
    # 开场段不能出现任何别的段的 end_hook（它自己的 end_hook 在请求行里，允许）
    others = [segment.end_hook for segment in plan.segments[1:]]
    assert all(hook not in llm.requests[0] for hook in others), \
        "开场段的起点块带进了别的段的落定画面"

    # 第 2 段（continue）与末段（ending）都是引导段：块内含上一段 end_hook 原文
    for position in (1, len(plan.segments) - 1):
        previous = plan.segments[position - 1]
        assert "上一段落定画面" in _start_line(llm.requests[position])
        assert previous.end_hook in _start_line(llm.requests[position]), \
            f"{plan.segments[position].label} 没带上一段落定画面原文"


def test_r2v_camera_cut_does_not_point_at_a_first_frame_image(tmp_path):
    """r2v 没有帧槽：切镜段写成硬切新场景，不能出现「本段首帧图」引向不存在的图。"""
    from minimax_h3_prompt.director.prompts import write_director_segments
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections

    plan = _r2v_plan("opening", "camera_same", "camera_cut")
    llm = _DialectLLM(lambda index: _r2v_reply(plan.segments[index - 1], plan))
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            sections=split_ref_sections(_SUBJECT_DEFS))

    cut = _start_line(llm.requests[2])
    assert "硬切" in cut and "不承接" in cut, cut
    assert "本段首帧图" not in cut, \
        "r2v 没有帧槽，切镜段不能指向不存在的首帧图"

    # camera_same 也是引导段：块内含上一段 end_hook 原文
    assert plan.segments[0].end_hook in _start_line(llm.requests[1])


# ---------------------------------------------------------------------------
# 逐段写与落盘：GENxxx/director/segment-NN.md
# ---------------------------------------------------------------------------

class _DialectLLM:
    """按请求里的段号产出对应段正文的假模型（不联网、不发真实请求）。"""

    def __init__(self, builder, behaviour=None):
        self.builder = builder
        self.behaviour = behaviour
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        if self.behaviour is not None:
            return self.behaviour(len(self.requests), request)
        index = int(re.search(r"Segment (\d+)/", request).group(1))
        return self.builder(index)


@pytest.mark.parametrize("mode", ["t2v", "i2v", "fl2v"])
def test_write_director_segments_writes_one_file_per_segment(tmp_path, mode):
    """每种单段模式都落盘：段号 1 起的 ``director/segment-NN.md``，内容过校验。"""
    from minimax_h3_prompt.director.prompts import (
        segment_path,
        validate_director_segment,
        write_director_segments,
    )
    plan = _plan(mode)
    llm = _DialectLLM(lambda index: _single_reply(plan.segments[index - 1], plan))
    written = write_director_segments(plan, llm=llm, session_dir=tmp_path / "GEN001",
                                      shot_table="分镜表：[Shot 1] 雨夜便利店……")

    assert len(written) == 5
    for item in written:
        assert item.path == segment_path(tmp_path / "GEN001", item.index)
        assert item.path.is_file()
        assert item.path.read_text(encoding="utf-8").strip() == item.text
        assert item.issues == (), f"第 {item.index} 段有校验问题：{item.issues}"

    # 段文件里是**绝对**坐标（成片坐标），不是相对本段起点的旧口径
    second = (tmp_path / "GEN001" / "director" / "segment-02.md").read_text(encoding="utf-8")
    assert "At 00:06.958" in second
    assert validate_director_segment(second, mode, plan=plan, segment=plan.segments[1]) == []


def test_write_director_segments_sends_mode_specific_instruction(tmp_path):
    """指令按模式取节：r2v 的请求说六节、t2v 的请求说单段正文（词表只复用不新增键）。"""
    from minimax_h3_prompt.director.prompts import (
        load_instruction,
        write_director_segments,
    )
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections

    plan = _plan("r2v")
    llm = _DialectLLM(lambda index: _r2v_reply(plan.segments[index - 1], plan))
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            sections=split_ref_sections(_SUBJECT_DEFS))

    request = llm.requests[0]
    assert "<Picture 1> is a woman in a grey trench coat" in request, \
        "r2v 请求要把本段定义块下发给模型取材"
    assert "词表" in request, "落笔侧词表必须进写段请求"
    assert load_instruction("t2v") != load_instruction("r2v")
    assert "detailed_description" in load_instruction("r2v")
    assert "detailed_description" not in load_instruction("t2v")


def test_write_director_segment_retries_with_the_issue_list(monkeypatch):
    """不合格产出带着问题清单重写一次（与多参写段同形），仍不合格则交回原文。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan("t2v")
    segment = plan.segments[1]
    reply = _single_reply(segment, plan)
    good = _single_text(segment, plan)
    # 第一版漏掉第三节（#130 起缺节才是不合格；旧形态「写了段头」现在反而是对的）。
    bad = reply.replace("non_diegetic_music:\nN/A\n", "")

    llm = _DialectLLM(None, behaviour=lambda call, request: bad if call == 1 else reply)
    text = prompts.write_director_segment(plan, segment, llm=llm)

    assert text.strip() == good.strip()
    assert len(llm.requests) == 2
    assert "DIRECTOR_SECTION_MISSING" in llm.requests[1], "重写请求必须点名上一版的问题"


def test_written_issues_come_from_the_rewrite_loop(tmp_path, monkeypatch):
    """出站清单的问题必须是重写循环交回的那一份，不许在调用处复跑校验器（#185）。

    复跑恰恰算不出循环里合成的 ``DIRECTOR_PICTURE_OVERFLOW``（它只存在于循环内）。
    这里用一个「同一段文本第二次被校验就换一套结论」的假校验器把两条来源分开：
    ``WrittenSegment.issues`` 拿到 ``FROM_LOOP`` ＝ 用的是循环那份；拿到
    ``FROM_RERUN`` ＝ 调用处又跑了一遍。
    """
    from minimax_h3_prompt.director import prompts
    from minimax_h3_prompt.director.prompts import write_director_segments
    from minimax_h3_prompt.tools.h3_validator import ValidationIssue

    seen: dict[tuple[str, int], int] = {}

    def fake_validate(text, mode, *, plan, segment, previous_text=""):
        key = (text, segment.index)
        seen[key] = seen.get(key, 0) + 1
        code = "FROM_LOOP" if seen[key] == 1 else "FROM_RERUN"
        return [ValidationIssue("warning", code, "marker")]

    monkeypatch.setattr(prompts, "validate_director_segment", fake_validate)

    plan = _plan("t2v")
    llm = _DialectLLM(lambda index: _single_reply(plan.segments[index - 1], plan))
    written = write_director_segments(plan, llm=llm, session_dir=tmp_path)

    for item in written:
        assert {issue.code for issue in item.issues} == {"FROM_LOOP"}, \
            f"第 {item.index} 段的出站问题被复跑的校验器覆盖了"


def test_write_director_segments_fails_loudly_per_segment(tmp_path):
    """写段调用失败→点名段号抛错，不写半段假内容（ADR 0019：回退可以，静默不可以）。"""
    from minimax_h3_prompt.director.prompts import write_director_segments

    def boom(call, request):
        raise RuntimeError("网关 502")

    plan = _plan("t2v")
    with pytest.raises(RuntimeError, match="第 1 段"):
        write_director_segments(plan, llm=_DialectLLM(None, behaviour=boom),
                                session_dir=tmp_path)
    assert not (tmp_path / "director" / "segment-01.md").exists()


def test_cancelled_modes_refuse_to_write(tmp_path):
    """v2v/rv2v/mixed 已取消（#170）：不许静默套用别的模式写段。"""
    from minimax_h3_prompt.director.prompts import write_director_segments

    for mode in ("v2v", "rv2v", "mixed"):
        with pytest.raises(ValueError, match="模式无效.*t2v/i2v/fl2v/r2v"):
            write_director_segments(_plan(mode), llm=_DialectLLM(lambda i: ""),
                                    session_dir=tmp_path)


def test_r2v_requires_subject_definitions_before_writing(tmp_path):
    """r2v 没有定义块就没有六节外壳的 1、3 节：当场报错，不写半份空定义。"""
    from minimax_h3_prompt.director.prompts import write_director_segments

    with pytest.raises(ValueError, match="subject_defs|定义"):
        write_director_segments(_plan("r2v"), llm=_DialectLLM(lambda i: ""),
                                session_dir=tmp_path)


# ---------------------------------------------------------------------------
# 向导接线：规划 → 逐段写并落盘 → 出站（#104 的打包与陪跑仍待接线）
# ---------------------------------------------------------------------------

_PLAN_JSON = """{"segments": [
  {"start_s": 0, "end_s": 6, "type": "opening", "summary": "a", "end_hook": "h1"},
  {"start_s": 6, "end_s": 12, "type": "continue", "summary": "b", "end_hook": "h2"},
  {"start_s": 12, "end_s": 18, "type": "continue", "summary": "c", "end_hook": "h3"},
  {"start_s": 18, "end_s": 24, "type": "continue", "summary": "d", "end_hook": "h4"},
  {"start_s": 24, "end_s": 30, "type": "continue", "summary": "e", "end_hook": "h5"}
]}"""


class _DirectorFlowLLM:
    """规划与写段共用一个模型：按请求里的角色名分派（真实流程也是同一个网关）。"""

    def __init__(self, plan):
        self.plan = plan
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        if "分段规划师" in request:
            return _PLAN_JSON
        index = int(re.search(r"Segment (\d+)/", request).group(1))
        return _single_reply(self.plan.segments[index - 1], self.plan)


def _director_session(tmp_path):
    from minimax_h3_prompt.session_store import SessionState

    return SessionState(
        directory=tmp_path / "GEN001",
        brief=Brief(mode="director", variant="T2VA", director_mode="t2v",
                    duration=30.0, plot="雨夜旧信",
                    # #104 起进导演台流程会先问打包参数（节点 id / 缓存名 / 引导模式）；
                    # 本文件测的是**写段**，把答案预先填好跳过那三问。
                    director_node_id="42", director_continuity_mode="guide"),
        stage_state={"shot_table": "分镜表：[Shot 1] 雨夜便利店……"},
        status="awaiting_frames",
    )


def test_wizard_director_flow_writes_segment_prompts_and_lists_them(tmp_path, monkeypatch,
                                                                   capsys):
    """真实向导路径：段表之后逐段写词并落盘，出站逐段给段号/类型/给图/路径。"""
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.director.planning import parse_director_plan
    from minimax_h3_prompt.ui import wizard

    plan = parse_director_plan(_PLAN_JSON, 30.0, mode="t2v")
    llm = _DirectorFlowLLM(plan)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: llm)
    # #105 起打包之后接逐段陪跑（要用户回导演台跑再回车）：本用例只测写段，
    # 在陪跑的第一问输入 q 停下，别让它去读真的 stdin。
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_flow(_director_session(tmp_path)) == 0

    segment_files = sorted((tmp_path / "GEN001" / "director").glob("segment-*.md"))
    assert [path.name for path in segment_files] == [
        "segment-01.md", "segment-02.md", "segment-03.md", "segment-04.md", "segment-05.md"]
    second = (tmp_path / "GEN001" / "director" / "segment-02.md").read_text(encoding="utf-8")
    assert second.startswith("Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video.")

    out = capsys.readouterr().out
    assert "段2" in out and "续接" in out and "segment-02.md" in out, "出站清单要能逐段核"
    # #104 已把打包接进这条流程：出站里「导演包」是**真产物**，逐段执行留给 #105。
    assert "导演包" in out and "#105" in out, "打包已完成，后续票号必须说出来"


def test_wizard_director_flow_stops_when_a_segment_cannot_be_written(tmp_path, monkeypatch,
                                                                     capsys):
    """写段失败要停下并说清段号，不假装流程跑完了。"""
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.ui import wizard

    class _Boom:
        def invoke(self, request, config=None):
            if "分段规划师" in request:
                return _PLAN_JSON
            raise RuntimeError("网关 502")

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: _Boom())

    assert wizard._run_director_flow(_director_session(tmp_path)) == 1
    assert "第 1 段" in capsys.readouterr().out





# ---------------------------------------------------------------------------
# #113：改词（rewrite_director_segment）——同一份写段上下文 + 同一个校验器
# ---------------------------------------------------------------------------

def test_rewrite_director_segment_asks_for_body_only_and_rebuilds_r2v_sections(monkeypatch):
    """r2v 改词：请求带**同一份**定义块与当前段提示词，产出仍是六节外壳。

    为什么不能用「重新写一段」代替：r2v 六节的第 1、3 节逐字取 ``subject_defs``
    （``_require_r2v_sections``），缺了就是半份空定义；而这两节只在 state 里。
    本用例把直调口换成记录器，断言请求里确实带上了它们，且产出过同一个校验器。
    """
    from minimax_h3_prompt.director import prompts
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections

    plan = _plan("r2v")
    segment = plan.segments[1]
    current = _r2v_text(segment, plan)      # 盘上的当前段提示词（成片坐标）
    reply = _r2v_reply(segment, plan)       # 模型回复：相对时间戳
    requests: list[str] = []

    def fake_direct_write(llm, request, *, role, label):
        requests.append(request)
        return reply

    monkeypatch.setattr(prompts, "_direct_write", fake_direct_write)

    outcome = prompts.rewrite_director_segment(
        plan, segment, llm=object(), current_text=current,
        sections=split_ref_sections(_SUBJECT_DEFS))

    assert outcome.errors == ()
    assert outcome.text is not None
    request = requests[0]
    # 同一份写段上下文：定义块（盘上这段自己的 1、3 节）与当前段提示词都在请求里。
    assert "<Picture 1> is a woman in a grey trench coat" in request
    # 当前段提示词交给模型前**先转回本段相对坐标**（2026-10-31 复审采纳）：盘上那份
    # 已是成片坐标，模型照抄会被组装器再加一次起点。``_r2v_reply`` 是测试侧独立
    # 构造的相对形态（时间戳 00:02.500），两者应逐字相同。
    assert prompts._REWRITE_REQUIREMENT_HEAD in request
    assert prompts._REWRITE_CURRENT_HEAD in request
    assert _r2v_reply(segment, plan).strip() in request
    # 产出仍是六节外壳，且定义块由组装器给定（模型改写不采信）。
    assert list(split_ref_sections(outcome.text)) == [
        "subject_definitions", "summary", "retention_analysis", "detailed_description",
        "overall_soundscape", "non_diegetic_music"]


def test_rewrite_request_rebases_the_current_prompt_back_to_segment_relative(monkeypatch):
    """改词请求先把已成片坐标的时间戳转回本段相对坐标（2026-10-31 复审采纳）。

    盘上的段文件是组装器产物：``At`` 已是成片坐标。原样交给模型，模型照抄后组装器
    会**再加一次**本段起点——时间戳翻倍、落到本段窗口之外，校验器判红、重写预算耗尽。
    本用例让假模型原样回显请求里的当前段提示词，钉住「最终仍只加一次」。
    """
    from minimax_h3_prompt.director import prompts

    plan = _plan("t2v")
    segment = plan.segments[1]              # start_frame=107 → 起点 4.458s
    current = _single_text(segment, plan)   # 成片坐标：本段内 2.5s → At 00:06.958
    requests: list[str] = []

    def echo(llm, request, *, role, label):
        requests.append(request)
        # 模型照抄请求里的当前段提示词（模型不会自己换算坐标）。
        return request.split(prompts._REWRITE_CURRENT_HEAD)[1]

    monkeypatch.setattr(prompts, "_direct_write", echo)
    outcome = prompts.rewrite_director_segment(
        plan, segment, llm=object(), current_text=current)

    assert outcome.errors == ()
    # 请求里给模型的是**本段相对坐标**（2.5 秒），不是成片坐标 6.958 秒。
    assert "At 00:02.500" in requests[0]
    assert "At 00:06.958" not in requests[0]
    # 组装器只加一次：最终仍是 06.958，不是 11.416（加两次的形态）。
    assert "At 00:06.958" in outcome.text
    assert "At 00:11.416" not in outcome.text
    # 校验器确认时间戳落在本段窗口（4.458s-10.833s）内。
    assert all(issue.code != "DIRECTOR_TIMESTAMP_OUT_OF_RANGE" for issue in outcome.issues)


def test_rewrite_director_segment_refuses_an_invalid_reply_without_a_text(monkeypatch):
    """校验不过且预算用尽 → 交回 issue 清单、``text`` 仍是最后那版（调用方据此不落盘）。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan("t2v")
    segment = plan.segments[1]
    bad = "overall_soundscape:\nrain\n[Shot 1] A woman turns.\n"

    monkeypatch.setattr(prompts, "_direct_write",
                        lambda llm, request, *, role, label: bad)

    outcome = prompts.rewrite_director_segment(
        plan, segment, llm=object(), current_text="段 2 的提示词")
    assert outcome.text is not None, "调用成功但内容不合格：text 仍是模型产出"
    assert [issue.code for issue in outcome.errors] == ["DIRECTOR_SECTION_MISSING"]


def test_rewrite_director_segment_returns_none_when_the_call_fails(monkeypatch):
    """调用失败 → ``text is None``（与首写同形），调用方保持原词、不落账。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan("t2v")
    segment = plan.segments[0]

    monkeypatch.setattr(prompts, "_direct_write",
                        lambda llm, request, *, role, label: None)

    outcome = prompts.rewrite_director_segment(
        plan, segment, llm=object(), current_text="段 1 的提示词")
    assert outcome.text is None
    assert outcome.errors == ()


# ---------------------------------------------------------------------------
# #193 落点语义：end_hook 是切点，不是要停下来的终点画面
# ---------------------------------------------------------------------------

def _r2v_request_for_segment(index: int) -> tuple:
    """造一份 r2v 请求（六节定义块必须给，否则 build_segment_request 报错）。"""
    from minimax_h3_prompt.director.prompts import build_segment_request, split_ref_sections

    plan = _plan("r2v")
    return plan, build_segment_request(plan, plan.segments[index],
                                       sections=split_ref_sections(_SUBJECT_DEFS))


def test_segment_request_reads_end_hook_as_the_cut_point():
    """端到端实测形态是「段尾先把画面停住、再硬跳」（#191）：请求行不能再要求落到终点。

    请求是模型离产出最近的前提，角色提示词里的纪律只有在这一行不反着说时才生效。
    """
    from minimax_h3_prompt.director.prompts import END_HOOK_CUT_CLAUSE

    plan, request = _r2v_request_for_segment(1)
    hook_line = next(line for line in request.splitlines()
                     if line.startswith("本段最后一帧的画面（end_hook）"))

    assert "正文必须落到这个状态" not in request, "旧的「落到终点」口径还在请求里"
    assert END_HOOK_CUT_CLAUSE in hook_line, "请求行没写「写到那一刻就结束」"
    assert plan.segments[1].end_hook in hook_line, "end_hook 原文没被注入"


def test_request_disclaimer_overrides_the_shared_settled_frame_pointer():
    """共用词表仍说「没给尾帧时落本段落定画面」，靠免责块就近压过（#191 U2）。

    改词表等于同时改分镜师／摄影指导／提示词工程师三份系统提示词，所以这一条只能
    在导演台请求的切片前言里声明「这一句按 end_hook 切点语义理解」。
    """
    from minimax_h3_prompt.director.prompts import SETTLED_FRAME_POINTER

    _, request = _r2v_request_for_segment(1)
    disclaimer = request.split("## 词表切片", 1)[1]
    assert SETTLED_FRAME_POINTER in disclaimer, "免责块没点名词表里那条「落定画面」指针"
    assert "落定画面" in disclaimer
    # #193 复审：免责块不能只放宽 r2v——fl2v 的「仅首帧」「无图」两段同样按切点理解。
    assert "导演台分段没给尾帧时" in disclaimer, \
        "免责块没说导演台分段没给尾帧时按 end_hook 切点语义理解"
    assert "有尾帧时仍以尾帧构图为终点" in disclaimer, \
        "免责块没说有尾帧时仍以尾帧构图为终点"
    assert "end_hook 语义" in disclaimer, \
        "免责块没说按 end_hook 语义理解"


def test_shared_wordlist_keeps_the_settled_frame_pointer_verbatim():
    """共用词表正文逐字未改：本票只加免责块，不去动四个角色共用的真源。

    选「钉住那句话」而不是整份 sha256：整份哈希会在别的票合法改词表时同样报红，
    那不是这条不变式要守的东西；要守的是免责块所指的那句话仍在、仍是原话。
    去空白与强调符后再比，因为真源里这句话跨了行、还带 ``**``。
    """
    from minimax_h3_prompt import film_language
    from minimax_h3_prompt.director.prompts import SETTLED_FRAME_POINTER

    source = film_language.VIDEO_SOURCE_PATH.read_text(encoding="utf-8")
    normalized = re.sub(r"[\s*]+", "", source)
    assert re.sub(r"[\s*]+", "", SETTLED_FRAME_POINTER) in normalized, \
        "共用词表里「没给尾帧时改成落本段的落定画面」那一句被改掉了"


def test_r2v_instruction_stops_at_the_end_hook_without_a_still_tail_shot():
    """写段角色的 r2v 节要有落点纪律：末镜写到那一刻即停，不得补静止尾镜。"""
    from minimax_h3_prompt.director.prompts import load_instruction

    r2v = load_instruction("r2v")
    assert "那一刻即停" in r2v, "r2v 指令没说末镜写到 end_hook 那一刻即停"
    assert "静止" in r2v and "尾镜" in r2v, "r2v 指令没禁止追加静止尾镜"
    assert "落点纪律" in r2v, "r2v 指令没把这条写成可认出的纪律块"


def test_i2v_and_fl2v_instructions_stop_at_the_end_hook_instead_of_settling():
    """#193 切点句要覆盖 i2v/fl2v 的每一条 end_hook 分支（复审口径不一致）。

    r2v 已有专门的落点纪律块，但 i2v 的「结果或反应」与 fl2v 的「仅首帧」「无图」
    「差异逐步收窄」「没有尾帧时」仍写着「落到 end_hook」——同一份提示词里两种口径
    会让模型去追一个静止终点。**有尾帧时**的官方 ``settles into …`` 句式不动。
    """
    from minimax_h3_prompt.director.prompts import load_instruction

    for mode in ("i2v", "fl2v"):
        text = load_instruction(mode)
        assert "那一刻即停" in text, f"{mode} 指令没写「写到 end_hook 那一刻即停」"
        assert "落到请求里的 end_hook" not in text, \
            f"{mode} 仍留着「落到 end_hook」的终点口径"
    assert "settles into the pose" in load_instruction("fl2v"), \
        "有尾帧时的官方终点构图句被误改（它保持不变）"
