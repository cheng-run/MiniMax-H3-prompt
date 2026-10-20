"""多参分段编排测试（issue #46）：>15 秒按镜头边界拆段、逐段自包含六段式、段清单+拼接顺序卡。

接缝（spec #39 分段写作层 + 产物出口）：``ref_segments.run_ref_segmentation`` 是
>15s 多参产物的唯一出口——段文件、plan.json、拼接顺序卡都从这里落盘。全部 LLM 打桩
（历史教训：暗读真 key 的用例只在本机能过）；llm=None 走纯机械路径，行为最可钉。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimax_h3_prompt.ref_segments import (
    SEGMENT_LEDGER_FILENAME,
    format_final_film,
    log_segment_output,
    read_segment_ledger,
    render_ref_segment_briefing,
    run_ref_segmentation,
)
from minimax_h3_prompt.segment_prompts import (
    assemble_ref_segment,
    render_ref_segment_annotations,
    validate_ref_segment,
)
from minimax_h3_prompt.tools.h3_validator import split_ref_sections

# 30s 整片（>15s）：五个镜头，切点 0/5/12/18/22/30——机械打包应得 12/10/8 三段
# （不均匀硬切、边界全落切点；6+8=14 虽可合并，但贪心从左吞并后 12/10/8 已是自然结果）
_WHOLE_30S = """subject_definitions:
<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with long dark hair and a red dress.
<Subject 2> is 青铜钥匙 (the key in <Picture 2>), with an old green patina.

summary:
[reference generation] The target video shows 红裙女孩 walking into the garden and handing 青铜钥匙 over.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2], [Shot 3], [Shot 4], [Shot 5]): fully_preserved - the red dress and long dark hair are retained.
<Subject 2> (appears in [Shot 1], [Shot 2], [Shot 3], [Shot 4], [Shot 5]): fully_preserved - the green patina is retained.

detailed_description:
The target video is in a cinematic autumn style with warm soft lighting.
[Shot 1] A medium shot frames the garden path. 红裙女孩 walks in holding 青铜钥匙 in her right hand.
[Shot 2] At 00:05.000, the shot cuts to a close-up of the bronze key as she turns it over.
[Shot 3] At 00:12.000, the camera pushes in on the girl as she steps toward the boy in the blue shirt.
[Shot 4] At 00:18.000, the shot cuts to a two-shot as she holds the key out to the boy.
[Shot 5] At 00:22.000, the boy takes 青铜钥匙 and bows his head in thanks.

overall_soundscape:
Soft outdoor ambience with light footsteps continues throughout the scene.

non_diegetic_music:
N/A
"""

# 8s 整片（≤15s 档）：两个镜头，切点 0/4——直出路线**不拆段**，整片就是唯一执行段
_WHOLE_8S = """subject_definitions:
<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with long dark hair and a red dress.
<Subject 2> is 青铜钥匙 (the key in <Picture 2>), with an old green patina.

summary:
[reference generation] The target video shows 红裙女孩 turning the 青铜钥匙 over in her hand.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the red dress is retained.
<Subject 2> (appears in [Shot 1], [Shot 2]): fully_preserved - the green patina is retained.

detailed_description:
The target video is in a cinematic autumn style with warm soft lighting.
[Shot 1] A medium shot frames the garden path. 红裙女孩 walks in holding 青铜钥匙 in her right hand.
[Shot 2] At 00:04.000, the shot cuts to a close-up of the bronze key as she turns it over.

overall_soundscape:
Soft outdoor ambience with light footsteps continues throughout the scene.

