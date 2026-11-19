"""base 长片分段规划必须把镜头切点表交给规划层（#343）。

「规则在、接线不在」是本仓反复踩过的坑：#343 给 ``parse_segment_plan`` 加了「边界
不得落在切点」的判据后，两个 base 调用点若不把切点传进去，约束就等于不存在——不会
报错，只是静默不生效。这两条钉住**调用点传了什么**（外部行为），不看内部实现。
"""
from types import SimpleNamespace

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.ui import wizard

SHOT_TABLE = (
    "[Shot 1] At 00:00.000, a wide shot of the street in the rain.\n"
    "[Shot 2] At 00:06.000, a closer shot as she turns at the corner."
)

TWO_SHOT_PROMPT = (
    "[Shot 1] The street is empty in the rain.\n"
    "[Shot 2] At 00:06.000, she turns at the corner.\n\n"
    "overall_soundscape: steady rain.\n\n"
    "non_diegetic_music: N/A\n"
)


def test_segmented_flow_hands_shot_cuts_to_planner(tmp_path, monkeypatch, capsys):
    brief = Brief(mode="base", variant="I2VA", duration=12.0, style="写实", plot="雨夜旧信")
    directory = tmp_path / "GEN001"
    directory.mkdir()
    session = session_store.SessionState(
        directory=directory, brief=brief,
        stage_state={"shot_table": SHOT_TABLE},
        status=session_store.STATUS_COMPLETED,
    )
    state = {"shot_table": SHOT_TABLE}

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())

    calls: list[dict] = []

    def fake_plan_segments(shot_table, total_s, llm, **kwargs):
        calls.append({"shot_table": shot_table, "total_s": total_s, **kwargs})
        return None  # 走回退路径：本用例只关心规划层的入参

    monkeypatch.setattr(segment_planner, "plan_segments", fake_plan_segments)
    # 回退路径的交互一律放行，别让本用例被无关问答挡住。
    monkeypatch.setattr(wizard, "_confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(wizard, "_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr(wizard, "_acquire_bridge_frame", lambda *a, **k: True)
    monkeypatch.setattr(
        segment_prompts, "rewrite_segment_prompt",
        lambda segment, full_prompt, llm, **kwargs: f"REWRITTEN {segment.shot_number}")

    wizard._run_segmented_flow(brief, session, TWO_SHOT_PROMPT, state=state)

    assert len(calls) == 1
    # 切点表由同一份分镜表抽出：含 0 与总时长，内部切点在 6s。
    assert calls[0]["shot_cuts"] == (0, 6, 12)

    # US10（spec #342）：回复不合格回退机械拆分时必须留下可见记录——机械拆分按镜头
    # 切点切，接缝处可能换机位。静默回退等于把这一结构风险藏起来。
    out = capsys.readouterr().out
    assert "回复不合格" in out
    assert "机械拆分按镜头切点切" in out
