"""向导的 base 短视频分支由步骤机驱动（D2-AC16/AC17 / issue #216）。

断言落在**用户可见行为**上：走哪条执行器、按什么顺序、阶段 2 要不要重展提示词、
产出哪些文件。步骤机自身的声明在 ``test_step_machine.py`` 里验。

既有向导测试一行没改——它们全绿本身就是「行为不变」的主证据；本文件补的是
「新那条驱动线真的接着、而别的路线真的没被带进去」这层接线。
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import session_store
from minimax_h3_prompt import step_machine
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import load_session, save_session
from minimax_h3_prompt.ui import wizard, wizard_base

TOPIC = "秋日庭院里的橘猫"
FRAME_NODE = "fl2va_frame_prompts"


def _cfg(root, **overrides):
    base = dict(
        default_duration=5.0, default_language="Chinese",
        sessions_root=Path(root), max_clarification_rounds=0,
        common_sense_qa=False, max_qa_iterations=1, roundtable_max_rounds=1,
        vision_model=SimpleNamespace(model="stub-vision"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _brief(*, duration: float = 5.0, mode: str = "base", variant: str = "FL2VA") -> Brief:
    return Brief(mode=mode, variant=variant, duration=duration, plot=TOPIC)


def _on_disk_session(root, brief: Brief, state: dict, status: str):
    gen = Path(root) / "sessions" / "某主题" / "GEN001"
    save_session(gen, brief, state, status=status)
    loaded = load_session(gen)
    assert loaded is not None
    return loaded


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _spy_drive(monkeypatch) -> list:
    calls: list = []
    real = wizard_base.drive

    def spy(*args, **kwargs):
        calls.append(kwargs.get("frames_shown"))
        return real(*args, **kwargs)

    monkeypatch.setattr(wizard_base, "drive", spy)
    return calls


# ---------------------------------------------------------------------------
# 各中断点续跑：base 短视频走步骤机
# ---------------------------------------------------------------------------

def test_stage1_interrupt_resumes_from_the_declared_next_node(tmp_path, monkeypatch):
    """阶段 1 断在 parallel_visual：起点由步骤机给（下一格是首帧提示词节点），
    续完闸门后由步骤机放行进阶段 2，且不重展提示词。"""
    session = _on_disk_session(
        tmp_path, _brief(),
        {"shot_table": "[Shot 1] 庭院", "_progress": {"last_completed_node": "parallel_visual"}},
        session_store.STATUS_STAGE1_RUNNING)

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_show_frame_prompts", lambda *a, **k: None)
    resumed: dict = {}

    def fake_stage1(brief, config, **kwargs):
        resumed.update(kwargs)
        return dict(session.stage_state), SimpleNamespace(), {"frame_prompt_engineer": object()}

    monkeypatch.setattr(wizard, "run_stage1", fake_stage1)
    stage2: dict = {}

    def stop_stage2(config, session_, *, show_frame_prompts=True):
        stage2["show"] = show_frame_prompts
        return 0

    monkeypatch.setattr(wizard, "_phase2_collect_and_finish", stop_stage2)
    drive_calls = _spy_drive(monkeypatch)

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    assert drive_calls, "base 短视频没有走步骤机驱动"
    assert resumed["resume_from_node"] == FRAME_NODE, \
        "续跑起点与步骤机声明的下一格不一致"
    assert stage2["show"] is False, "修改循环里已经展示过提示词，进阶段 2 不该再刷一遍"


def test_awaiting_frames_runs_the_gate_then_stage2(tmp_path, monkeypatch):
    """阶段 1 已完成、等帧图：先过人工闸门，再由步骤机放行进阶段 2。"""
    session = _on_disk_session(tmp_path, _brief(), {"shot_table": "[Shot 1] 庭院"},
                               session_store.STATUS_AWAITING_FRAMES)

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    order: list[str] = []

    def gate(config, session_):
        order.append("gate")
        return session_

    monkeypatch.setattr(wizard, "_resume_awaiting_frames", gate)

    def stop_stage2(config, session_, *, show_frame_prompts=True):
        order.append(f"stage2:{show_frame_prompts}")
        return 0

    monkeypatch.setattr(wizard, "_phase2_collect_and_finish", stop_stage2)
    drive_calls = _spy_drive(monkeypatch)

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    assert drive_calls == [False], "起点应交给步骤机（闸门未过那一格）"
    assert order == ["gate", "stage2:False"]


# ---------------------------------------------------------------------------
# 别的路线不许被带进这台步骤机
# ---------------------------------------------------------------------------

def test_base_long_form_keeps_the_original_dispatch(tmp_path, monkeypatch):
    """长片接下来是分段陪跑（D6–D8）：仍走原分派，不进 base 短视频步骤机。"""
    session = _on_disk_session(tmp_path, _brief(duration=30.0), {},
                               session_store.STATUS_AWAITING_FRAMES)

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_resume_awaiting_frames", lambda config, s: s)
    recorded: dict = {}

    def stop_stage2(config, session_, *, show_frame_prompts=True):
        recorded["show"] = show_frame_prompts
        return 0

    monkeypatch.setattr(wizard, "_phase2_collect_and_finish", stop_stage2)
    monkeypatch.setattr(wizard_base, "drive", lambda *a, **k: pytest.fail(
        "长片不该进 base 短视频步骤机"))

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    assert recorded["show"] is False


def test_ref_route_does_not_enter_the_step_machine(tmp_path, monkeypatch):
    session = _on_disk_session(tmp_path, _brief(mode="ref", variant="T2VA"), {},
                               session_store.STATUS_AWAITING_FRAMES)

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_resume_awaiting_frames", lambda config, s: s)
    monkeypatch.setattr(wizard, "_phase2_collect_and_finish",
                        lambda *a, **k: pytest.fail("多参落进了首尾帧链的阶段 2"))
    reached: list = []
    monkeypatch.setattr(wizard, "_phase2_ref_finish", lambda config, s: reached.append(s) or 0)
    monkeypatch.setattr(wizard_base, "drive", lambda *a, **k: pytest.fail(
        "多参不该进 base 短视频步骤机"))

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    assert reached and reached[0] is session


def test_director_route_does_not_enter_the_step_machine(tmp_path, monkeypatch):
    session = _on_disk_session(
        tmp_path, Brief(mode="director", variant="T2VA", director_mode="t2v", plot=TOPIC),
        {}, session_store.STATUS_AWAITING_FRAMES)

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_resume_awaiting_frames", lambda config, s: s)
    monkeypatch.setattr(wizard, "_phase2_collect_and_finish",
                        lambda *a, **k: pytest.fail("导演台落进了首尾帧链的阶段 2"))
    reached: list = []
    monkeypatch.setattr(wizard, "_run_director_flow", lambda s: reached.append(s) or 0)
    monkeypatch.setattr(wizard_base, "drive", lambda *a, **k: pytest.fail(
        "导演台不该进 base 短视频步骤机"))

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    assert reached and reached[0] is session


# ---------------------------------------------------------------------------
# 同一 brief：向导 与 直接驱动步骤机 产出同一批文件
# ---------------------------------------------------------------------------

_BUNDLE = {
    "scene_anchor": "秋日庭院",
    "first": {"zimage": {
        "positive_prompt": "秋日庭院里的橘猫，暖光，浅景深",
        "width": 640, "height": 1280, "prompt_node_id": "9",
    }},
    "last": {},
    "continuity_constraints": ["同一只橘猫与同一庭院"],
}


def _stub_base_pipeline(monkeypatch) -> None:
    """把问答、阶段 1/2 与摘要全部换掉，只留「编排」这一层在被测。"""
    _silence(monkeypatch)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        # 按**问题文本**作答，不按位置：同一入口要跑两遍，位置表会被第二次错位。
        asked.append(text)
        return TOPIC if "视频主题" in text else ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_ask_clarifications", lambda config, topic: "")
    monkeypatch.setattr(wizard, "_choose_generation_mode", lambda: "FL2VA")
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    monkeypatch.setattr(wizard, "_show_frame_prompts", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(wizard, "run_stage1",
                        lambda brief, config, **kw: (
                            {"fl2va_prompt_bundle": _BUNDLE, "shot_table": "[Shot 1] 庭院"},
                            SimpleNamespace(), {"frame_prompt_engineer": object()}))
    monkeypatch.setattr(wizard, "run_stage2",
                        lambda state, brief, config, **kw: (dict(state), "最终视频提示词正文"))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *a, **k: None)


def _artifact_files(root) -> list[str]:
    gen_dirs = sorted(Path(root).glob("**/GEN*"))
    assert len(gen_dirs) == 1, f"会话目录不唯一：{gen_dirs}"
    gen = gen_dirs[0]
    return sorted(p.relative_to(gen).as_posix() for p in gen.rglob("*") if p.is_file())


def test_wizard_and_direct_step_machine_produce_the_same_files(tmp_path, monkeypatch):
    """同一条 base 短视频：经向导，与「问步骤机下一步、再执行」直接驱动，产物文件一致。"""
    _stub_base_pipeline(monkeypatch)

    # 路径 A：走完整向导
    cfg_wizard = _cfg(tmp_path / "a")
    assert wizard.run_wizard(cfg_wizard) == 0
    files_wizard = _artifact_files(cfg_wizard.sessions_root)

    # 路径 B：不经 run_wizard 的分派，只按步骤机的回答推进
    cfg_direct = _cfg(tmp_path / "b")
    session = wizard._phase1_new(cfg_direct)
    assert session is not None
    frames_shown = True  # _phase1_new 的收尾已经过人工闸门
    action = step_machine.next_action(
        wizard_base.facts_from_session(session, frames_shown=frames_shown))
    assert action.step == step_machine.STEP_STAGE2, \
        "闸门已过的会话，步骤机的下一步必须是阶段 2"
    assert wizard._phase2_collect_and_finish(
        cfg_direct, session, show_frame_prompts=not frames_shown) == 0
    files_direct = _artifact_files(cfg_direct.sessions_root)

    assert files_wizard == files_direct
    assert "video-prompt.md" in files_wizard, "产物文件集合是空的，这条对照没有咬住东西"


# ---------------------------------------------------------------------------
# --brief 快路径仍然不进会话
# ---------------------------------------------------------------------------

def test_brief_fast_path_still_does_not_create_a_session(tmp_path, monkeypatch):
    """spec §11 / D2-AC17：快路径不建会话、不落 state。"""
    from minimax_h3_prompt import config as config_module

    main_module = importlib.import_module("minimax_h3_prompt.main")
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("输入: 首尾帧\n时长: 5\n## 剧情\n雨夜公交站。", encoding="utf-8")
    monkeypatch.setattr(pipeline, "run_pipeline", lambda brief, cfg: "final")
    monkeypatch.setattr(config_module, "config",
                        SimpleNamespace(output_path=tmp_path / "out.md"))

    def must_not_save(*args, **kwargs):
        raise AssertionError("--brief 快路径不该建会话")

    monkeypatch.setattr(session_store, "save_session", must_not_save)

    assert main_module.main(["--brief", str(brief_file)]) == 0
    assert (tmp_path / "out.md").read_text(encoding="utf-8") == "final"
