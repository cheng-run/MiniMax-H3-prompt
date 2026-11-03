"""P4/P5/P6：段简报的三处（2026-11-02 验收问题汇总）。

1. P4：切镜段要明写「挂首帧 + 关引用上段」（ComfyUI 勾上会多 12 帧、判定必挂）；
2. P5：节点性能组的 cache_name 要与包内一致（留空 → 缓存目录名对不上）；
3. P6：段提示词正文直接打在终端，不再只给文件路径。
"""
from __future__ import annotations

from pathlib import Path


def _segment(index, segment_type, guided, give_images):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=0, end_frame=153, declared_frames=141, export_frames=153,
        summary="剧情", end_hook="落定画面", give_images=give_images)


def _plan():
    from minimax_h3_prompt.director.frames import DirectorPlan

    return DirectorPlan(mode="fl2v", total_frames=306, segments=(
        _segment(1, "opening", False, ("first",)),
        _segment(2, "camera_cut", False, ("first", "end")),
        _segment(3, "ending", True, ("end",)),
    ))


def _plant_segment(directory: Path, index: int, body: str) -> None:
    path = directory / "director" / f"segment-{index:02d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_cut_segment_briefing_tells_the_user_to_disable_previous_segment_reference(tmp_path):
    from minimax_h3_prompt.ui.wizard import _render_director_segment_briefing

    plan = _plan()
    _plant_segment(tmp_path, 2, "integrated_multimodal_description: [Shot 1] …")
    text = _render_director_segment_briefing(
        plan, plan.segments[1], tmp_path,
        cache_directory=tmp_path / "cache", cache_name="ab-a")

    assert "引用上段" in text and "关掉" in text, "切镜段没有明写「关引用上段」"
    assert "cache_name 也填：ab-a" in text, "简报没有点名节点性能组的 cache_name"


def test_briefing_prints_the_segment_prompt_body(tmp_path):
    from minimax_h3_prompt.ui.wizard import _render_director_segment_briefing

    plan = _plan()
    body = "integrated_multimodal_description: [Shot 1] 暴雨夜的街道"
    _plant_segment(tmp_path, 2, body)
    text = _render_director_segment_briefing(plan, plan.segments[1], tmp_path)

    assert body in text, "段提示词正文没有打在终端"


def test_guided_segment_gets_no_disable_reference_line(tmp_path):
    from minimax_h3_prompt.ui.wizard import _render_director_segment_briefing

    plan = _plan()
    _plant_segment(tmp_path, 3, "integrated_multimodal_description: [Shot 1] 收尾")
    text = _render_director_segment_briefing(plan, plan.segments[2], tmp_path)

    assert "引用上段" not in text, "引导段不该提示「关引用上段」"


def test_r2v_cut_segment_briefing_does_not_ask_for_a_first_frame(tmp_path):
    """#145：简报的首帧提示也走给图契约——r2v 的 `camera_cut` 没有首帧槽。"""
    from minimax_h3_prompt.director.frames import DirectorPlan
    from minimax_h3_prompt.ui.wizard import _render_director_segment_briefing

    fl2v_plan = _plan()
    r2v_segments = tuple(
        _segment(segment.index, segment.type, segment.guided, ("assets",))
        for segment in fl2v_plan.segments)
    r2v_plan = DirectorPlan(mode="r2v", total_frames=306, segments=r2v_segments)
    _plant_segment(tmp_path, 2, "integrated_multimodal_description: [Shot 1] 切镜")

    text = _render_director_segment_briefing(r2v_plan, r2v_segments[1], tmp_path)

    assert "本段是切镜段" not in text, "r2v 的切镜段没有首帧槽，不该提示挂首帧"
    assert "挂**首帧**" not in text

    fl2v_text = _render_director_segment_briefing(
        fl2v_plan, fl2v_plan.segments[1], tmp_path)
    assert "挂**首帧**" in fl2v_text, "fl2v 的切镜段必须提示挂首帧"


def test_opening_segment_briefing_does_not_ask_for_a_segment_first_frame(tmp_path):
    """开场段有首帧槽，但那张是全片首帧（阶段 1 已收过），不是「段首帧」。"""
    from minimax_h3_prompt.ui.wizard import _render_director_segment_briefing

    plan = _plan()
    _plant_segment(tmp_path, 1, "integrated_multimodal_description: [Shot 1] 开场")
    text = _render_director_segment_briefing(plan, plan.segments[0], tmp_path)

    assert "挂**首帧**" not in text
    assert "本段是切镜段" not in text