non_diegetic_music:
N/A
"""

_REF_META = [(1, "红裙女孩", "人物·红裙女孩专属生图提示词"),
             (2, "青铜钥匙", "道具·青铜钥匙专属生图提示词")]


def _run(tmp_path: Path, llm=None, total_s: float = 30.0):
    return run_ref_segmentation(
        whole_prompt=_WHOLE_30S, total_s=total_s, out_dir=tmp_path,
        llm=llm, ref_meta=_REF_META)


# ---------------------------------------------------------------------------
# 1. 纯机械路径（llm=None）：确定性打包 + 定义逐段重复 + 注记/状态
# ---------------------------------------------------------------------------

def test_mechanical_segments_are_packed_at_shot_cuts(tmp_path):
    """>15s → 4–15s 整数段，不均匀硬切、边界落镜头切点（0/5/12/18/22/30 → 12/10/8）。"""
    result = _run(tmp_path, llm=None)
    windows = [(p.start_s, p.end_s) for p in result.plans]
    assert windows == [(0, 12), (12, 22), (22, 30)]
    assert result.used_fallback  # 无规划时走机械拆段，明记来源


def test_mechanical_segments_are_self_contained_six_section(tmp_path):
    """每段完整六段式：定义性段落逐段重复整片定义，注记与起止状态声明齐。"""
    result = _run(tmp_path, llm=None)
    assert len(result.texts) == 3
    whole = split_ref_sections(_WHOLE_30S)
    for position, text in enumerate(result.texts):
        sections = split_ref_sections(text)
        assert sections["subject_definitions"].strip() == whole["subject_definitions"].strip()
        assert sections["retention_analysis"].strip() == whole["retention_analysis"].strip()
        plan = result.plans[position]
        errors = [i for i in validate_ref_segment(
            text, duration=plan.duration_s, segment_number=position + 1,
            total_segments=3, start_s=plan.start_s, total_s=30, ref_meta=_REF_META)
            if i.severity == "error"]
        assert errors == [], f"段 {position + 1}: {[(i.code, i.message) for i in errors]}"


def test_mechanical_segments_rebase_timestamps_to_segment_start(tmp_path):
    """机械切片重新编号 [Shot 1] 起、时间戳归零重基——绝对片时不得进正文。"""
    result = _run(tmp_path, llm=None)
    second = split_ref_sections(result.texts[1])["detailed_description"]
    # 段 2（12-22s）含原 [Shot 3]+[Shot 4]：重编号 [Shot 1]/[Shot 2]，
    # 首镜切点时间剥离（[Shot 1] 无切点），次镜切点重基（18-12=6s）
    assert second.lstrip().startswith("Segment 2 of 3")  # 注记置顶
    assert "[Shot 1]" in second and "[Shot 3]" not in second
    assert "At 00:12.000" not in second  # 原绝对切点时间不进正文
    assert "At 00:06.000" in second  # 次镜切点重基为相对本段起点


def test_mechanical_segments_have_no_anaphora(tmp_path):
    """机械拼装也禁指代前文——禁令是产物属性，不是 LLM 专属纪律。"""
    result = _run(tmp_path, llm=None)
    for position, text in enumerate(result.texts):
        errors = [i for i in validate_ref_segment(
            text, duration=result.plans[position].duration_s,
            segment_number=position + 1, total_segments=3,
            start_s=result.plans[position].start_s, total_s=30, ref_meta=_REF_META)
            if i.code == "SEGMENT_ANAPHORA"]
        assert errors == []


# ---------------------------------------------------------------------------
# 2. 段清单 + 拼接顺序卡（段号/起止秒/先后）
# ---------------------------------------------------------------------------

def test_order_card_lists_segments_in_concatenation_order(tmp_path):
    result = _run(tmp_path, llm=None)
    card = result.order_card_path.read_text(encoding="utf-8")
    assert "拼接顺序卡" in card
    # 段号 / 起止秒 / 先后 三样都在，且按 1→N 的先后列出提示词文件
    for header in ("段号", "起止秒", "先后"):
        assert header in card
    for position, plan in enumerate(result.plans, 1):
        assert f"{plan.start_s}s-{plan.end_s}s" in card
        assert f"segment-{position:02d}.md" in card
    assert card.index("segment-01.md") < card.index("segment-02.md") < card.index("segment-03.md")
    # 拼接归谁（issue #57 反转 #39 ID13）：归**系统**，并指出重跑某段后的手动出口
    assert "拼接归系统" in card
    assert "ref-concat" in card, "顺序卡要指出重跑某段后的手动拼接命令"


def test_report_says_concat_belongs_to_system(tmp_path):
    """交付报告不再说「拼接归用户」（#39 ID13 已被取代）。"""
    from minimax_h3_prompt.ref_segments import format_ref_segment_report

    report = format_ref_segment_report(_run(tmp_path, llm=None))
    assert "拼接归系统" in report
    assert "拼接归用户" not in report


def test_segment_files_and_plan_are_written(tmp_path):
    result = _run(tmp_path, llm=None)
    for position in (1, 2, 3):
        assert (tmp_path / "segments" / f"segment-{position:02d}.md").is_file()
    plan_data = json.loads((tmp_path / "segments" / "plan.json").read_text(encoding="utf-8"))
    assert len(plan_data) == 3
    assert plan_data[0]["start_s"] == 0 and plan_data[-1]["end_s"] == 30


# ---------------------------------------------------------------------------
# 3. 规划式（v2）路径：规划成功 → 逐段细写；产出同过自包含六段式校验
# ---------------------------------------------------------------------------

_PLAN_JSON = json.dumps({"segments": [
    {"index": 0, "start_s": 0, "end_s": 12, "shots_in_segment": [1, 2],
     "summary": "女孩进花园端详钥匙", "start_state": "The garden path is empty.",
     "end_hook": "The girl has just turned the bronze key over in her hand."},
    {"index": 1, "start_s": 12, "end_s": 22, "shots_in_segment": [3, 4],
     "summary": "她走向少年递出钥匙", "start_state": "The girl holds the key and steps toward the boy.",
     "end_hook": "The girl holds the bronze key out to the boy in a two-shot."},
    {"index": 2, "start_s": 22, "end_s": 30, "shots_in_segment": [5],
     "summary": "少年接钥匙", "start_state": "The boy reaches out for the bronze key.",
     "end_hook": "The bronze key has just been handed over to the boy."},
]})


