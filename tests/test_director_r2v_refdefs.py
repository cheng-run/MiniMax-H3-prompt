"""导演台 r2v 定义块（2026-11-01 真实验收现场）：中文素材 → 英文六节；每段素材子集。

两个**真实**形态（不是自证 fixture）：

1. 阶段 1 的 ``subject_defs`` 是**中文 markdown**——``reference_consistency`` 的角色提示词
   第一句就是「你的产出（markdown）」，模型于是写 ``## subject_definitions``；而
   ``split_ref_sections`` 只认行首裸标签 ``subject_definitions:``，解析结果是**空字典**，
   r2v 写段当场炸在「定义块缺失」上。
2. 素材台账可能登记 **13 张**图，而插件 ``lib/ref_images.py`` 的
   ``MAX_REFERENCE_IMAGES = 9`` 是**每次请求**（＝每段）的上限。用户 2026-11-01 裁定：
   「导演台 r2v 每段最多 9 张图 + 3 个视频 + 3 段音频，也就是说可以按段上传素材」——
   推翻设计 §3.4.1 的「全量挂到每段」。
"""
from __future__ import annotations

import json
import re

import pytest


# 真实会话里 ``subject_defs`` 的形态：中文、markdown 标题、正文带反引号标签。
_REAL_SHAPE_MATERIAL = """## subject_definitions

> 人物、道具、空间均按语义命名；`<Picture N>` 仅作为具体资产锚点。

### 人物

`<Subject 1> 是“日本女高中生”的固定身份外观：视觉年龄约16—17岁，普通偏纤细、健康匀称。`

锚点：`<Picture 1> 是 <Subject 1> 的正面全身、严格侧面和四分之三侧身份母版。`

`<Subject 2> 是同一名日本女高中生的四档连续表情。`

锚点：`<Picture 2> 是 <Subject 2> 的表情母版。`

### 道具

`<Subject 3> 是玄关门镜。`

锚点：`<Picture 3> 是门镜的外观母版。`

## retention_analysis

### 各标签保留关系

`<Subject 1>: fully_preserved；<Subject 2>: fully_preserved；<Subject 3>: fully_preserved`
"""

# 13 张登记（3 人物 + 5 道具 + 5 场景）：与真实会话一致，超过每段 9 张上限。
_REGISTRY = (
    [(number, f"人物{number}") for number in (1, 2, 3)]
    + [(number, f"道具{number}") for number in (4, 5, 6, 7, 8)]
    + [(number, f"场景{number}") for number in (9, 10, 11, 12, 13)]
)


# 中文语义名 → 英文描述：模型该做的事就是翻过来（语言闸按中文占比判，原样照抄会被判红）。
_EN_NAME = {1: "high-school girl", 2: "girl expression sheet", 3: "door viewer",
            4: "living-room sofa", 5: "remote control", 6: "glass cup",
            7: "entryway door", 8: "ceiling lamp", 9: "apartment living room",
            10: "entry hall", 11: "dark corridor", 12: "television set",
            13: "night street"}


def _english_definitions(registry=_REGISTRY) -> str:
    """模型**该写出来**的形态：英文、行首裸标签、每张登记图都有定义。"""
    lines = ["subject_definitions:"]
    for number, _name in registry:
        lines.append(f"<Subject {number}> is the {_EN_NAME[number]} reference subject.")
        lines.append(f"<Picture {number}> is the reference sheet of <Subject {number}>.")
    lines += ["", "retention_analysis:"]
    for number, _name in registry:
        lines.append(f"<Picture {number}>: fully_preserved.")
    return "\n".join(lines)


class _RefDefsLLM:
    """两段调用共用一个模型：定义块请求回英文两节，写段请求回六节段正文。"""

    def __init__(self):
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        if "subject_definitions 与 retention_analysis" in request:
            return _english_definitions()

        # 写段请求：三个段头都齐，只引用前 3 张图（本段真正用到的素材）。
        note = re.search(r"Segment \d+ of \d+, covering [^\n]+", request).group(0)
        return (
            "subject_definitions:\nplaceholder\n\n"
            "summary:\n[reference generation] the girl looks at the door.\n\n"
            "retention_analysis:\nplaceholder\n\n"
            "detailed_description:\n"
            f"{note}\n"
            "[Shot 1] <Picture 1> sits on the sofa; <Picture 2> is on the table; "
            "<Picture 3> is near the door.\n\n"
            "overall_soundscape:\nA television murmurs in the next room.\n\n"
            "non_diegetic_music:\nN/A"
        )


