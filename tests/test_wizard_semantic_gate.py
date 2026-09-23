"""锚帧语义层与剥帧流程重排（issue #14）。

**要解决的问题**：桥接链的每一环靠人看一眼，但证据从来**没有在决策时刻被摆到一起**——
``plan.end_hook``（该达到的末态）只在展示提示词时打印，比那张帧存在**早约十分钟**；
剥帧当轮只打印读图结果。人得在两个时刻各记一半，然后凭记忆判。

本票做四件事：
1. 语义层主判定：剥帧当轮把「该达到的末态」与「尾帧实际画面」**并排**打给人判；
   判不合格 → **劝阻**（默认不继续，可显式覆盖）。
2. 流程重排：剥帧读图**前置于**「本段满意」——先摆证据，再由人决定继续／重跑。
3. 动作集：重跑本段／手工换帧／改用关键帧图另起／显式接受并继续；**不含自动重掷**
   （#10 已论证重掷不是独立实验）。
4. 每次遭遇写 ``segments/gate-log.jsonl``——这是将来给自动判据（#15）校准阈值**唯一**
   的素材来源，所以覆盖与重跑两类判定都要记，且必须带尾帧图路径（否则无法事后复跑）。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.ui import wizard

END_HOOK = "店员右手刚从扫码枪上收回，猫还没出现在画面里"
BRIDGE_DESC = "便利店收银台中景：店员右手搭在台面上，画面没有猫。"
OTHER_DESC = "木框棕色玻璃门特写，门内是地砖，看不到收银台。"


def _session(tmp_path) -> session_store.SessionState:
    brief = Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫")
    gen = tmp_path / "GEN001"
    gen.mkdir(parents=True, exist_ok=True)
    return session_store.SessionState(directory=gen, brief=brief, stage_state={},
                                      status=session_store.STATUS_SEGMENTED_RUNNING)


def _patch_strip(monkeypatch, *, description: str = BRIDGE_DESC, frame_name: str = "f.png"):
    """拦住真剥帧：本票要测的是剥帧**之后**的语义层与流程，可证层另有专测。"""
    frame = Path("/tmp") / frame_name

    def fake(video_path, position, generation_dir, *, expected_seconds=None):
        return [], description, frame

    monkeypatch.setattr(wizard, "_capture_bridge_frame", fake)
    return frame


def _answers(monkeypatch, sequence: list[str], *, reached: bool = True):
    """``_prompt`` 按序列出，``_confirm`` 固定为「画面达到末态了吗」的答案。"""
    it = iter(sequence)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(it, ""))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: reached)


def _log(session) -> list[dict]:
    path = session.directory / "segments" / "gate-log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- 语义层：并排证据 ---------------------------------------------------

def test_evidence_is_printed_side_by_side(tmp_path, monkeypatch, capsys):
    """末态声明与尾帧实际画面必须出现在**同一个**证据块里，摆在人做决定的那一刻。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["some_video.mp4"], reached=True)

    wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    out = capsys.readouterr().out
    assert "[锚帧语义核验]" in out
    assert END_HOOK in out, "该达到的末态没有出现在决策时刻"
    assert BRIDGE_DESC in out, "尾帧实际画面没有出现在决策时刻"
    assert out.index(END_HOOK) < out.index(BRIDGE_DESC), "证据没有并排（末态在前、画面在后）"
    assert "该达到的末态" in out and "尾帧实际画面" in out, "两半证据的标签必须标明"


def test_semantic_layer_runs_on_the_fallback_path_too(tmp_path, monkeypatch, capsys):
    """验收标准：两条分段路径都要落到。

    回退路径没有 plan 层，末态参照取上一段**落盘那份**提示词的收尾句，并标明是降级参照。
    """
    session = _session(tmp_path)
    from minimax_h3_prompt import model_factory, segment_planner, segment_prompts

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: None)
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt",
                        lambda segment, *a, **k: f"REWRITTEN {segment.shot_number}\n末尾节拍：猫落上柜台")
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["", "some_video.mp4"], reached=True)

    prompt = ("For the target video, at 0.00 seconds into the target video, "
              "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
              "[Shot 1] The store interior is still.\n"
              "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
              "overall_soundscape: a low hum.\n\n"
              "non_diegetic_music: N/A\n")
    wizard._run_segmented_flow(session.brief, session, prompt, state={})

    out = capsys.readouterr().out
    assert "[锚帧语义核验]" in out, "回退路径没有语义层"
    assert "末尾节拍：猫落上柜台" in out, "回退路径没有把上一段收尾句当作末态参照"
    assert "无 plan 末态声明" in out, "降级参照必须标明，不假装它是 end_hook"