def _stub_segment_doc() -> str:
    """写段打桩的正样本：合法六段式（注记/状态由组装器注入的形态）。"""
    return assemble_ref_segment(
        subject_definitions="PLACEHOLDER",  # 组装/归一会用整片定义覆盖
        retention_analysis="PLACEHOLDER",
        task_type="reference generation",
        summary_body="The girl in the red dress hands the bronze key over.",
        detailed_body=(
            "The target video is in a cinematic autumn style with warm soft lighting.\n"
            "[Shot 1] A medium shot frames the garden path. 红裙女孩 walks in holding 青铜钥匙.\n"
            "[Shot 2] At 00:03.000, the shot cuts to a close-up as she hands 青铜钥匙 to the boy."),
        overall_soundscape="Soft outdoor ambience with light footsteps continues.",
        non_diegetic_music="N/A",
        annotations=render_ref_segment_annotations(
            1, 1, 0, 7, 7, "the girl stands on the path", "the key is handed over"),
    )


class _V2LLM:
    """规划成功 + 写段成功的打桩：回复按调用序分流。"""

    def __init__(self):
        self.requests: list[str] = []

    def invoke(self, request):
        self.requests.append(request)
        if "分段规划师" in request:
            return _PLAN_JSON
        return _stub_segment_doc()


def test_v2_path_writes_planned_segments(tmp_path):
    result = _run(tmp_path, llm=_V2LLM())
    assert not result.used_fallback
    assert [(p.start_s, p.end_s) for p in result.plans] == [(0, 12), (12, 22), (22, 30)]
    for position, (plan, text) in enumerate(zip(result.plans, result.texts), 1):
        sections = split_ref_sections(text)
        assert f"Segment {position} of 3" in sections["detailed_description"]
        errors = [i for i in validate_ref_segment(
            text, duration=plan.duration_s, segment_number=position,
            total_segments=3, start_s=plan.start_s, total_s=30, ref_meta=_REF_META)
            if i.severity == "error"]
        assert errors == [], f"段 {position}: {[(i.code, i.message) for i in errors]}"


class _FallbackLLM:
    """规划失败（胡乱回复）→ 回退式重写成功：验「规划一失败不掉档次」。"""

    def __init__(self):
        self.requests: list[str] = []

    def invoke(self, request):
        self.requests.append(request)
        if "分段规划师" in request:
            return "not json at all"
        return _stub_segment_doc()


def test_fallback_path_still_delivers_self_contained_segments(tmp_path):
    result = _run(tmp_path, llm=_FallbackLLM())
    assert result.used_fallback
    assert [(p.start_s, p.end_s) for p in result.plans] == [(0, 12), (12, 22), (22, 30)]
    for position, (plan, text) in enumerate(zip(result.plans, result.texts), 1):
        errors = [i for i in validate_ref_segment(
            text, duration=plan.duration_s, segment_number=position,
            total_segments=3, start_s=plan.start_s, total_s=30, ref_meta=_REF_META)
            if i.severity == "error"]
        assert errors == [], f"段 {position}: {[(i.code, i.message) for i in errors]}"


def test_short_film_is_not_segmented_at_source(tmp_path):
    """>15s 才拆是入口守卫的事：编排器面对 ≤15s 也不会硬拆——行为定死。"""
    result = run_ref_segmentation(
        whole_prompt=_WHOLE_30S, total_s=12.0, out_dir=tmp_path,
        llm=None, ref_meta=_REF_META)
    assert [(p.start_s, p.end_s) for p in result.plans] == [(0, 12)]
    assert (tmp_path / "segments" / "segment-01.md").is_file()


# ---------------------------------------------------------------------------
# 4. 对抗验证修复钉子（B1/B2/C3：内容不静默丢失、状态句无绝对片时、空块不退化）
# ---------------------------------------------------------------------------

# 5–28s 一个跨窗长镜（镜内节拍 18/24s），机械拆窗后长镜内容必须进所有重叠窗
_WHOLE_LONG_SHOT = """subject_definitions:
<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with long dark hair and a red dress.

summary:
[reference generation] The target video shows the girl driving a truck across the yard.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2], [Shot 3]): fully_preserved - the red dress is retained.

detailed_description:
The target video is in a cinematic style.
[Shot 1] A wide shot frames the yard as the truck rolls out of the bay.
[Shot 2] At 00:05.000, the camera follows the truck across the yard. At 00:18.000, the girl waves from the window. At 00:24.000, the truck slows down.
[Shot 3] At 00:28.000, she exits the cab and closes the door.

overall_soundscape:
Engine rumble continues.

non_diegetic_music:
N/A
"""