def _segment(index, segment_type, start, end, declared, export, guided):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=end,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=(),
    )


def _plan():
    from minimax_h3_prompt.director.frames import DirectorPlan

    segments = (
        _segment(1, "opening", 0, 107, 107, 107, False),
        _segment(2, "continue", 107, 260, 141, 153, True),
    )
    return DirectorPlan(mode="r2v", total_frames=260, segments=segments)


def test_real_shape_material_becomes_parsable_english_sections():
    """真实形态（中文 + ``##`` 标题）→ 英文、行首裸标签、13 张图全覆盖。"""
    from minimax_h3_prompt.director import refdefs
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections

    llm = _RefDefsLLM()
    sections = refdefs.build_ref_sections(
        material=_REAL_SHAPE_MATERIAL, registry=_REGISTRY, llm=llm)

    parsed = split_ref_sections(refdefs.render_sections(sections))
    assert parsed["subject_definitions"].strip(), "定义块必须能被 split_ref_sections 认出来"
    assert parsed["retention_analysis"].strip()
    assert not re.search(r"[一-鿿]", parsed["subject_definitions"]), \
        "r2v 段是全英文外壳，中文定义直接进提示词就是 issue #75 的语言缺陷"
    for number, _name in _REGISTRY:
        assert f"<Picture {number}>" in parsed["subject_definitions"], \
            f"登记图 {number} 没有定义：<Picture N> 会悬空"


