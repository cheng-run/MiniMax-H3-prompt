"""末段尾帧一致性校验（issue #16）——链的另一端。

链式闸门只管**段间**（末段不剥帧：``if position + 1 < total``）。链的**另一端**留着
同一个洞：FL2VA/L2VA 下末段必须收在用户提交的那张尾帧上，而现在只有 ``_frame_anchor_note``
在提示词里**声明**这个要求（「视频结束时必须落到用户提交的尾帧实际画面」），**没有任何
校验**；末段连输出视频路径都不会索要。整条视频的最后画面是否真的落在承诺的那张尾帧上
——那是 FL2VA/L2VA 的核心承诺——只能靠人自己盯。

复用语义层（#14）的对照机制与 ``gate-log.jsonl`` 记录形状；差别在末段没有下游锚可换，
所以动作集少掉「手工换帧／改用关键帧图另起」两条。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.tools import frame_auditor
from minimax_h3_prompt.ui import wizard

END_HOOK = "猫的前爪刚落在柜台上，店员的手还没伸过去"
PROMISED = "便利店收银台前：橘猫蹲在柜台上，店员低头伸手去摸它。"
# 实测末帧与承诺是**同一件事的两种措辞**（读图模型不会逐字复述），所以两者必须不同串，
# 否则「并排顺序」这类断言会退化成拿同一段文字自比。
ACTUAL = "收银台前的画面：橘猫蹲坐于柜台上，店员伸手轻摸它的头。"
WRONG = "便利店玻璃门外街道夜景，画面里没有猫也没有店员。"


def _brief(duration: float = 8.0) -> Brief:
    return Brief(mode="base", variant="FL2VA", duration=duration, style="写实", plot="便利店橘猫")


def _session(tmp_path) -> session_store.SessionState:
    gen = tmp_path / "GEN001"
    gen.mkdir(parents=True, exist_ok=True)
    return session_store.SessionState(directory=gen, brief=_brief(),
                                      stage_state={},
                                      status=session_store.STATUS_SEGMENTED_RUNNING)


def _state(*, with_last: bool = True, with_first: bool = True) -> dict:
    frames = []
    if with_first:
        frames.append({"picture": 1, "role": "first", "path": "a.png",
                       "description": "店员独自站在收银台后。"})
    if with_last:
        frames.append({"picture": 2, "role": "last", "path": "b.png", "description": PROMISED})
    return {"shot_table": "[Shot 1] x", "fl2va_frame_descriptions": frames}


def _patch_strip(monkeypatch, tmp_path, *, description: str = ACTUAL):
    """拦住真剥帧（本票测的是末段核验的接线与判定），返回会被写出的帧图路径。"""
    frame = tmp_path / "extracted_tail.png"
    frame.write_bytes(b"png")
    monkeypatch.setattr(frame_auditor, "extract_last_frame", lambda *a, **k: frame)
    monkeypatch.setattr(frame_auditor, "describe_bridge_frame", lambda path: description)
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    return frame


def _answers(monkeypatch, sequence: list[str], *, reached: bool = True):
    it = iter(sequence)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(it, ""))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: reached)


def _log(session) -> list[dict]:
    path = session.directory / "segments" / "gate-log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- 不该误报的情形 -----------------------------------------------------

def test_no_promised_last_frame_means_no_check(tmp_path, monkeypatch, capsys):
    """无尾帧变体（I2VA / T2VA）不得误报：没有 role=last 就什么都不做、直接放行。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path)
    _answers(monkeypatch, [])

    allowed = wizard._verify_last_frame(_state(with_last=False), 1, session.directory,
                                        end_hook=END_HOOK, segment_number=2, total=2,
                                        expected_seconds=4.0)

    assert allowed is True
    out = capsys.readouterr().out
    assert "[末段尾帧核验]" not in out, "无尾帧变体被误报了"
    assert _log(session) == [], "无尾帧变体不该产生闸门遭遇记录"