def test_long_shot_content_reaches_every_overlapping_window(tmp_path):
    """跨窗长镜不许静默丢内容（对抗验证 B1）：5–28s 的块重叠进 (5,17) 与 (17,30) 两窗。

    按块首时间归属会让 17–28s 的内容在后窗凭空消失——机械切片宁可重复、不可缺肉。
    """
    result = run_ref_segmentation(
        whole_prompt=_WHOLE_LONG_SHOT, total_s=30.0, out_dir=tmp_path,
        llm=None, ref_meta=[(1, "红裙女孩", "…")])
    assert [(p.start_s, p.end_s) for p in result.plans] == [(0, 5), (5, 17), (17, 30)]
    later = split_ref_sections(result.texts[2])["detailed_description"]
    assert "waves from the window" in later  # 长镜中段内容进后窗（此前被静默丢弃）
    assert "exits the cab" in later
    # 段号用真实镜头号（合成 1..n 会让 plan.json 撒谎）
    assert result.plans[2].shots_in_segment == (2, 3)
    # 状态句不得携带绝对片时（At 时间戳从状态句剥净）
    for text in result.texts:
        assert "begins with At" not in text and "ends with At" not in text


def test_no_shot_blocks_edge_keeps_annotations_non_degenerate(tmp_path):
    """整片 detailed 解析不出镜头块（畸形输入）：状态声明不退化成「begins with .」，
    缺段问题由校验器照报、当面打出（对抗验证 C3/B2 的边界钉子）。"""
    malformed = _WHOLE_30S.replace("[Shot 1]", "[Seg A]").replace(
        "[Shot 2]", "[Seg B]").replace("[Shot 3]", "[Seg C]").replace(
        "[Shot 4]", "[Seg D]").replace("[Shot 5]", "[Seg E]")
    result = run_ref_segmentation(
        whole_prompt=malformed, total_s=20.0, out_dir=tmp_path,
        llm=None, ref_meta=_REF_META)
    assert result.texts, "畸形输入也要出产物（问题在 issues 里当面报）"
    for text in result.texts:
        assert "begins with ." not in text and "ends with ." not in text
        assert "This segment begins with the scene at" in text  # 兜底状态句非空
    assert any(getattr(i, "severity", "") == "error"
               for issues in result.issues for i in issues), "缺段必须报出来"


# ---------------------------------------------------------------------------
# 4. 段产出账与逐段展示块（issue #55）
# ---------------------------------------------------------------------------

def test_result_carries_the_delivery_total_for_revalidation(tmp_path):
    """结果带着**交付时用的**全片时长（复审采纳项，issue #56）。

    注记句「of the full Ns video」与交付校验共用这个数；规划器允许末段 end 略超 total
    （留余量），所以 ``plans[-1].end_s`` 不是它的替代品——逐段陪跑里改写后的重过校验
    若拿末段 end 当 total，就会打出假的「缺注记」。
    """
    result = _run(tmp_path)
    assert result.total_s == 30
    assert result.total_s == int(round(30.0))


def test_segment_ledger_row_carries_path_sha_and_verdict(tmp_path):
    """一段一行：段号 / 起止秒 / 视频路径 / SHA256 / 可证层判定 / 时间（issue #55 AC5）。

    SHA256 是为了对抗**文件名可变引用**——只记文件名的话，素材换版/改名后旧产物
    永久不可复测且无处报错（素材台账同款理由）。
    """
    import hashlib

    video = tmp_path / "out" / "seg-01.mp4"
    video.parent.mkdir()
    video.write_bytes(b"segment-one-video")

    path = log_segment_output(
        tmp_path, segment=1, start_s=0, end_s=12, video_path=video)
    assert path == tmp_path / "segments" / SEGMENT_LEDGER_FILENAME

    rows = read_segment_ledger(tmp_path, event=None)
    assert len(rows) == 1
    row = rows[0]
    assert row["segment"] == 1 and row["start_s"] == 0 and row["end_s"] == 12
    assert row["video"] == str(video.resolve())
    assert row["sha256"] == hashlib.sha256(b"segment-one-video").hexdigest()
    assert row["verdict"] == "pass"
    assert row["at"]


def test_segment_ledger_appends_one_row_per_segment(tmp_path):
    """追加式：第二段不改第一段那一行（历史可查，同资产轮次账的纪律）。"""
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=12, video_path=first)
    log_segment_output(tmp_path, segment=2, start_s=12, end_s=20, video_path=second)
    rows = read_segment_ledger(tmp_path, event=None)
    assert [r["segment"] for r in rows] == [1, 2]
    assert rows[0]["sha256"] != rows[1]["sha256"]