def test_segment_subset_is_capped_and_renumbered(tmp_path):
    """13 张登记 → 每段只留本段引用的图，号从 1 起连续；不超 9 张。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    llm = _RefDefsLLM()
    from minimax_h3_prompt.director import refdefs

    sections = refdefs.build_ref_sections(
        material=_REAL_SHAPE_MATERIAL, registry=_REGISTRY, llm=llm)
    written = prompts.write_director_segments(
        plan, llm=llm, session_dir=tmp_path / "GEN001",
        sections=sections, registry=_REGISTRY)

    assert len(written) == 2
    for item in written:
        text = item.path.read_text(encoding="utf-8")
        pictures = sorted({int(m) for m in re.findall(r"<Picture\s+(\d+)>", text)})
        assert pictures and max(pictures) <= 9, \
            f"第 {item.index} 段引用了 <Picture {max(pictures)}>，超过每段 9 张上限"
        assert item.issues == (), f"第 {item.index} 段校验没过：{item.issues}"
        assert not re.search(r"[一-鿿]", text.split("retention_analysis:")[0]), \
            "段内定义块必须是英文"


class _OverflowThenFixLLM(_RefDefsLLM):
    """第一版写 10 张（超限），被退回后按问题清单改成 9 张——真实现场的形状。"""

    def invoke(self, request, config=None):
        if "subject_definitions 与 retention_analysis" in request:
            return super().invoke(request, config)
        self.requests.append(request)

        note = re.search(r"Segment \d+ of \d+, covering [^\n]+", request).group(0)
        fixed = "上一次产出被校验器判为不合格" in request
        count = 9 if fixed else 10
        pictures = " ".join(f"<Picture {n}>" for n in range(1, count + 1))
        return (
            "subject_definitions:\nplaceholder\n\n"
            "summary:\n[reference generation] the girl looks at the door.\n\n"
            "retention_analysis:\nplaceholder\n\n"
            "detailed_description:\n"
            f"{note}\n"
            f"[Shot 1] {pictures} appear in order as the camera pans the room.\n\n"
            "overall_soundscape:\nA television murmurs.\n\n"
            "non_diegetic_music:\nN/A"
        )


def test_overflow_is_written_back_to_the_model_instead_of_truncating(tmp_path):
    """每段 10 张 > 插件上限 9 → 带着问题清单重写，不静默截断。

    2026-11-01 真实验收现场（第 2 段）：截掉一张图，正文里那句指称就没有接线可落，
    跑出来是「主体凭空少了一个」。所以超限 = 一条**校验错误**，回灌给模型自己取舍。
    """
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    llm = _OverflowThenFixLLM()
    written = prompts.write_director_segments(
        plan, llm=llm, session_dir=tmp_path / "GEN001",
        sections={"subject_definitions": "<Picture 1> is the girl.",
                  "retention_analysis": "<Picture 1>: fully_preserved."},
        registry=_REGISTRY)

    assert any("DIRECTOR_PICTURE_OVERFLOW" in request for request in llm.requests), \
        "超限必须作为校验错误回灌给模型（而不是用户看到的那条一次性报错）"
    for item in written:
        text = item.path.read_text(encoding="utf-8")
        used = {int(m) for m in re.findall(r"<Picture\s+(\d+)>", text)}
        assert max(used) <= 9, f"第 {item.index} 段仍超 9 张：{sorted(used)}"
        assert item.issues == ()


_SCOPE_REGISTRY = ((1, "red bicycle"), (2, "blue car"))

# 定义块里只有「红自行车」带独立 Picture 行；正文点名的是「蓝车」，裁完子集后
# `<Picture 1>` 那条定义被滤掉——于是「裁好子集」与「复判不合格」能同时发生。
_SCOPE_SECTIONS = {
    "subject_definitions": (
        "<Subject 1> is the red bicycle.\n"
        "<Picture 1> is the red bicycle master.\n"
        "<Subject 2> is the blue car."),
    "retention_analysis": (
        "<Subject 1>: fully_preserved.\n"
        "<Subject 2>: fully_preserved."),
}


class _ScopeThenBreakLLM:
    """第 1 轮裁出了子集但复判不过，第 2 轮直接写坏（返回的文本与 record 必须同源）。"""

    def __init__(self):
        self.requests: list[str] = []

    def invoke(self, request, config=None):

        self.requests.append(request)
        if "上一次产出被校验器判为不合格" in request:
            # 第 2 轮：没有六节段头 → 校验器直接判红，这一轮根本走不到裁剪。
            return "太短，没有六节。"
        note = re.search(r"Segment \d+ of \d+, covering [^\n]+", request).group(0)
        return (
            "subject_definitions:\nplaceholder\n\n"
            "summary:\n[reference generation] the blue car waits at the curb.\n\n"
            "retention_analysis:\nplaceholder\n\n"
            "detailed_description:\n"
            f"{note}\n"
            "[Shot 1] the street stays quiet.\n\n"
            "overall_soundscape:\nTraffic murmurs.\n\n"
            "non_diegetic_music:\nN/A"
        )


def test_failed_round_never_lands_a_stale_scope_sidecar(tmp_path):
    """第 2 轮写坏：第 1 轮的子集记录不许落成段内素材表（#185 起该段硬失败）。

    旧实现把「第 1 轮 record + 第 2 轮坏词」一起交回，调用方照 record 落
    ``assets-in-segment.json``，而盘上的段文件是第 2 轮的坏词——打包期按表挂图，
    段词与挂图从此不同源。现在内容不合格即拒绝：坏词只进 ``.rejected.md``，
    正式段文件与素材表都不写，这个错配在结构上不再可能发生。
    """
    from minimax_h3_prompt.director import prompts

    segment = _segment(1, "opening", 0, 107, 107, 107, False)
    from minimax_h3_prompt.director.frames import DirectorPlan

    plan = DirectorPlan(mode="r2v", total_frames=107, segments=(segment,))
    session_dir = tmp_path / "GEN001"
    llm = _ScopeThenBreakLLM()

    with pytest.raises(prompts.DirectorSegmentRejectedError) as excinfo:
        prompts.write_director_segments(
            plan, llm=llm, session_dir=session_dir,
            sections=_SCOPE_SECTIONS, registry=_SCOPE_REGISTRY)

    assert len(llm.requests) == 2, "第 1 轮裁完复判失败，应触发一次重写"
    assert "DIRECTOR_SECTION_MISSING" in str(excinfo.value), \
        "第 2 轮写坏必须带着问题清单交回（不是 None 装死）"
    assert not prompts.segment_assets_path(session_dir).exists(), \
        "第 2 轮没有裁子集，不能把第 1 轮的子集记录落成段内素材表（段词与挂图会不同源）"
    assert not prompts.segment_path(session_dir, 1).exists(), "坏段不许写正式文件名"
    assert prompts.rejected_segment_path(session_dir, 1).is_file()


class _SecondSegmentStaysOverBudgetLLM(_RefDefsLLM):
    """第 1 段正常；第 2 段每一版都写 10 张——有界重写用尽仍超限的现场。"""

    def invoke(self, request, config=None):
        if "subject_definitions 与 retention_analysis" in request:
            return super().invoke(request, config)
        index = int(re.search(r"Segment (\d+)/", request).group(1))
        if index != 2:
            return super().invoke(request, config)
        self.requests.append(request)
        note = re.search(r"Segment \d+ of \d+, covering [^\n]+", request).group(0)
        pictures = " ".join(f"<Picture {n}>" for n in range(1, 11))
        return (
            "subject_definitions:\nplaceholder\n\n"
            "summary:\n[reference generation] the girl looks at the door.\n\n"
            "retention_analysis:\nplaceholder\n\n"
            "detailed_description:\n"
            f"{note}\n"
            f"[Shot 1] {pictures} appear in order as the camera pans the room.\n\n"
            "overall_soundscape:\nA television murmurs.\n\n"
            "non_diegetic_music:\nN/A"
        )


def test_segment_that_stays_over_budget_is_rejected_not_written(tmp_path):
    """重试用尽仍超 9 张：中止写段、点名段号，坏段改存 ``.rejected.md``。

    2026-11-06 事故形态：第 5 段真实引用了 10 张，坏段被照写成正式文件，用户直到
    打包才收到一句不点名的话。现在超限在写段期就硬失败，且坏文本仍留档可对照。
    """
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    sections = {"subject_definitions": "<Picture 1> is the girl.",
                "retention_analysis": "<Picture 1>: fully_preserved."}
    llm = _SecondSegmentStaysOverBudgetLLM()

    with pytest.raises(prompts.DirectorSegmentRejectedError) as excinfo:
        prompts.write_director_segments(
            plan, llm=llm, session_dir=session_dir,
            sections=sections, registry=_REGISTRY)

    message = str(excinfo.value)
    assert "第 2 段" in message, "失败信息必须点名段号（用户不必翻完整份日志）"
    assert "DIRECTOR_PICTURE_OVERFLOW" in message, "失败信息必须点名真正的问题"
    assert "调用失败" not in message, "内容不合格与调用失败是两种表达，不能混成一类"
    assert not isinstance(excinfo.value, RuntimeError), \
        "内容不合格不能用「调用失败」那一种异常类型表达"

    director = session_dir / "director"
    assert (director / "segment-01.md").is_file(), "已落盘的前几段必须保留"
    assert not (director / "segment-02.md").exists(), "坏段不许占正式文件名"
    rejected = director / "segment-02.rejected.md"
    assert rejected.is_file(), "坏段要留档：同目录、同段号"
    assert "<Picture 10>" in rejected.read_text(encoding="utf-8"), \
        ".rejected.md 里必须是被拒的那一版原文（不是空壳）"


def test_rejected_file_is_cleaned_only_for_the_segment_that_now_passes(tmp_path):
    """该段重写成功后清掉它自己那份 ``.rejected.md``；别的段的一律不动。"""
    from minimax_h3_prompt.director import prompts, refdefs

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    director = session_dir / "director"
    director.mkdir(parents=True)
    own = director / "segment-01.rejected.md"
    unrelated = director / "segment-03.rejected.md"
    own.write_text("旧的坏段 1", encoding="utf-8")
    unrelated.write_text("别的段的坏段", encoding="utf-8")

    llm = _RefDefsLLM()
    sections = refdefs.build_ref_sections(
        material=_REAL_SHAPE_MATERIAL, registry=_REGISTRY, llm=llm)
    prompts.write_director_segments(
        plan, llm=llm, session_dir=session_dir,
        sections=sections, registry=_REGISTRY)

    assert not own.exists(), "本段这次写成功了，它自己的坏段证据该清掉"
    assert unrelated.is_file(), \
        "不许整批清空：别的段的 .rejected.md 是用户排查到一半的证据"


def test_request_states_the_per_segment_budget_up_front():
    """请求里先写清「每段最多 9 张」：模型第一版就该按剧情取舍，而不是写满再退回。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    request = prompts.build_segment_request(
        plan, plan.segments[1],
        sections={"subject_definitions": "<Picture 1> is the girl.",
                  "retention_analysis": "<Picture 1>: fully_preserved."})
    assert "本段参考图预算" in request and "9 张" in request
    assert "不会替你删图" in request, "要说清超限的处置：退回重写，不是悄悄截断"