def test_empty_frame_descriptions_means_no_check(tmp_path, monkeypatch, capsys):
    """T2VA 连帧图都没有：同样不得冒出来一个空核对。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path)
    _answers(monkeypatch, [])

    allowed = wizard._verify_last_frame({"shot_table": "[Shot 1] x"}, 1, session.directory,
                                        end_hook=None, segment_number=2, total=2,
                                        expected_seconds=None)

    assert allowed is True
    assert "[末段尾帧核验]" not in capsys.readouterr().out


# --- 三方对照 -----------------------------------------------------------

def test_evidence_puts_end_hook_and_promised_tail_against_the_actual_tail(tmp_path, monkeypatch, capsys):
    """并排三方：该段末态声明、用户提交尾帧的画面（承诺的落点）、末段实际末帧画面。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path)
    _answers(monkeypatch, ["last_seg.mp4"], reached=True)

    wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                              segment_number=2, total=2, expected_seconds=4.0)

    out = capsys.readouterr().out
    assert "[末段尾帧核验]" in out
    # 读图结果在剥帧那一步也会打一次，所以顺序只能在**判定块内**断言
    block = out.split("[末段尾帧核验] 第")[-1]
    assert END_HOOK in block, "判定块里缺少该段末态声明"
    assert PROMISED in block, "判定块里缺少用户提交尾帧的画面（承诺的落点）"
    assert ACTUAL in block, "判定块里缺少末段实际末帧画面"
    order = [block.index(text) for text in ("该段末态声明", "承诺的落点", "末段实际末帧画面")]
    assert order == sorted(order), f"三方证据没有按序并排：{order}"


def test_missing_end_hook_still_checks_against_the_promised_frame(tmp_path, monkeypatch, capsys):
    """回退路径没有 plan 末态声明时，仍要对着**承诺的尾帧画面**核（那是硬承诺）。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path)
    _answers(monkeypatch, ["last_seg.mp4"], reached=True)

    wizard._verify_last_frame(_state(), 1, session.directory, end_hook=None,
                              segment_number=2, total=2, expected_seconds=4.0)

    out = capsys.readouterr().out
    assert "[末段尾帧核验]" in out and PROMISED in out


# --- 动作集 -------------------------------------------------------------

def test_not_reached_defaults_to_stop(tmp_path, monkeypatch, capsys):
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path, description=WRONG)
    _answers(monkeypatch, ["last_seg.mp4", ""], reached=False)

    allowed = wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                                        segment_number=2, total=2, expected_seconds=4.0)

    assert allowed is False
    out = capsys.readouterr().out
    assert "[劝阻]" in out and "默认**不继续**" in out
    assert _log(session)[-1]["action"] == wizard.GATE_STOP


def test_accept_overrides_and_continues(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path, description=WRONG)
    _answers(monkeypatch, ["last_seg.mp4", "3"], reached=False)

    allowed = wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                                        segment_number=2, total=2, expected_seconds=4.0)

    assert allowed is True
    record = _log(session)[-1]
    assert record["action"] == wizard.GATE_ACCEPT
    assert record["verdict"] == "not_reached"


def test_rerun_asks_for_the_last_segment_video_again(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path, description=WRONG)
    _answers(monkeypatch, ["v1.mp4", "2", "v2.mp4", ""], reached=False)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: bool(_log(session)))

    allowed = wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                                        segment_number=2, total=2, expected_seconds=4.0)

    assert allowed is True
    assert [r["action"] for r in _log(session)] == [wizard.GATE_RERUN, wizard.GATE_CONTINUE]


def test_last_segment_menu_has_no_frame_replacement(tmp_path, monkeypatch, capsys):
    """末段没有下游锚可换：菜单不得给出「手工换帧／改用关键帧图另起」。"""
    session = _session(tmp_path)
    _patch_strip(monkeypatch, tmp_path, description=WRONG)
    # 先给一个非法选项（应重问），再选 3＝接受并继续
    _answers(monkeypatch, ["v.mp4", "4", "3"], reached=False)

    allowed = wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                                        segment_number=2, total=2, expected_seconds=4.0)

    out = capsys.readouterr().out
    menu = out.split("[劝阻]")[1]
    assert "手工换帧" not in menu, "末段不该出现「手工换帧」（没有下游锚可换）"
    assert "关键帧图另起" not in menu, "末段不该出现「改用关键帧图另起」"
    assert "无效选项" in out, "非法选项必须重问，不能被当成某个选项接受"
    assert allowed is True
    assert _log(session)[-1]["action"] == wizard.GATE_ACCEPT


def test_gate_log_records_the_last_frame_verdict(tmp_path, monkeypatch):
    """记录形状复用 #14：段号＝末段，且带上承诺尾帧与实测末帧两边的画面描述。"""
    session = _session(tmp_path)
    frame = _patch_strip(monkeypatch, tmp_path, description=WRONG)
    _answers(monkeypatch, ["v.mp4", "3"], reached=False)

    wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                              segment_number=2, total=2, expected_seconds=4.0)

    record = _log(session)[-1]
    assert record["judged_video_segment"] == 2
    assert record["anchor_segment"] is None, "末帧核验没有下游锚"
    assert record["end_hook"] == END_HOOK
    assert record["bridge_frame_description"] == WRONG
    assert record["promised_last_frame_description"] == PROMISED
    assert record["bridge_frame_path"] == str(frame)