# --- 动作集 -------------------------------------------------------------

def test_not_reached_defaults_to_stop(tmp_path, monkeypatch, capsys):
    """判不合格 → 劝阻，且**默认不继续**（回车即停）。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["some_video.mp4", ""], reached=False)

    allowed = wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    assert allowed is False, "判不合格却放行了"
    out = capsys.readouterr().out
    assert "[劝阻]" in out and "默认**不继续**" in out
    assert _log(session)[-1]["action"] == wizard.GATE_STOP


def test_explicit_accept_overrides_and_continues(tmp_path, monkeypatch, capsys):
    """「接受并继续」是**显式覆盖**：能继续，但判定要留在 gate-log 里当负样本。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["some_video.mp4", "5"], reached=False)

    allowed = wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    assert allowed is True
    record = _log(session)[-1]
    assert record["action"] == wizard.GATE_ACCEPT
    assert record["verdict"] == "not_reached", "覆盖也要如实记成「未达到」"


def test_rerun_asks_for_a_new_video_on_the_same_segment(tmp_path, monkeypatch, capsys):
    """「重跑本段」＝留在这一段重新索要输出视频，不是跳到下一段、也不是从头重来。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["old.mp4", "2", "new.mp4", ""], reached=False)
    monkeypatch.setattr(wizard, "_confirm",
                        lambda *a, **k: False if not _log(session) else True)

    allowed = wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    assert allowed is True
    actions = [r["action"] for r in _log(session)]
    assert actions == [wizard.GATE_RERUN, wizard.GATE_CONTINUE], actions
    out = capsys.readouterr().out
    assert "重跑本段" in out and "第 2/4 段" in out, "重跑话术必须点明是哪一段"


def test_manual_frame_uses_the_supplied_image(tmp_path, monkeypatch, capsys):
    """「手工换帧」用用户给的那张图作锚，锚的描述来自**那张图**的读图结果。"""
    from minimax_h3_prompt.tools import reference_auditor

    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    supplied = tmp_path / "my_tail.png"
    supplied.write_bytes(b"png")
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: OTHER_DESC)
    _answers(monkeypatch, ["some_video.mp4", "3", str(supplied)], reached=False)
    state: dict = {}

    allowed = wizard._capture_bridge_frame_until_clean(
        state, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    assert allowed is True
    assert _log(session)[-1]["action"] == wizard.GATE_MANUAL_FRAME
    assert state["bridge_frame_descriptions"] == [{"segment": 1, "description": OTHER_DESC}]
    assert (session.directory / "bridge_frames" / "shot-02-start.png").is_file(), \
        "换的帧没有落到规范位置"


def test_keyframe_restart_hint_only_after_two_blocks(tmp_path, monkeypatch, capsys):
    """「改用关键帧图另起」是 #10 的兜底 B：第一次被拦不提示，第二次才提示。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    # 第一次判不合格 → 选「重跑本段」回到判断题；第二次判不合格才看得到兜底提示
    _answers(monkeypatch, ["v.mp4", "2", "v.mp4", ""], reached=False)

    wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    out = capsys.readouterr().out
    blocks = out.split("[劝阻]")[1:]
    assert len(blocks) == 2, f"期望两次遭遇，实际 {len(blocks)}"
    assert "考虑第 4 条另起" not in blocks[0], "第一次被拦就提示兜底路径（#10 说它不作常规走法）"
    assert "考虑第 4 条另起" in blocks[1], "连续被拦两次必须给出兜底提示"