# --- 同一角色的多张母版（L6-09 / L6-10，票 #134） -------------------------
#
# 存量实跑里的形状（``output/.../GEN001/director/ref-sections.json``）：三个 Subject
# 各自对着一张母版图，而三张图的语义名共享前缀 ``CHAR_01``——模型把「同一个人」拆成了
# 三个主体。合并写法才是官方形态（ref-en.txt:68「同一 subject 来自多个资产时合并来源」）。
_MULTI_MASTER_REGISTRY = (
    (1, "CHAR_01_身份母版"),
    (2, "CHAR_01_表情母版"),
    (3, "CHAR_01_服装母版"),
    (4, "普通客厅电视"),
)

_SPLIT_MASTERS_TEXT = """subject_definitions:
<Subject 1> is the fixed identity appearance of the high-school girl.
<Subject 2> is the four-stage expression sequence of the same high-school girl.
<Subject 3> is the fixed homewear outfit worn by the same high-school girl.
<Subject 4> is the ordinary LCD television in the living room.
<Picture 1> is the identity-master image for <Subject 1>.
<Picture 2> is the expression-master image for <Subject 2>.
<Picture 3> is the wardrobe-master image for <Subject 3>.
<Picture 4> is the asset image for <Subject 4>.

retention_analysis:
<Picture 1>: fully_preserved.
<Picture 2>: fully_preserved.
<Picture 3>: fully_preserved.
<Picture 4>: fully_preserved.
"""

