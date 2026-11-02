"""阶段 2 → 分段陪跑的帧描述交接。

bug（2026-09-22）：``_phase2_collect_and_finish`` 把读图结果写进
``state = dict(session.stage_state)`` 这个**副本**，而 ``_run_segmented_flow`` 读的是
入口时 load 出来的**旧 session 对象** → 同进程内帧描述也到不了分段流程，
段 1 于是照分镜表写「空店 + 无猫」，与用户提交的首帧图直接矛盾。
"""
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.tools import frame_auditor
from minimax_h3_prompt.ui import wizard

FRAME_DESC = "室内灯光明亮，收银台后站着一名年轻男性店员；前景下方偏左处一只橘色虎斑猫站在门口地砖上。"

TWO_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def test_phase2_hands_frame_descriptions_to_segmented_flow(tmp_path, monkeypatch):
    """阶段 2 收集到的帧读图结果，必须真的送到分段流程手里。"""
    brief = Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    session_store.save_session(
        generation_dir, brief, {"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_AWAITING_FRAMES,
    )
    session = session_store.load_session(generation_dir)
    assert session is not None
    # 根因：入口 load 出来的旧对象上本来就没有帧描述，写副本等于没写
    assert "fl2va_frame_descriptions" not in session.stage_state

    image = tmp_path / "first.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake-first-frame")  # 内容无关：_store_frame 只做 sha256 + 复制

    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: (str(image), ""))
    monkeypatch.setattr(
        frame_auditor, "audit_frame_images",
        lambda refs, variant, **kwargs: [
            frame_auditor.FrameAudit(picture=1, role="first", path=str(image), description=FRAME_DESC)
        ],
    )
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        wizard, "run_stage2",
        lambda state, brief, config, **kwargs: (dict(state), "PROMPT"),
    )

    seen: dict = {}

    def fake_segmented_flow(brief, session, prompt, summary=None, state=None):
        seen["state"] = state
        seen["prompt"] = prompt

    monkeypatch.setattr(wizard, "_run_segmented_flow", fake_segmented_flow)
    config = SimpleNamespace(vision_model=SimpleNamespace(model="stub-vision"))

    assert wizard._phase2_collect_and_finish(config, session) == 0
    state = seen["state"]
    assert state is not None, "分段流程没拿到阶段 2 的 state（读的仍是旧 session 对象）"
    descriptions = state.get("fl2va_frame_descriptions")
    assert descriptions, "同进程内帧描述也没到分段流程 → 段 1 仍不锚定首帧"
    assert descriptions[0]["description"] == FRAME_DESC


def test_fallback_path_passes_frame_anchor(tmp_path, monkeypatch):
    """分段规划失败回退到机械拆分时，逐段重写仍要拿到帧描述与首/末段位置。"""
    brief = Brief(mode="base", variant="I2VA", duration=12.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    session = session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_COMPLETED,
    )
    state = {
        "shot_table": "[Shot 1] 深夜空店内……",
        "fl2va_frame_descriptions": [{"role": "first", "description": FRAME_DESC}],
    }

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *args, **kwargs: None)
    monkeypatch.setattr(wizard, "_confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(wizard, "_prompt", lambda *args, **kwargs: "")
    # 本测试的题目是「回退路径有没有把帧锚定上下文传下去」，不是交接校验本身：
    # 视频必经环节（issue #13）由 test_wizard_bridge_gate.py 覆盖，这里直接放行。
    monkeypatch.setattr(wizard, "_acquire_bridge_frame", lambda *a, **k: True)

    calls: list[dict] = []

    def fake_rewrite(segment, full_prompt, llm, **kwargs):
        calls.append({"shot_number": segment.shot_number, **kwargs})
        return f"REWRITTEN {segment.shot_number}"

    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", fake_rewrite)

    wizard._run_segmented_flow(brief, session, TWO_SHOT_PROMPT, state=state)

    assert calls, "回退路径没有调用逐段重写"
    assert all(c["state"] is state for c in calls), "回退路径没把 state（含帧描述）传下去"
    assert calls[0]["is_first"] is True and calls[0]["is_last"] is False
    assert calls[-1]["is_last"] is True
