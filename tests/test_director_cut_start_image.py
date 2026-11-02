"""P1：切镜段的尾帧词必须从**本段首帧**写起（2026-11-02 验收问题汇总）。

文档口径：`prompts/tailframe_writer_director.md:8`「参考图就是本段的起点画面」。
引导段里「本段起点」＝上一段终点，两种取法等价；`camera_cut` 段起点在别处，
必须取用户挂在该段**首帧槽**的那张图。

缝：`ui.wizard._write_director_tailframe` 传给 `write_tailframe_prompts` 的
``start_image``——这正是写词模型看到的参考图，外部可观察（落盘文件与模型请求都从它来）。

变异检验：把 `_write_director_tailframe` 改回「只认上一段终点图」→ 第一条用例红。
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _segment(index, segment_type, start, end, declared, export, guided, give_images=()):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=end,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=give_images,
    )


def _plan():
    from minimax_h3_prompt.director.frames import DirectorPlan, give_images_for

    segments = (
        _segment(1, "opening", 0, 107, 107, 107, False,
                 give_images_for("opening", "fl2v")),
        _segment(2, "camera_cut", 107, 260, 141, 153, False,
                 give_images_for("camera_cut", "fl2v")),
        _segment(3, "ending", 260, 413, 141, 153, True,
                 give_images_for("ending", "fl2v")),
    )
    return DirectorPlan(mode="fl2v", total_frames=413, segments=segments)


def _session(tmp_path: Path):
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.session_store import SessionState

    brief = Brief(mode="director", director_mode="fl2v", duration=20.0, plot="测试")
    return SessionState(
        directory=tmp_path, brief=brief, stage_state={}, status="director_running")


def _capture(monkeypatch):
    """把写词调用截下来，只记 start_image；返回记录字典。"""
    from minimax_h3_prompt.director import tailframe

    recorded: dict = {}

    def fake_write(plan, *, llm, session_dir, start_image, identity_baseline=None):
        recorded["start_image"] = Path(start_image)
        recorded["index"] = plan.segments[0].index
        return []

    monkeypatch.setattr(tailframe, "write_tailframe_prompts", fake_write)
    return recorded


def test_camera_cut_tailframe_uses_its_own_first_image(tmp_path, monkeypatch):
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]  # camera_cut
    cut_first = tmp_path / "cut-first.png"
    cut_first.write_bytes(b"first")
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")

    asked: list[str] = []

    def fake_prompt(question: str):
        asked.append(question)
        return cut_first

    monkeypatch.setattr(wizard, "_prompt_optional_image", fake_prompt)
    monkeypatch.setattr(
        ledger, "passed_director_rows", lambda directory: {1: {"end_image": str(prev_end)}})
    recorded = _capture(monkeypatch)

    ok = wizard._write_director_tailframe(plan, segment, _session(tmp_path), llm=object())

    assert ok is True
    assert any("首帧" in q for q in asked), "切镜段没有问本段首帧路径"
    assert recorded["start_image"] == cut_first, (
        "切镜段的尾帧词参考图仍是上一段终点图，不是本段首帧")


def test_guided_segment_keeps_using_the_previous_end_image(tmp_path, monkeypatch):
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[2]  # ending：引导段，起点＝上一段终点
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")

    asked: list[str] = []

    def fake_prompt(question: str):
        asked.append(question)
        return None

    monkeypatch.setattr(wizard, "_prompt_optional_image", fake_prompt)
    monkeypatch.setattr(
        ledger, "passed_director_rows", lambda directory: {2: {"end_image": str(prev_end)}})
    recorded = _capture(monkeypatch)

    ok = wizard._write_director_tailframe(plan, segment, _session(tmp_path), llm=object())

    assert ok is True
    assert not any("首帧" in q for q in asked), "引导段不该问本段首帧"
    assert recorded["start_image"] == prev_end
