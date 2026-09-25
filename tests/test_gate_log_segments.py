"""gate-log 里段号的语义唯一性（#14/#16 复审）。

复核发现同一份 log 的 `segment` 字段在两条路径上指的不是同一段：

- v2：`_acquire_bridge_frame` 在**刚跑完那一段**的末尾调用，交上来的是**本段**的输出视频
  ——字段值＝产出段；
- 回退：它在**本段开头**调用，交上来的是**上一段**的输出视频——字段值＝消费段。

而 #14 的验收标准说这份 log 是自动判据（#15）取阈值的**唯一**素材，要靠它定位「哪一段的
锚」。同一个数字两处含义不同，事后就没法复跑。

改成两个各自无歧义的字段，由调用点各自声明自己那两件事实：
- ``anchor_segment``：这一帧成了**哪一段**的开场锚（末帧核验没有锚 → None）
- ``judged_video_segment``：被核的那段**输出视频**属于哪一段
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.ui import wizard

FIXTURE = Path(__file__).parent / "fixtures" / "official_shot01_i2va.md"


def _session(tmp_path) -> session_store.SessionState:
    brief = Brief(mode="base", variant="I2VA", duration=12.0, style="写实", plot="便利店橘猫")
    gen = tmp_path / "GEN001"
    gen.mkdir(parents=True, exist_ok=True)
    return session_store.SessionState(directory=gen, brief=brief,
                                      stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
                                      status=session_store.STATUS_SEGMENTED_RUNNING)


def _plans() -> list[SegmentPlan]:
    return [
        SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,), summary="守店",
                    end_hook="第一段末态"),
        SegmentPlan(index=1, start_s=4, end_s=8, shots_in_segment=(2,), summary="猫上柜台",
                    end_hook="第二段末态"),
    ]


def _patch(monkeypatch, tmp_path, *, description="桥接帧画面"):
    """拦住真剥帧、静音确认。``_prompt`` 由各用例自己给序列——

    别在这里固定返回同一个值：确认循环拿不到 "" 会一直转（本用例集第一次就踩了）。
    """
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")

    def fake_capture(video_path, position, generation_dir, *, expected_seconds=None,
                     frame_suffix="start"):
        return [], description, frame

    monkeypatch.setattr(wizard, "_capture_bridge_frame", fake_capture)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)


def _answers(monkeypatch, sequence: list[str]):
    iterator = iter(sequence)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(iterator, ""))


def _log(session) -> list[dict]:
    path = session.directory / "segments" / "gate-log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_v2_records_the_anchor_segment_and_the_judged_video_segment(tmp_path, monkeypatch):
    """v2：跑完第 1 段后核它的视频（judged=1），得到的帧成为第 2 段的开场锚（anchor=2）。"""
    from minimax_h3_prompt import segment_prompts

    session = _session(tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _patch(monkeypatch, tmp_path)
    # 段1 已生成好 → 交段1 的输出视频 → 段2（末段）已生成好
    _answers(monkeypatch, ["", "video.mp4", ""])

    wizard._run_segmented_flow_v2(session, _plans(),
                                  {"shot_table": "[Shot 1] 深夜空店内……"}, SimpleNamespace())

    record = _log(session)[0]
    assert record["judged_video_segment"] == 1, "被核的是第 1 段的输出视频"
    assert record["anchor_segment"] == 2, "这一帧是第 2 段的开场锚"
    assert "segment" not in record, "二义性的字段不许留着"


def test_v2_judgement_title_names_the_anchor_segment(tmp_path, monkeypatch, capsys):
    """并排标题曾错一格（说「第 1 段的首帧锚」，实为第 2 段）——人判时看的就是这行。"""
    from minimax_h3_prompt import segment_prompts

    session = _session(tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _patch(monkeypatch, tmp_path)
    _answers(monkeypatch, ["", "video.mp4", ""])

    wizard._run_segmented_flow_v2(session, _plans(),
                                  {"shot_table": "[Shot 1] 深夜空店内……"}, SimpleNamespace())

    out = capsys.readouterr().out
    assert "第 2/2 段的首帧锚" in out, "标题没有指向真正被锚的那一段"
    assert "第 1 段" in out, "标题要同时点出这帧是从哪一段来的"


def test_fallback_records_the_anchor_segment_and_the_judged_video_segment(tmp_path, monkeypatch):
    """回退：写第 2 段前核第 1 段的视频（judged=1），得到的帧是第 2 段的锚（anchor=2）。

    两条路径算出来的**事实**必须一样，即便它们调用闸门的时机相反。
    """
    from minimax_h3_prompt import model_factory, segment_planner, segment_prompts

    session = _session(tmp_path)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: None)
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")
    answers = iter(["", "video.mp4", ""])
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers, ""))

    prompt = ("For the target video, at 0.00 seconds into the target video, "
              "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
              "[Shot 1] The store interior is still.\n"
              "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
              "overall_soundscape: a low hum.\n\n"
              "non_diegetic_music: N/A\n")
    wizard._run_segmented_flow(session.brief, session, prompt, state={})

    record = _log(session)[0]
    assert record["judged_video_segment"] == 1
    assert record["anchor_segment"] == 2


def test_last_frame_record_has_no_anchor_segment(tmp_path, monkeypatch):
    """末帧核验核的是末段**自己**的收尾，不是下一段的开场锚 → anchor_segment 为 None。"""
    from minimax_h3_prompt.tools import frame_auditor

    session = _session(tmp_path)
    frame = tmp_path / "tail.png"
    frame.write_bytes(b"png")
    monkeypatch.setattr(frame_auditor, "extract_last_frame", lambda *a, **k: frame)
    monkeypatch.setattr(frame_auditor, "describe_bridge_frame", lambda path: "末帧画面")
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    answers = iter(["last.mp4"])
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers, ""))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    state = {"fl2va_frame_descriptions": [{"role": "last", "description": "承诺的尾帧画面"}]}

    wizard._verify_last_frame(state, 1, session.directory, end_hook="末段末态",
                              segment_number=2, total=2, expected_seconds=4.0)

    record = _log(session)[0]
    assert record["anchor_segment"] is None
    assert record["judged_video_segment"] == 2
