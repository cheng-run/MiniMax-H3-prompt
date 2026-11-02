"""向导展示层的金样（体检 P5：拆 ``wizard.py`` 第一刀）。

``ui/wizard_display.py`` 是从 6400 行的 ``wizard.py`` 里搬出来的纯函数。搬家的
**验收判据是行为逐字不变**，所以这里钉的是几条**验收事故现场**留下的硬要求：

- 多段必须说「用 runSelection 只勾第 N 段」（全片点运行会整条重烧）；
- 导出是「全部导出」，跑完的视频含前面已有段（2026-10-31 现场贴回 226 帧）；
- 段缓存目录只供定位、不是判定依据（#112）；
- 切镜段要关「引用上段」（#137 段2 实测多出 12 帧保完整尾）；
- 单段计划不印缓存目录（插件根本不为单段写缓存）。

变异检验：删掉任何一句硬要求，对应用例红。
"""
from __future__ import annotations

from pathlib import Path

from minimax_h3_prompt.asset_loop import QueueItem
from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
from minimax_h3_prompt.ui.wizard_display import (
    render_asset_model_line,
    render_director_segment_briefing,
)


def _segment(index, segment_type, start, declared, export, guided):
    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=start + export,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=(),
    )


def _plan(*segments):
    return DirectorPlan(mode="t2v", total_frames=sum(s.export_frames for s in segments),
                        segments=tuple(segments))


def _write_prompt(tmp_path: Path, index: int, text: str) -> None:
    from minimax_h3_prompt.director.prompts import segment_path

    path = segment_path(tmp_path, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_multi_segment_briefing_keeps_the_acceptance_hard_lines(tmp_path):
    segments = (
        _segment(1, "opening", 0, 107, 107, False),
        _segment(2, "continue", 107, 141, 153, True),
    )
    plan = _plan(*segments)
    _write_prompt(tmp_path, 2, "第二段的提示词正文")

    text = render_director_segment_briefing(
        plan, segments[1], tmp_path,
        cache_directory=tmp_path / "cache", cache_name="我的缓存")

    assert "第 2/2 段" in text
    assert "runSelection **只勾第 2 段**" in text, "全片点运行会整条时间线重烧"
    assert "导出是「全部导出」" in text, "用户会以为贴回的合片是跑错了"
    assert "第二段的提示词正文" in text, "提示词正文要直接打出来，别只给路径"
    assert "只供定位，不是判定依据" in text
    assert "cache_name 也填：我的缓存" in text
    assert "成片视频路径贴回来" in text


def test_cut_segment_briefing_tells_you_to_disable_previous_clip(tmp_path):
    segments = (
        _segment(1, "opening", 0, 107, 107, False),
        _segment(2, "camera_cut", 107, 124, 136, False),
    )
    plan = _plan(*segments)

    text = render_director_segment_briefing(plan, segments[1], tmp_path)

    assert "本段是切镜段" in text and "引用上段" in text
    assert "continuityFromPrev=false" in text, "包内已关，用户手动勾回会多 12 帧"


def test_single_shot_briefing_omits_cache_lines(tmp_path):
    segments = (_segment(1, "opening", 0, 107, 107, False),)
    plan = _plan(*segments)

    text = render_director_segment_briefing(
        plan, segments[0], tmp_path, single_shot=True,
        cache_directory=tmp_path / "cache", cache_name="不该出现")

    assert "直接点运行" in text
    assert "段缓存目录" not in text, "单段计划插件不写缓存，印路径只会误导"
    assert "cache_name" not in text


def test_asset_model_line_reads_the_recorded_assignment():
    state = {"character_image_prompts": [{
        "name": "红裙女孩",
        "positive_prompt": "提示词",
        "image_model": {"model": "moss", "source": "auto", "keyword": "包装"},
    }]}
    item = QueueItem(picture=1, kind="character", name="红裙女孩")

    line = render_asset_model_line(state, item)

    assert line.startswith("[生图模型] 红裙女孩：命中『包装』")