_MERGED_MASTERS_TEXT = """subject_definitions:
<Subject 1> is the high-school girl whose appearance comes from <Picture 1>, whose expressions come from <Picture 2>, and whose outfit comes from <Picture 3>.
<Subject 2> is the ordinary LCD television in the living room, from <Picture 4>.

retention_analysis:
<Picture 1>: fully_preserved.
<Picture 2>: fully_preserved.
<Picture 3>: fully_preserved.
<Picture 4>: fully_preserved.
"""


def test_same_prefix_subjects_raise_a_merge_warning_but_no_error():
    """同一语义名前缀下的多个 Subject → warning（提示可合并），且一条硬错都不报。"""
    from minimax_h3_prompt.director import refdefs

    check = refdefs._guard_issues(_SPLIT_MASTERS_TEXT, _MULTI_MASTER_REGISTRY)

    assert check.problems == (), "合并建议不该判死：定义块本身是合格的"
    assert len(check.warnings) == 1, f"应只报一条合并建议：{check.warnings}"
    warning = check.warnings[0]
    assert refdefs.SUBJECT_MERGE_CANDIDATE in warning, "告警要带可 grep 的判据码"
    assert "CHAR_01" in warning, "要点名是哪组前缀，否则用户不知道该合并谁"
    for number in (1, 2, 3):
        assert f"<Subject {number}>" in warning, f"告警没点名 <Subject {number}>"


def test_merged_masters_produce_no_warning_and_no_error():
    """合并写法（一个 Subject 逐图说明来源）→ 既无告警也无硬错。"""
    from minimax_h3_prompt.director import refdefs

    check = refdefs._guard_issues(_MERGED_MASTERS_TEXT, _MULTI_MASTER_REGISTRY)

    assert check.problems == ()
    assert check.warnings == ()


def test_reference_consistency_prompt_no_longer_claims_chinese_goes_in_verbatim():
    """定义句是**中文素材**，下游转英文后才进最终提示词（#155）。"""
    from minimax_h3_prompt.agents import load_role_prompt

    text = load_role_prompt("reference_consistency")
    assert "中文素材" in text, "没写定义句是中文素材"
    assert "英文" in text and ("下游" in text or "转" in text), "没写下游会转英文"
    assert "直接进入最终提示词" not in text, "还声称中文会逐字进入最终提示词"