def test_action_set_has_no_auto_reroll(tmp_path, monkeypatch, capsys):
    """动作集不含自动重掷（#10：同载荷换 seed 不是独立实验），非法输入只重问。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["v.mp4", "9", "z", "4", str(tmp_path / "kf.png")], reached=False)
    (tmp_path / "kf.png").write_bytes(b"png")
    from minimax_h3_prompt.tools import reference_auditor

    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: OTHER_DESC)

    allowed = wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    assert allowed is True
    assert _log(session)[-1]["action"] == wizard.GATE_KEYFRAME_RESTART
    assert "无效选项" in capsys.readouterr().out
    assert set(wizard._OUTCOME_LABELS) == {
        wizard.GATE_CONTINUE, wizard.GATE_ACCEPT, wizard.GATE_RERUN,
        wizard.GATE_MANUAL_FRAME, wizard.GATE_KEYFRAME_RESTART, wizard.GATE_STOP,
    }


# --- 记录与接线 ---------------------------------------------------------

def test_gate_log_records_both_kinds_of_verdict_with_frame_path(tmp_path, monkeypatch):
    """覆盖与重跑**两类判定都要记**，且带尾帧图路径——自动判据靠它事后复跑。"""
    session = _session(tmp_path)
    frame = _patch_strip(monkeypatch, frame_name="shot-02-start.png")
    _answers(monkeypatch, ["a.mp4", "2", "b.mp4", ""], reached=False)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: bool(_log(session)))

    wizard._capture_bridge_frame_until_clean(
        {}, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    records = _log(session)
    assert {r["action"] for r in records} == {wizard.GATE_RERUN, wizard.GATE_CONTINUE}, \
        "两类判定没有都进 gate-log"
    for record in records:
        assert record["end_hook"] == END_HOOK
        assert record["bridge_frame_description"] == BRIDGE_DESC
        assert record["bridge_frame_path"] == str(frame), "缺尾帧图路径就无法事后复跑"
        assert record["segment"] == 2, "段号必须 1-based，与其它话术一致"


def test_rerun_verdict_does_not_leave_a_stale_anchor_in_state(tmp_path, monkeypatch):
    """接线断言：判定为「重跑」时**不得**把这段错锚写进 state。

    写了会怎样：``_bridge_frame_descriptions`` 同段号取最后一个，错锚会被当成事实喂给
    写段 LLM —— 正是「图片是唯一事实源」要防的东西。
    """
    session = _session(tmp_path)
    _patch_strip(monkeypatch, description=OTHER_DESC)
    _answers(monkeypatch, ["a.mp4", "2", "b.mp4", ""], reached=False)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: bool(_log(session)))
    state: dict = {}

    wizard._capture_bridge_frame_until_clean(
        state, 1, session.directory, expected_seconds=4.0, ask="视频路径：",
        segment_number=2, total=4, reference=END_HOOK)

    anchors = state.get("bridge_frame_descriptions", [])
    assert len(anchors) == 1, f"重跑时写进了多余的锚：{anchors}"
    assert anchors[0]["description"] == OTHER_DESC


def test_v2_flow_passes_the_current_segments_end_hook(tmp_path, monkeypatch, capsys):
    """接线断言（流程侧）：v2 必须把**刚跑完那段**的 end_hook 当末态参照交下去。"""
    from minimax_h3_prompt import segment_prompts
    from minimax_h3_prompt.segment_planner import SegmentPlan

    session = _session(tmp_path)
    plans = [
        SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                    summary="店员守店", end_hook=END_HOOK),
        SegmentPlan(index=1, start_s=4, end_s=8, shots_in_segment=(2,),
                    summary="猫上柜台", end_hook="猫前爪搭上台面"),
    ]
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "SEG")
    _patch_strip(monkeypatch)
    _answers(monkeypatch, ["", "a.mp4", ""], reached=True)

    wizard._run_segmented_flow_v2(session.brief, session, plans, {"shot_table": "[Shot 1] x"},
                                  SimpleNamespace())

    out = capsys.readouterr().out
    assert "[锚帧语义核验]" in out
    assert END_HOOK in out, "v2 没把本段的 end_hook 作为末态参照"
