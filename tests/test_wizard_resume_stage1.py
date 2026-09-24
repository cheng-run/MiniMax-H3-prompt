"""阶段 1 中断续接（_resume_stage1）的收尾完整性。

bug（2026-09-24）：``_resume_stage1`` 恢复完成只落盘 + 打「阶段 1 已恢复完成」，
从不调 ``_show_frame_prompts``——新跑路径（``_phase1_new``）进阶段 2 前会展示生图
提示词并给修改循环，续接路径直接掉进阶段 2 的「首帧图片路径」提问。用户没有提示词
就无法生成关键帧图，也就没有路径可提交（实测：I2VA 会话断在 fl2va_frame_prompts
前，续跑完成后拿不到任何提示词）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.ui import wizard


class _StopPhase2(Exception):
    """阶段 2 的重活在测试里切成哨兵异常——本文件只关心进阶段 2 前的展示。"""


def _brief() -> Brief:
    return Brief(mode="base", variant="I2VA", duration=30.0, style="写实电影感",
                 plot="一队冒险者在远古森林探险的途中发现了一只正在睡觉的巨龙")


def _interrupted_gen(tmp_path) -> object:
    """造一个「阶段 1 断在 fl2va_frame_prompts 之前」的 GEN001。"""
    gen = tmp_path / "某主题" / "GEN001"
    bundle = {
        "scene_anchor": "远古森林深处",
        "first": [{
            "frame": "first", "model_family": "zimage",
            "positive_prompt": "远古森林深处，一队冒险者围着一只正在睡觉的巨龙。",
            "scene_anchor": "远古森林深处",
            "time_seconds": 0.0,
        }],
        "last": [],
        "continuity_constraints": ["同一组人物、同一套服装"],
    }
    session_store.save_session(
        gen, _brief(),
        {
            "shot_table": "[Shot 1] ……",
            "fl2va_prompt_bundle": bundle,
            # 断点：阶段 1 链上 fl2va_frame_prompts 的前一个节点（本次实测的现场）
            "_progress": {"last_completed_node": "parallel_visual"},
        },
        status=session_store.STATUS_STAGE1_RUNNING,
    )
    return session_store.load_session(gen)


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _stop_phase2(*args, **kwargs):
    raise _StopPhase2()


def test_resume_stage1_shows_frame_prompts(tmp_path, monkeypatch, capsys):
    """续接恢复完成必须展示生图提示词——没有它用户无法进入阶段 2 的图片提交。"""
    session = _interrupted_gen(tmp_path)
    _silence(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())

    # fl2va_frame_prompts 及之后的节点不再真跑：断点在它前面，但本测试的题目是收尾展示
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda *a, **k: (dict(session.stage_state), SimpleNamespace(), {}),
    )
    captured: dict = {}
    real_show = wizard._show_frame_prompts

    def spy_show(state, generation_dir):
        captured["called"] = True
        captured["has_bundle"] = isinstance(state.get("fl2va_prompt_bundle"), dict)
        real_show(state, generation_dir)

    monkeypatch.setattr(wizard, "_show_frame_prompts", spy_show)

    resumed = wizard._resume_stage1(SimpleNamespace(), session)

    assert resumed is not None
    assert captured.get("called"), "续接完成没有展示生图提示词（_show_frame_prompts 未被调用）"
    assert captured.get("has_bundle"), "展示时 state 里没有 fl2va_prompt_bundle"
    out = capsys.readouterr().out
    assert "远古森林深处" in out, "输出里没有首帧提示词正文"
    assert "I2VA 首帧生图提示词" in out


def test_phase2_shows_frame_prompts_when_session_is_already_awaiting(tmp_path, monkeypatch, capsys):
    """阶段 1 已完成、会话处于 awaiting_frames 时重启向导：进阶段 2 前仍要能看到提示词。

    否则用户的处境是：向导跳过阶段 1 续接、直接问「首帧图片路径」，而提示词从没露过面。
    """
    session = _interrupted_gen(tmp_path)
    _silence(monkeypatch)
    # 模拟「续接跑完阶段 1 后落盘」的现场：状态已是 awaiting_frames
    session_store.save_session(
        session.directory, session.brief, session.stage_state,
        status=session_store.STATUS_AWAITING_FRAMES,
    )
    session = session_store.load_session(session.directory)

    # 阶段 2 的图片收集与 LLM 组装在测试里短路（本测试只关心进阶段 2 前的展示）
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "run_stage2", _stop_phase2)

    with pytest.raises(_StopPhase2):
        wizard._phase2_collect_and_finish(SimpleNamespace(), session)

    out = capsys.readouterr().out
    assert "I2VA 首帧生图提示词" in out, "进阶段 2 前没有展示生图提示词"
    assert "远古森林深处" in out


def test_fresh_phase1_does_not_double_print_prompts(tmp_path, monkeypatch, capsys):
    """新跑路径已在阶段 1 收尾展示过提示词，进阶段 2 不该再刷一遍同样的正文。"""
    session = _interrupted_gen(tmp_path)
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "run_stage2", _stop_phase2)

    with pytest.raises(_StopPhase2):
        wizard._phase2_collect_and_finish(SimpleNamespace(), session, show_frame_prompts=False)

    out = capsys.readouterr().out
    assert "I2VA 首帧生图提示词" not in out