def test_request_carries_the_subject_first_naming_rule():
    """refdefs 示例同步 Subject/Picture 分工（#155）：可复用内容先 Subject。"""
    from minimax_h3_prompt.director import refdefs

    request = refdefs._build_request(_REAL_SHAPE_MATERIAL, _REGISTRY)
    assert "<Subject N>" in request and "可复用" in request
    assert "正文里指称" in request and "只留给" in request


def test_request_asks_every_picture_to_be_cited_not_defined_on_its_own_line():
    """规则 3：从「每张都要有定义」改成「每张都必须被**引用**」，两种合法形态都写清。"""
    from minimax_h3_prompt.director import refdefs

    request = refdefs._build_request(_REAL_SHAPE_MATERIAL, _REGISTRY)

    assert "必须被**引用**" in request, "判据是「被引用」，不是「各占一行」"
    assert "来源" in request, "第一种合法形态：作为某个 Subject 的来源"
    assert "每一张都要在 subject_definitions 里有定义" not in request, \
        "旧措辞会把模型推向一图一行，正是 L6-09 要纠的形态"


def test_request_carries_the_official_merge_sentence_and_the_no_extra_line_rule():
    """规则 5：官方合并句式 + 「图只作来源时不单开 Picture 行」（ref-en.txt:68）。"""
    from minimax_h3_prompt.director import refdefs

    request = refdefs._build_request(_REAL_SHAPE_MATERIAL, _REGISTRY)

    assert ("whose appearance comes from <Picture 1>, whose expressions come from "
            "<Picture 2>, and whose outfit comes from <Picture 3>") in request, \
        "要逐字给官方合并句式：口头说「可以合并」模型不会照做"
    assert "不单开" in request and "<Picture N>" in request, \
        "只作来源的图不许单开 Picture 行，否则合并之后又多出一排空行"


class _SplitMastersLLM:
    """每次调用都回「一个人拆成三个 Subject」那一版（模型最常见的初稿形态）。"""

    def __init__(self):
        self.calls = 0

    def invoke(self, request, config=None):
        self.calls += 1
        return _SPLIT_MASTERS_TEXT


def test_merge_warning_reaches_the_user_and_never_triggers_a_rewrite(capsys):
    """告警要**出站**（否则等于没接线），且不吃重写预算（一次调用就收工）。"""
    from minimax_h3_prompt.director import refdefs

    llm = _SplitMastersLLM()
    sections = refdefs.build_ref_sections(
        material="人物：日本女高中生（身份／表情／服装各一张）。",
        registry=_MULTI_MASTER_REGISTRY, llm=llm)

    assert llm.calls == 1, "合并建议不是硬错：不能退回重写，更不能耗尽预算抛错"
    assert sections.subject_definitions.strip()
    printed = capsys.readouterr().out
    assert refdefs.SUBJECT_MERGE_CANDIDATE in printed, \
        "告警只留在函数返回值里等于用户看不到：必须随生成过程出站"


def test_scope_sidecar_records_per_segment_mounts(tmp_path):
    """段内素材表落盘（打包照它挂图）：new/original/name 三者齐全。"""
    from minimax_h3_prompt.director import prompts, refdefs

    plan = _plan()
    llm = _RefDefsLLM()
    sections = refdefs.build_ref_sections(
        material=_REAL_SHAPE_MATERIAL, registry=_REGISTRY, llm=llm)
    prompts.write_director_segments(
        plan, llm=llm, session_dir=tmp_path / "GEN001",
        sections=sections, registry=_REGISTRY)

    scope_file = tmp_path / "GEN001" / "director" / "assets-in-segment.json"
    assert scope_file.is_file(), "段内素材表必须落盘：打包期要照它挂图，不能重算另一份口径"
    data = json.loads(scope_file.read_text(encoding="utf-8"))
    for index in ("1", "2"):
        entries = data[index]
        assert [entry["new"] for entry in entries] == list(range(1, len(entries) + 1))
        assert {entry["name"] for entry in entries} <= {name for _n, name in _REGISTRY}


# --- 重跑单段：合并写与摘要按子集重编号（票 #184） -------------------------
#
# 真实现场 ``output/sessions/一段电影的预告片-内容是在美国某个州突然-efeace45``：
# 第 1 段只挂两张图（原号 8、18 → 段内 1、2），摘要却按整片编号写成 ``<Picture 18>`` /
# ``<Picture 8>``——重建段文本时摘要、环境声、配乐三节取了未裁剪的原文，于是留在了
# 段内不存在的号上。同一处整份覆盖写的表还会让「只重跑第 5 段」把其它 9 段一起抹掉。