def test_segment_ledger_missing_file_is_empty_and_bad_line_raises(tmp_path):
    """文件不在＝空账；坏行**响**（追加账是「历史可查」的凭据，静默跳过等于缺页无人知）。"""
    assert read_segment_ledger(tmp_path, event=None) == []
    seg_dir = tmp_path / "segments"
    seg_dir.mkdir()
    (seg_dir / SEGMENT_LEDGER_FILENAME).write_text("{不是 JSON\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_segment_ledger(tmp_path, event=None)


def test_segment_briefing_carries_window_and_duration(tmp_path):
    """展示块含段号 / 起止秒 / 本段时长 / 提示词文件（issue #55 AC1）。"""
    from minimax_h3_prompt.segment_planner import SegmentPlan

    plan = SegmentPlan(index=1, start_s=12, end_s=22, shots_in_segment=(3, 4),
                       summary="她走向少年递出钥匙", end_hook="钥匙递出")
    block = render_ref_segment_briefing(
        plan, 2, 3, tmp_path / "segments" / "segment-02.md")
    assert "第 2/3 段" in block
    assert "起止 12s-22s" in block and "本段时长 10s" in block
    assert "segment-02.md" in block


# ---------------------------------------------------------------------------
# 5. 语义层遭遇与改写轮次（issue #56 AC6/AC8）：同一本段产出账，按 event 判别
# ---------------------------------------------------------------------------

def test_segment_ledger_carries_encounter_and_rewrite_rows(tmp_path):
    """一本账三类行（event=output/encounter/rewrite），互不覆盖、可各自取数。

    AC8 要的是「每次遭遇（段号/判定/出路/尝试次数）记入段产出账」、AC6 要的是
    「每轮改写落一行（含改写前后全文）」——两样都不是通过行，所以账上必须有判别
    字段：``#58`` 的续跑要按 ``event=output`` 取「哪几段过了」，不判别就会把
    「改了一轮词」当成「这段过了」。
    """
    from minimax_h3_prompt.ref_segments import (
        SEGMENT_EVENT_ENCOUNTER,
        SEGMENT_EVENT_OUTPUT,
        SEGMENT_EVENT_REWRITE,
        log_segment_encounter,
        log_segment_rewrite_round,
    )

    video = tmp_path / "seg-01.mp4"
    video.write_bytes(b"segment-one-video")
    log_segment_output(tmp_path, segment=1, start_s=0, end_s=12, video_path=video)
    log_segment_encounter(
        tmp_path, segment=1, verdict="not_reached", outcome="rewrite_prompt", attempt=2,
        first_frame=tmp_path / "segments" / "frames" / "segment-01-first.png",
        last_frame=tmp_path / "segments" / "frames" / "segment-01-last.png")
    log_segment_rewrite_round(
        tmp_path, segment=1, feedback="末态应该是罐子已经放下",
        prompt_before="OLD BODY", prompt_after="NEW BODY")

    rows = read_segment_ledger(tmp_path, event=None)
    assert [r["event"] for r in rows] == [
        SEGMENT_EVENT_OUTPUT, SEGMENT_EVENT_ENCOUNTER, SEGMENT_EVENT_REWRITE]

    encounter = rows[1]
    assert (encounter["segment"], encounter["verdict"], encounter["outcome"],
            encounter["attempt"]) == (1, "not_reached", "rewrite_prompt", 2)
    # 帧图路径同 gate-log 的理由：它是「事后复跑那次判定」的唯一入口
    assert encounter["first_frame"].endswith("segment-01-first.png")
    assert encounter["last_frame"].endswith("segment-01-last.png")
    assert encounter["at"]

    rewrite = rows[2]
    assert rewrite["round"] == 1
    assert rewrite["feedback"] == "末态应该是罐子已经放下"
    assert rewrite["prompt_before"] == "OLD BODY" and rewrite["prompt_after"] == "NEW BODY"
    assert rewrite["at"]

    # 取数口：按 event 过滤（#58 续跑据此认「哪几段过了」）
    assert [r["segment"] for r in read_segment_ledger(tmp_path, event=SEGMENT_EVENT_OUTPUT)] == [1]
    assert [r["segment"] for r in read_segment_ledger(tmp_path, event=SEGMENT_EVENT_REWRITE)] == [1]
    assert read_segment_ledger(tmp_path, event="nope") == []


def test_segment_ledger_round_index_continues_across_calls(tmp_path):
    """改写轮次不设硬上限，序号按该段已有轮次自动续（AC6；跨调用不靠内存计数）。"""
    from minimax_h3_prompt.ref_segments import log_segment_rewrite_round

    for feedback in ("先改这里", "再改那里"):
        log_segment_rewrite_round(tmp_path, segment=2, feedback=feedback,
                                  prompt_before="A", prompt_after="B")
    from minimax_h3_prompt.ref_segments import SEGMENT_EVENT_REWRITE

    rounds = [r["round"] for r in read_segment_ledger(tmp_path, event=SEGMENT_EVENT_REWRITE)]
    assert rounds == [1, 2]


def test_segment_encounter_never_touches_the_bridge_gate_log(tmp_path):
    """AC8 后半：遭遇进段产出账，**不写**首尾帧链的 gate-log.jsonl。

    两本账混在一起，将来给自动判据取阈值时就分不开「多参的语义人裁」与「桥接链的
    锚帧人裁」——判定物、参照物、出路集都不是同一套。
    """
    from minimax_h3_prompt.ref_segments import log_segment_encounter

    log_segment_encounter(tmp_path, segment=1, verdict="reached", outcome="continue",
                          attempt=1, first_frame=None, last_frame=None)
    assert not (tmp_path / "segments" / "gate-log.jsonl").exists()
    assert (tmp_path / "segments" / SEGMENT_LEDGER_FILENAME).is_file()


# ---------------------------------------------------------------------------
# 6. 语义层核验块（issue #56 AC1）：声明与首/末帧实际画面**并排**
# ---------------------------------------------------------------------------

def _semantic_plan():
    from minimax_h3_prompt.segment_planner import SegmentPlan

    return SegmentPlan(
        index=0, start_s=0, end_s=10, shots_in_segment=(1, 2),
        summary="女孩把罐子放下", start_state="the jar is held in her right hand",
        end_hook="the jar rests on the wooden table")


def test_semantic_check_puts_each_declaration_beside_its_frame_reading(tmp_path):
    """两端的「声明 → 实际画面」各自并排（AC1）。

    这一层存在的理由与桥接链的锚帧语义核验同源：证据曾经没在决策时刻被摆到一起——
    ``start_state``/``end_hook`` 只在展示提示词时打印（比视频交回早十几分钟），剥帧
    当轮只打印读图结果，人得在两个时刻各记一半再凭记忆判。所以判据是**相邻且有序**，
    不是「都出现了」。
    """
    from minimax_h3_prompt.ref_segments import render_ref_segment_semantic_check

    first = tmp_path / "segments" / "frames" / "segment-01-first.png"
    last = tmp_path / "segments" / "frames" / "segment-01-last.png"
    block = render_ref_segment_semantic_check(
        plan=_semantic_plan(), position=1, total=2,
        first_description="画面里女孩右手举着陶罐", last_description="陶罐已落在木桌上",
        first_frame=first, last_frame=last)

    assert "第 1/2 段" in block
    assert "the jar is held in her right hand" in block
    assert "the jar rests on the wooden table" in block
    assert "画面里女孩右手举着陶罐" in block
    assert "陶罐已落在木桌上" in block
    assert str(first) in block and str(last) in block

    # 并排：每端的读图结果紧跟它自己的声明（声明在上、实际画面在下）
    assert block.index("the jar is held in her right hand") < block.index("画面里女孩右手举着陶罐")
    assert block.index("画面里女孩右手举着陶罐") < block.index("the jar rests on the wooden table")
    assert block.index("the jar rests on the wooden table") < block.index("陶罐已落在木桌上")


def test_semantic_check_says_so_when_a_reading_is_missing(tmp_path):
    """读图失败/没取到帧时**说破**，不静默留白（否则「并排」缺了一半没人知道）。

    缺了照样摆出来给人判：读图失败是「量不出来」，不是「画面不合格」——不许拿它
    当自动判据，也不许因此跳过这一层。
    """
    from minimax_h3_prompt.ref_segments import render_ref_segment_semantic_check

    block = render_ref_segment_semantic_check(
        plan=_semantic_plan(), position=1, total=2,
        first_description=None, last_description="陶罐已落在木桌上",
        first_frame=None, last_frame=tmp_path / "last.png")
    assert "读图失败" in block
    assert "未取得" in block
    # 声明仍在（对照物不能因为读图失败就消失）
    assert "the jar is held in her right hand" in block
    assert "the jar rests on the wooden table" in block


# ---------------------------------------------------------------------------
# 7. ≤15 秒：整片六段式即唯一执行段（issue #57 AC5）
# ---------------------------------------------------------------------------

def test_single_segment_result_keeps_whole_prompt_as_the_only_segment(tmp_path):
    """≤15 秒**不拆段**：整片六段式原样就是本片唯一执行段的文本，收尾照走同一套。

    「触发口径仍按总时长分流」在这里的落法：直出路线不套分段注记（注记是给「段与全片
    对位」用的，段数=1 时它无从对位），所以 ``annotated=False``——校验与改写据此走
    ``validate_ref`` 口径，而不是拿分段校验器去报两条结构性必挂的假错。
    """
    from minimax_h3_prompt.ref_segments import single_segment_result

    result = single_segment_result(
        whole_prompt=_WHOLE_8S, total_s=8.0, out_dir=tmp_path, ref_meta=_REF_META)

    assert len(result.plans) == 1
    plan = result.plans[0]
    assert (plan.start_s, plan.end_s, plan.duration_s) == (0, 8, 8)
    assert plan.shots_in_segment == (1, 2)
    # 起止状态取自整片自己的首/末镜句——语义层拿它当对照物（派生自正文，不是另写一份）
    assert plan.start_state.startswith("A medium shot frames the garden path")
    assert plan.end_hook.startswith("the shot cuts to a close-up of the bronze key")
    assert result.texts == [_WHOLE_8S]
    assert result.total_s == 8
    assert result.annotated is False

    # 段文件/plan.json/顺序卡照落（收尾与手动拼接命令读的是同一套产物）
    assert (tmp_path / "segments" / "segment-01.md").read_text(encoding="utf-8") == _WHOLE_8S
    plan_data = json.loads((tmp_path / "segments" / "plan.json").read_text(encoding="utf-8"))
    assert [(p["start_s"], p["end_s"]) for p in plan_data] == [(0, 8)]
    assert (tmp_path / "segments" / "order-card.md").is_file()

    # 直出文本不带注记 → 不许拿分段校验器的两条注记判据去报它
    codes = [i.code for i in result.issues[0]]
    assert not [c for c in codes if c.startswith("SEGMENT_")], codes


# ---------------------------------------------------------------------------
# 8. 成片拼接（issue #57）：全段通过才拼、按 plan 的段序、段数=1 退化为拷贝改名
# ---------------------------------------------------------------------------

def _plan_of(index: int, start: int, end: int):
    from minimax_h3_prompt.segment_planner import SegmentPlan

    return SegmentPlan(index=index, start_s=start, end_s=end,
                       shots_in_segment=(index + 1,), summary=f"seg{index + 1}",
                       end_hook="the scene at the segment's end",
                       start_state="the scene at the segment's start")


def _log(tmp_path: Path, segment: int, start: int, end: int, video: Path) -> None:
    """把一段记进段产出账（真走 ``log_segment_output``：成片读的就是这本账）。"""
    log_segment_output(tmp_path, segment=segment, start_s=start, end_s=end,
                       video_path=video)


def test_passed_segment_rows_takes_only_output_rows(tmp_path, tmp_video):
    """一本账三类行，取「哪几段过了」必须按 event 过滤（encounter/rewrite 不是通过）。"""
    from minimax_h3_prompt.ref_segments import (
        log_segment_encounter,
        log_segment_rewrite_round,
        passed_segment_rows,
    )

    video = tmp_video("s1.mp4", frames=48)
    _log(tmp_path, 1, 0, 2, video)
    log_segment_encounter(tmp_path, segment=2, verdict="not_reached",
                          outcome="rerun", attempt=1)
    log_segment_rewrite_round(tmp_path, segment=2, feedback="改末态",
                              prompt_before="a", prompt_after="b")

    rows = passed_segment_rows(tmp_path)
    assert sorted(rows) == [1]
    assert rows[1]["video"] == str(video.resolve())


def test_assemble_final_film_refuses_when_a_segment_did_not_pass(tmp_path, tmp_video):
    """缺一段**不产出成片**（AC3）：缺段的成片时长对不上总时长，是伪产物。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    first = tmp_video("s1.mp4", frames=48)
    third = tmp_video("s3.mp4", frames=48)
    _log(tmp_path, 1, 0, 2, first)
    _log(tmp_path, 3, 4, 6, third)
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4), _plan_of(2, 4, 6)]

    film = assemble_final_film(directory=tmp_path, plans=plans, total_s=6,
                               rows=passed_segment_rows(tmp_path))

    assert film.path is None
    assert not (tmp_path / "final.mp4").exists()
    assert "第 2 段" in film.reason, film.reason
    assert "未产出成片" in format_final_film(film)


def test_assemble_final_film_concats_in_plan_order_and_covers_the_total(tmp_path, tmp_video):
    """成片按 plan 的段序接（不是交回顺序），帧数＝各段之和、时长＝起步总时长。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows
    from minimax_h3_prompt.tools.frame_match import mad, read_frames_gray

    # 两段各 2.0s/3.0s 的**整时长**素材：拼接不丢帧时成片恰为 5.0s＝起步总时长。
    # 段 2 的方块起手位置挪开（offset），段序错了两段的接缝帧就对不上。
    first = tmp_video("s1.mp4", frames=48, width=400)
    second = tmp_video("s2.mp4", frames=72, width=400, offset=60)
    _log(tmp_path, 1, 0, 2, first)
    _log(tmp_path, 2, 2, 5, second)
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5)]

    film = assemble_final_film(directory=tmp_path, plans=plans, total_s=5,
                               rows=passed_segment_rows(tmp_path))

    assert film.path == tmp_path / "final.mp4"
    assert (film.total_s, film.planned_s) == (5, 5)
    assert film.warnings == []
    frames = read_frames_gray(film.path)
    assert len(frames) == 48 + 72, "拼接不许丢帧/加帧"
    assert round(film.measured_s, 3) == 5.0, "成片时长＝起步输入的总时长"
    # 段序：第 1 帧是段 1 的开场、第 48 帧是段 2 的开场（段间硬切、用各段自身帧）
    assert mad(frames[0], read_frames_gray(first)[0]) < 1.0
    assert mad(frames[48], read_frames_gray(second)[0]) < 1.0