# --- 降级 ---------------------------------------------------------------

def test_strip_failure_warns_and_does_not_block(tmp_path, monkeypatch, capsys):
    """剥帧失败＝「我们量不出来」，不是「末段错了」——按项目纪律不许据此阻断。"""
    session = _session(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("视频读不了")

    monkeypatch.setattr(frame_auditor, "extract_last_frame", boom)
    _answers(monkeypatch, ["v.mp4"], reached=True)

    allowed = wizard._verify_last_frame(_state(), 1, session.directory, end_hook=END_HOOK,
                                        segment_number=2, total=2, expected_seconds=4.0)

    assert allowed is True, "测不出事实就阻断 = 把「量不出来」当成「错了」"
    assert "[警告]" in capsys.readouterr().out


# --- 接线 ---------------------------------------------------------------

def test_v2_flow_runs_the_last_frame_check_after_the_loop(tmp_path, monkeypatch, capsys):
    """接线断言：跑完全部段之后、宣布「全部完成」之前，必须核末段尾帧。"""
    from minimax_h3_prompt import segment_prompts
    from minimax_h3_prompt.segment_planner import SegmentPlan

    session = _session(tmp_path)
    plans = [
        SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,), summary="守店",
                    end_hook="猫还没进门"),
        SegmentPlan(index=1, start_s=4, end_s=8, shots_in_segment=(2,), summary="猫上柜台",
                    end_hook=END_HOOK),
    ]
    state = _state()
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "SEG")
    monkeypatch.setattr(wizard, "_acquire_bridge_frame", lambda *a, **k: True)
    _patch_strip(monkeypatch, tmp_path)
    _answers(monkeypatch, ["", "", "last_seg.mp4"], reached=True)

    finished = wizard._run_segmented_flow_v2(session, plans, state,
                                             SimpleNamespace())

    out = capsys.readouterr().out
    assert finished is True
    assert "[末段尾帧核验]" in out
    assert out.index("[末段尾帧核验]") < out.index("全部 2 段已人工确认完成"), \
        "末帧核验必须发生在宣布完成之前"
    assert _log(session)[-1]["judged_video_segment"] == 2


def test_v2_flow_stops_when_the_last_frame_verdict_is_stop(tmp_path, monkeypatch, capsys):
    """末帧核验没通过 → 整条流程不得报「全部完成」（否则会话会被标成已完成）。"""
    from minimax_h3_prompt import segment_prompts
    from minimax_h3_prompt.segment_planner import SegmentPlan

    session = _session(tmp_path)
    plans = [SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,), summary="守店",
                         end_hook=END_HOOK)]
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "SEG")
    _patch_strip(monkeypatch, tmp_path, description=WRONG)
    _answers(monkeypatch, ["", "last_seg.mp4", ""], reached=False)

    finished = wizard._run_segmented_flow_v2(session, plans, _state(),
                                             SimpleNamespace())

    assert finished is False
    assert "全部 1 段已人工确认完成" not in capsys.readouterr().out


def test_fallback_flow_also_runs_the_last_frame_check(tmp_path, monkeypatch, capsys):
    """两条分段路径都覆盖（验收标准第 4 条）。"""
    from minimax_h3_prompt import model_factory, segment_planner, segment_prompts

    session = _session(tmp_path)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: None)
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt",
                        lambda segment, *a, **k: f"REWRITTEN {segment.shot_number}")
    _patch_strip(monkeypatch, tmp_path)
    # 段1 生成好 → 第 2 段的桥接视频 → 段2 生成好 → 末段核验的视频
    _answers(monkeypatch, ["", "seg1.mp4", "", "last_seg.mp4"], reached=True)

    prompt = ("For the target video, at 0.00 seconds into the target video, "
              "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
              "[Shot 1] The store interior is still.\n"
              "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
              "overall_soundscape: a low hum.\n\n"
              "non_diegetic_music: N/A\n")
    wizard._run_segmented_flow(session.brief, session, prompt, state=_state())

    out = capsys.readouterr().out
    assert "[末段尾帧核验]" in out, "回退路径没有末帧核验"
    assert PROMISED in out