# 登记 5 张、本段只用其中 2 张：只有真子集才会走重建那一支（全量即为子集时不改一字）。
_STALE_SUMMARY_REGISTRY = (
    (5, "commuter car"),
    (8, "neighborhood notice sign"),
    (16, "distant skyline"),
    (18, "suburban street corner"),
    (22, "front yard and driveway"),
)

_STALE_SUMMARY_SECTIONS = {
    "subject_definitions": (
        "<Subject 8> is the fixed neighborhood notice sign; "
        "its appearance comes from <Picture 8>.\n"
        "<Picture 8> is the notice sign master.\n"
        "<Subject 18> is the suburban street corner; "
        "its establishing frame comes from <Picture 18>.\n"
        "<Picture 18> is the street corner master.\n"
        "<Picture 5> is the commuter car.\n"
        "<Picture 16> is the distant skyline.\n"
        "<Picture 22> is the front yard and driveway."),
    "retention_analysis": (
        "<Picture 8>: fully_preserved.\n"
        "<Picture 18>: fully_preserved.\n"
        "<Picture 5>: not_in_frame.\n"
        "<Picture 16>: not_in_frame.\n"
        "<Picture 22>: not_in_frame."),
}


class _StaleSummaryLLM:
    """段正文只点名两张图，摘要/音轨两节却按整片编号写旧号（真实现场的形态）。"""

    def invoke(self, request, config=None):
        note = re.search(r"Segment \d+ of \d+, covering [^\n]+", request).group(0)
        return (
            "subject_definitions:\nplaceholder\n\n"
            "summary:\n[reference generation] The opening wide shot anchors the corner of "
            "<Picture 18> and the notice sign of <Picture 8>.\n\n"
            "retention_analysis:\nplaceholder\n\n"
            "detailed_description:\n"
            f"{note}\n"
            "[Shot 1] The suburban street corner (<Subject 18>) holds still while the fixed "
            "notice sign (<Subject 8>) switches to its amber alert state.\n\n"
            "overall_soundscape:\nA short electronic alert tone enters beside <Picture 8>.\n\n"
            "non_diegetic_music:\nA low drone rises as <Picture 18> lights up."
        )


def _one_segment_plan():
    from minimax_h3_prompt.director.frames import DirectorPlan

    return DirectorPlan(mode="r2v", total_frames=107,
                        segments=(_segment(1, "opening", 0, 107, 107, 107, False),))


def _segment_block(raw: str, index: int) -> str:
    """段内素材表原文里某一号的整块（含前导缩进与尾随逗号），用于逐字节比对。"""
    match = re.search(rf'^  "{index}": \[.*?^  \],?\n', raw, flags=re.S | re.M)
    assert match, f"段内素材表里没有第 {index} 段的记录"
    return match.group(0)


def test_summary_and_audio_sections_are_renumbered_with_the_subset(tmp_path):
    """摘要/环境声/配乐按**裁剪后**的版本重建：段内不再残留整片旧号。

    只改摘要不换号的话，用户拿到的段词是「正文只有 ``<Picture 1>``、``<Picture 2>``，
    摘要却指着 ``<Picture 18>``」——一个段内根本不存在的号，模型与打包都无从对。
    """
    from minimax_h3_prompt.director import prompts
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections

    plan = _one_segment_plan()
    written = prompts.write_director_segments(
        plan, llm=_StaleSummaryLLM(), session_dir=tmp_path / "GEN001",
        sections=_STALE_SUMMARY_SECTIONS, registry=_STALE_SUMMARY_REGISTRY)

    assert written[0].issues == (), f"段校验没过：{written[0].issues}"
    text = written[0].path.read_text(encoding="utf-8")
    parts = split_ref_sections(text)
    assert parts["summary"].startswith("[reference generation]"), \
        "换号不能把 summary 的任务类型前缀一起洗掉（那是整片共用的）"
    assert "<Picture 2>" in parts["summary"] and "<Picture 1>" in parts["summary"], \
        f"摘要必须换成本段新号（18→2、8→1）：{parts['summary']!r}"
    assert "<Picture 18>" not in parts["summary"] and "<Picture 8>" not in parts["summary"], \
        "摘要里不能残留整片旧号"
    assert "<Picture 1>" in parts["overall_soundscape"], \
        f"环境声也要按裁剪后的版本重建：{parts['overall_soundscape']!r}"
    assert "<Picture 2>" in parts["non_diegetic_music"], \
        f"配乐也要按裁剪后的版本重建：{parts['non_diegetic_music']!r}"
    assert "<Picture 18>" not in text and "<Picture 8>" not in text, \
        "整份段文件里都不该再出现整片旧号（定义块也换过号）"
    data = json.loads((tmp_path / "GEN001" / "director" /
                       "assets-in-segment.json").read_text(encoding="utf-8"))
    assert [entry["original"] for entry in data["1"]] == [8, 18], \
        "子集记录仍是原号：段内新号只活在段词里"