def test_assemble_final_film_degrades_to_copy_for_a_single_segment(tmp_path, tmp_video):
    """段数=1（≤15 秒直出）：拼接退化为拷贝改名，不劳 ffmpeg 一趟。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    only = tmp_video("s1.mp4", frames=120)
    _log(tmp_path, 1, 0, 5, only)

    film = assemble_final_film(directory=tmp_path, plans=[_plan_of(0, 0, 5)],
                               total_s=5, rows=passed_segment_rows(tmp_path))

    assert film.path == tmp_path / "final.mp4"
    assert film.how == "copy"
    assert film.path.read_bytes() == only.read_bytes(), "拷贝改名：内容一字不差"
    assert round(film.measured_s, 3) == 5.0


def test_assemble_final_film_refuses_when_a_recorded_video_changed(tmp_path, tmp_video):
    """账上记的 SHA256 与磁盘上的文件对不上＝换版/改名：不许把没验收过的视频拼进成片。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    first = tmp_video("s1.mp4", frames=48)
    _log(tmp_path, 1, 0, 2, first)
    # 同一路径换版（重编码/覆盖）——文件名没变，内容变了
    tmp_video("s1.mp4", frames=48, offset=60)

    film = assemble_final_film(directory=tmp_path, plans=[_plan_of(0, 0, 2)],
                               total_s=2, rows=passed_segment_rows(tmp_path))

    assert film.path is None
    assert "第 1 段" in film.reason and "SHA256" in film.reason, film.reason


