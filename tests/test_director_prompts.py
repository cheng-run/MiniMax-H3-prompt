"""导演台提示词方言（issue #103）：按模式分派、绝对时间轴、路线能力判据。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. 路线能力判据（``brief_parser`` 的两个纯函数）与 ``_brief_block`` 送模式；
2. 校验器/组装器**不许把 director 当 base**（#103 硬约束：不许靠「反正不走那条路」）；
3. ``director/prompts.py`` 的方言产物：r2v 六节外壳＋时序写法＋无状态句，
   t2v/i2v/fl2v 单段正文，v2v/rv2v/mixed 一期报错；时间轴注记按帧重算成绝对位置；
4. 向导接线：逐段落 ``GENxxx/director/segment-NN.md`` 并出站段号/类型/给图/路径。

断言只碰产物与返回值，不断言私有调用顺序。
"""
from __future__ import annotations

import json
import re

import pytest

from minimax_h3_prompt.brief_parser import (
    Brief,
    director_frame_variant,
    route_emits_asset_prompts,
    route_emits_frame_prompts,
)


# ---------------------------------------------------------------------------
# 路线能力判据（设计 §4「唯一真源」）：不许只写 or brief.is_director
# ---------------------------------------------------------------------------

def test_route_capabilities_are_derived_from_route_and_mode():
    """真值表：ref 全产资产图；director 只有 r2v/rv2v/mixed 产资产图、只有 i2v/fl2v 产帧图。

    为什么必须按模式分派：``t2v``/``v2v`` 没有参考素材可出词，``i2v`` 只要首帧。
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
        ("director", "v2v", "T2VA", False, False),
        ("director", "rv2v", "T2VA", True, False),
        ("director", "mixed", "T2VA", True, False),
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

_SINGLE_BODY = """{note}
[Shot 1] A woman in a grey trench coat steps under the shop awning.
[Shot 2] At {cut}, the camera pushes in until her face fills the frame.
Ambience: rain on the metal awning, footsteps on wet concrete, faint shop chime.
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


def test_validate_single_body_modes_reject_section_headers():
    """t2v/i2v/fl2v 是单段正文：出现段头就说明写成了六节/三节，必须报红。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan("t2v")
    segment = plan.segments[1]
    for mode in ("t2v", "i2v", "fl2v"):
        assert validate_director_segment(_single_text(segment, plan), mode,
                                         plan=plan, segment=segment) == []
        polluted = "overall_soundscape:\nrain\n" + _single_text(segment, plan)
        codes = {i.code for i in validate_director_segment(polluted, mode,
                                                           plan=plan, segment=segment)}
        assert "DIRECTOR_SECTION_PRESENT" in codes, mode


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


def test_phase_two_modes_are_refused_loudly():
    """v2v/rv2v/mixed 是二期（#109/#110）：一期必须报错，不许静默按别的模式写段。"""
    from minimax_h3_prompt.director.prompts import validate_director_segment

    plan = _plan()
    segment = plan.segments[1]
    for mode in ("v2v", "rv2v", "mixed"):
        with pytest.raises(ValueError, match="#109|#110|二期"):
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
    from minimax_h3_prompt.director.prompts import normalize_segment_output, split_ref_sections

    plan = _plan("i2v")
    segment = plan.segments[1]
    text = normalize_segment_output(
        _SINGLE_BODY.format(note="", cut="00:01.000"), plan=plan, segment=segment)
    assert text.splitlines()[0] == (
        "Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video.")
    assert "At 00:05.458" in text, "1.000s 相对本段 + 107/24 应为 5.458"
    assert "subject_definitions:" not in text, "单段正文不许出现段头"


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

    plan = _plan("r2v")
    llm = _DialectLLM(lambda index: _r2v_reply(plan.segments[index - 1], plan))
    write_director_segments(plan, llm=llm, session_dir=tmp_path, subject_defs=_SUBJECT_DEFS)

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
    bad = reply.replace("[Shot 1]", "overall_soundscape:\nrain\n[Shot 1]")

    llm = _DialectLLM(None, behaviour=lambda call, request: bad if call == 1 else reply)
    text = prompts.write_director_segment(plan, segment, llm=llm)

    assert text.strip() == good.strip()
    assert len(llm.requests) == 2
    assert "DIRECTOR_SECTION_PRESENT" in llm.requests[1], "重写请求必须点名上一版的问题"


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


def test_phase_two_modes_refuse_to_write(tmp_path):
    """v2v/rv2v/mixed 一期不写段（票面 AC）：不许静默套用别的模式。"""
    from minimax_h3_prompt.director.prompts import write_director_segments

    for mode in ("v2v", "rv2v", "mixed"):
        with pytest.raises(ValueError, match="#109|#110|二期"):
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
                    duration=30.0, plot="雨夜旧信"),
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

    assert wizard._run_director_flow(_director_session(tmp_path)) == 0

    segment_files = sorted((tmp_path / "GEN001" / "director").glob("segment-*.md"))
    assert [path.name for path in segment_files] == [
        "segment-01.md", "segment-02.md", "segment-03.md", "segment-04.md", "segment-05.md"]
    second = (tmp_path / "GEN001" / "director" / "segment-02.md").read_text(encoding="utf-8")
    assert second.startswith("Segment 2 of 5, covering 4.458s-10.833s of the full 29.958s video.")

    out = capsys.readouterr().out
    assert "段2" in out and "续接" in out and "segment-02.md" in out, "出站清单要能逐段核"
    assert "导演包" in out and "#104" in out, "打包与陪跑的后续票号必须说出来"


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