def test_rerunning_one_segment_keeps_the_other_segments_byte_identical(tmp_path):
    """只重跑第 5 段：其余 9 段的记录逐字节不变（整份覆盖写会静默抹掉它们）。"""
    from minimax_h3_prompt.director import prompts

    session_dir = tmp_path / "GEN001"
    prompts.write_segment_assets(session_dir, {
        index: [{"new": 1, "original": index, "name": f"图{index}"}]
        for index in range(1, 11)
    })
    path = prompts.segment_assets_path(session_dir)
    before = path.read_text(encoding="utf-8")

    prompts.write_segment_assets(session_dir, {
        5: [{"new": 1, "original": 8, "name": "告示牌"},
            {"new": 2, "original": 18, "name": "街角环境"}],
    })
    after = path.read_text(encoding="utf-8")

    for index in range(1, 11):
        if index == 5:
            continue
        assert _segment_block(after, index) == _segment_block(before, index), \
            f"重跑第 5 段不能动第 {index} 段的记录"
    data = json.loads(after)
    assert sorted(data, key=int) == [str(index) for index in range(1, 11)], \
        "合并写不能把没在本轮出现的段号丢掉"
    assert data["5"] == [{"new": 1, "original": 8, "name": "告示牌"},
                         {"new": 2, "original": 18, "name": "街角环境"}], \
        "本轮出现的段号要被这一轮的新记录替换"


def test_writer_does_not_wipe_segments_it_did_not_write_this_run(tmp_path):
    """写段入口也走合并：本轮只写段 1、2 时，盘上已有的 3–10 段必须原样保留。"""
    from minimax_h3_prompt.director import prompts

    session_dir = tmp_path / "GEN001"
    prompts.write_segment_assets(session_dir, {
        index: [{"new": 1, "original": index, "name": f"图{index}"}]
        for index in range(3, 11)
    })
    path = prompts.segment_assets_path(session_dir)
    before = path.read_text(encoding="utf-8")

    prompts.write_director_segments(
        _plan(), llm=_StaleSummaryLLM(), session_dir=session_dir,
        sections=_STALE_SUMMARY_SECTIONS, registry=_STALE_SUMMARY_REGISTRY)

    after = path.read_text(encoding="utf-8")
    for index in range(3, 11):
        assert _segment_block(after, index) == _segment_block(before, index), \
            f"本轮没写第 {index} 段，记录不能变"
    data = json.loads(after)
    assert sorted(data, key=int) == [str(index) for index in range(1, 11)]
    assert [entry["original"] for entry in data["1"]] == [8, 18]


def test_empty_scope_table_leaves_an_existing_file_untouched(tmp_path):
    """空表不写文件（别的模式没有素材子集）；盘上已有的表也不能被清空。"""
    from minimax_h3_prompt.director import prompts

    session_dir = tmp_path / "GEN001"
    prompts.write_segment_assets(
        session_dir, {1: [{"new": 1, "original": 3, "name": "图3"}]})
    path = prompts.segment_assets_path(session_dir)
    before = path.read_text(encoding="utf-8")

    assert prompts.write_segment_assets(session_dir, {}) is None
    assert path.read_text(encoding="utf-8") == before, \
        "空表＝本次没有 r2v 段，不能把别的段留下的表清空"