def test_assemble_final_film_says_so_when_a_recorded_video_is_gone(tmp_path, tmp_video):
    """文件不在了（改名/搬走）也要当场说破，不许静默少拼一段。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    first = tmp_video("s1.mp4", frames=48)
    _log(tmp_path, 1, 0, 2, first)
    first.unlink()

    film = assemble_final_film(directory=tmp_path, plans=[_plan_of(0, 0, 2)],
                               total_s=2, rows=passed_segment_rows(tmp_path))
    assert film.path is None
    assert "第 1 段" in film.reason and "读不到" in film.reason, film.reason


def test_assemble_final_film_accepts_a_last_segment_that_overshoots_the_total(tmp_path, tmp_video):
    """末段 end 略超起步总时长是**合法**产物（规划器留余量）——不许判成「不是一套」而拒绝出片。

    规划器契约（``segment_planner.parse_segment_plan``）：「最后一段 end 可以略超 total
    （留余量）但不许欠」。拿「各段之和 ＝ total」当判据，末段一留余量就会把**全段通过**的
    片子拒绝掉，理由还是「段表与本次生成不是一套」——同一份契约两处口径（issue #56 的
    假 SEGMENT_TIMELINE_MISSING 就是这个形态）。
    """
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    first = tmp_video("s1.mp4", frames=48)     # 2.0s
    second = tmp_video("s2.mp4", frames=72)    # 3.0s
    _log(tmp_path, 1, 0, 2, first)
    _log(tmp_path, 2, 2, 5, second)
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5)]

    film = assemble_final_film(directory=tmp_path, plans=plans, total_s=4,   # 起步 4s，段表到 5s
                               rows=passed_segment_rows(tmp_path))

    assert film.path is not None, film.reason
    assert (film.total_s, film.planned_s) == (4, 5)
    assert "各段计划时长之和 5s，末段留余量" in format_final_film(film)


def test_assemble_final_film_refuses_a_segment_table_with_a_gap(tmp_path, tmp_video):
    """段表有缺口/欠总时长（手改过 plan.json）→ 不产出成片并说清哪儿不对。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    _log(tmp_path, 1, 0, 2, first)
    _log(tmp_path, 2, 3, 5, second)     # 2-3s 缺口
    plans = [_plan_of(0, 0, 2), _plan_of(1, 3, 5)]

    film = assemble_final_film(directory=tmp_path, plans=plans, total_s=5,
                               rows=passed_segment_rows(tmp_path))
    assert film.path is None
    assert "缺口/重叠" in film.reason and "0-2s 之后是 3-5s" in film.reason, film.reason
