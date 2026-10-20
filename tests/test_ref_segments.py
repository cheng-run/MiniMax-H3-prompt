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
    assert "拼接" in card and "用户" in card  # 拼接归用户


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

    rows = read_segment_ledger(tmp_path)
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
    rows = read_segment_ledger(tmp_path)
    assert [r["segment"] for r in rows] == [1, 2]
    assert rows[0]["sha256"] != rows[1]["sha256"]


def test_segment_ledger_missing_file_is_empty_and_bad_line_raises(tmp_path):
    """文件不在＝空账；坏行**响**（追加账是「历史可查」的凭据，静默跳过等于缺页无人知）。"""
    assert read_segment_ledger(tmp_path) == []
    seg_dir = tmp_path / "segments"
    seg_dir.mkdir()
    (seg_dir / SEGMENT_LEDGER_FILENAME).write_text("{不是 JSON\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_segment_ledger(tmp_path)


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
