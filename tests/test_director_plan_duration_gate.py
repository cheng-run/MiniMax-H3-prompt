"""P7：4–15 秒段长闸（2026-11-02 验收问题汇总）。

之前这段契约只写在规划师提示词里，代码不校验：#137 首轮拿到 39 帧＝1.625s 的段，
打包与陪跑全盘接受。两条缝：

1. ``parse_director_plan`` 对**规划窗口**报错（不合格的规划直接作废）；
2. 窗口合格但 ``17k+5`` 吸附把落位压出带外时，只写进出站说明（不报错——
   报错会让 4s 档永远规划不出来）。
"""
from __future__ import annotations

import json

import pytest


def _reply(segments):
    return json.dumps({"segments": segments}, ensure_ascii=False)


def _seg(index, start, end, segment_type, summary="剧情", hook="落定画面"):
    return {"index": index, "start_s": start, "end_s": end,
            "type": segment_type, "summary": summary, "end_hook": hook}


def test_rejects_a_window_shorter_than_four_seconds():
    from minimax_h3_prompt.director.planning import parse_director_plan

    text = _reply([
        _seg(1, 0, 1.625, "opening"),
        _seg(2, 1.625, 6.0, "continue"),
        _seg(3, 6.0, 20.0, "ending"),
    ])
    with pytest.raises(ValueError, match="4–15 秒"):
        parse_director_plan(text, 20.0, mode="fl2v")


def test_rejects_a_window_longer_than_fifteen_seconds():
    from minimax_h3_prompt.director.planning import parse_director_plan

    text = _reply([
        _seg(1, 0, 16.0, "opening"),
        _seg(2, 16.0, 20.0, "continue"),
    ])
    with pytest.raises(ValueError, match="4–15 秒"):
        parse_director_plan(text, 20.0, mode="fl2v")


def test_legal_windows_build_a_plan_and_note_grid_snapping():
    """窗口 0–4/4–8/8–14/14–20 全合格；吸附若把某段压出带外，说明里必须点名。"""
    from minimax_h3_prompt.director.planning import parse_director_plan, render_plan_listing

    text = _reply([
        _seg(1, 0, 4.0, "opening"),
        _seg(2, 4.0, 8.0, "continue"),
        _seg(3, 8.0, 14.0, "camera_same"),
        _seg(4, 14.0, 20.0, "ending"),
    ])
    plan = parse_director_plan(text, 20.0, mode="fl2v")

    assert len(plan.segments) == 4
    for segment in plan.segments:
        seconds = segment.export_frames / 24.0
        if seconds < 4.0 - 1e-3 or seconds > 15.0 + 1e-3:
            assert any("网格吸附" in note and f"第 {segment.index} 段" in note
                       for note in plan.notes), \
                f"第 {segment.index} 段落位 {seconds:.3f}s 越界却没有出站说明"
    listing = render_plan_listing(plan)
    assert "合计导出" in listing
