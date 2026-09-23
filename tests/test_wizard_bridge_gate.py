"""可证层交接校验（issue #13）：本段交上来的输出视频要在剥帧当轮判，不合格硬阻断。

分段的交接物是「本段输出视频 + 由它剥出的锚帧」这一对。原票面写的是「在剥帧点调
``preflight.check_input_frame``」，但那个调用点**恒真**：向导的锚帧是当场用
``extract_last_frame`` 从这段视频剥出来的，而 ``check_input_frame`` 比的正是**同一段
视频**的尾窗（窗口最小 mad 必然 < ``MAD_MAYBE``）。实证：

    A的剥出帧 ↔ A段视频（向导会传的组合）: 零 issue（放行）
    A的剥出帧 ↔ B段视频（喂错段的场景）:  [('error', 'bridge_frame_mismatch')]

接一个恒真的检查 = 「接线在、牙齿不在」。故裁定（用户 2026-09-23）改为：本票接
**当场真能报错**的可证检查（段视频时长 / 尺寸），``check_input_frame`` 的 MAD 档
留给「用户回报实际投喂的那张图」的输入点（#14），那里它才有牙齿。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.tools.preflight import EXPECTED_VIDEO_SIZE, check_segment_video
from minimax_h3_prompt.ui import wizard

BRIDGE_DESC = "便利店收银台中景：玻璃店门完整关闭，画面中没有猫；店员右手平放在台面上。"

TWO_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _make_video(path: Path, seconds: float, *, size=EXPECTED_VIDEO_SIZE, fps: int = 24) -> Path:
    """写一段真视频（时长可控）。时长由 帧数/fps 得出，故帧数取整。"""
    frames = max(2, int(round(seconds * fps)))
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert writer.isOpened(), "测试环境缺 MJPG 编码器"
    image = np.full((size[1], size[0], 3), 128, np.uint8)
    for i in range(frames):
        image[:] = 128 + (i % 8)
        writer.write(image)
    writer.release()
    actual = video_meta(path).get("duration_s", 0.0)
    assert abs(actual - frames / fps) < 0.05, f"测试视频时长不可控：{actual}"
    return path


def _session(tmp_path):
    brief = Brief(mode="base", variant="I2VA", duration=12.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    return brief, session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_COMPLETED,
    )


def _plans2():
    """两段各 4s，好让测试视频小一点（真写视频要花时间）。"""
    return [
        SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                    summary="店员守店", end_hook="右手停在台面"),
        SegmentPlan(index=1, start_s=4, end_s=12, shots_in_segment=(2,),
                    summary="猫冲进店内", end_hook="猫落上柜台"),
    ]


# ---------------------------------------------------------------------------
# check_segment_video：可证判据本身
# ---------------------------------------------------------------------------

def test_matching_duration_passes(tmp_path):
    video = _make_video(tmp_path / "seg.avi", 4.0)
    assert check_segment_video(video, expected_seconds=4.0) == []


def test_wrong_duration_is_error(tmp_path):
    """交错段 / 用了别的时长档 → 可证错误。"""
    video = _make_video(tmp_path / "seg.avi", 4.0)
    issues = check_segment_video(video, expected_seconds=6.0)
    assert "segment_video_duration_mismatch" in {i.code for i in issues}
    assert {i.severity for i in issues} == {"error"}


def test_out_of_range_duration_is_error(tmp_path):
    """3s 不在 H3 的 4–10 整数档内 → 复用既有 check_shot_duration 报错。"""
    video = _make_video(tmp_path / "seg.avi", 3.0)
    assert "duration_out_of_range" in {i.code for i in check_segment_video(video)}


def test_unmeasurable_video_is_warning_not_error(tmp_path):
    """读不到时长元数据只降级告警——测不出事实就不许阻断（可证性分级）。"""
    broken = tmp_path / "broken.avi"
    broken.write_bytes(b"not a video")
    issues = check_segment_video(broken, expected_seconds=4.0)
    assert issues and {i.severity for i in issues} == {"warning"}


def test_unexpected_size_is_warning(tmp_path):
    """尺寸不符是启发式（H3 输出尺寸是我们的假设，用户可以改工作流）→ warning。"""
    video = _make_video(tmp_path / "seg.avi", 4.0, size=(640, 360))
    issues = check_segment_video(video, expected_seconds=4.0)
    assert "segment_video_size" in {i.code for i in issues}
    assert {i.severity for i in issues} == {"warning"}


# ---------------------------------------------------------------------------
# 接线断言：写在调用方（向导的剥帧点），不是被测函数自身的单测
# ---------------------------------------------------------------------------

def test_v2_flow_blocks_on_bad_handoff_and_keeps_progress(tmp_path, monkeypatch, capsys):
    """交接校验不合格 → 硬阻断：不写下一段、不落盘进度、留给用户明确的出路。"""
    brief, session = _session(tmp_path)
    state = {"shot_table": "[Shot 1] 深夜空店内……"}
    bad_video = _make_video(tmp_path / "seg1.avi", 4.0)  # 交上来 4s，而段 1 计划 8s
    plans = [
        SegmentPlan(index=0, start_s=0, end_s=8, shots_in_segment=(1,),
                    summary="店员守店", end_hook="右手停在台面"),
        SegmentPlan(index=1, start_s=8, end_s=12, shots_in_segment=(2,),
                    summary="猫冲进店内", end_hook="猫落上柜台"),
    ]

    written: list[int] = []

    def fake_write(plan, all_plans, state_arg, brief_arg, llm):
        written.append(plan.index)
        return f"SEGMENT {plan.index + 1}"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: BRIDGE_DESC)
    answers = iter(["", str(bad_video), ""])
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers, ""))

    wizard._run_segmented_flow_v2(brief, session, plans, state, SimpleNamespace())

    assert written == [0], "闸门拦下后不得继续写下一段"
    assert not (session.directory / "segments" / "progress.json").exists(), \
        "被拦下的段不得被记成已完成（否则续跑直接跳过它）"
    out = capsys.readouterr().out
    assert "segment_video_duration_mismatch" in out, "必须当面报出可证错误，绝不静默"
    assert "[交接校验未通过]" in out and "出路：" in out, "必须给出明确话术与出路"
    assert "重新剥帧" in out, "出路必须是会话内可执行的（重跑向导会另开新 GEN，代价极高）"
    assert "第 1/2 段没有拿到输出视频" in out, "段号必须是 1-based，与同文件其他话术一致"


def test_v2_flow_passes_good_handoff_and_writes_progress(tmp_path, monkeypatch):
    """合规视频照常走完：闸门不得误伤（否则每段都要人工绕）。"""
    brief, session = _session(tmp_path)
    state = {"shot_table": "[Shot 1] 深夜空店内……"}
    plans = _plans2()
    video = _make_video(tmp_path / "seg1.avi", 4.0)

    written: list[int] = []

    def fake_write(plan, all_plans, state_arg, brief_arg, llm):
        written.append(plan.index)
        return f"SEGMENT {plan.index + 1}"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: BRIDGE_DESC)
    # 段1末确认("") → 段1视频路径 → 段2末确认("")（末段不剥帧）
    answers = iter(["", str(video), ""])
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers, ""))

    wizard._run_segmented_flow_v2(brief, session, plans, state, SimpleNamespace())

    assert written == [0, 1], "合规交接不得被拦"
    progress = json.loads((session.directory / "segments" / "progress.json").read_text(encoding="utf-8"))
    assert progress["done"] == 2


def test_v2_flow_prints_size_warning_and_continues(tmp_path, monkeypatch, capsys):
    """warning 档必须当面打出且**不阻断**（AC3 的调用方断言，不只函数级单测）。"""
    brief, session = _session(tmp_path)
    state = {"shot_table": "[Shot 1] 深夜空店内……"}
    plans = _plans2()
    odd_size = _make_video(tmp_path / "seg1.avi", 4.0, size=(640, 360))  # 时长对，尺寸不对

    written: list[int] = []

    def fake_write(plan, all_plans, state_arg, brief_arg, llm):
        written.append(plan.index)
        return f"SEGMENT {plan.index + 1}"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: BRIDGE_DESC)
    answers = iter(["", str(odd_size), ""])
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers, ""))

    wizard._run_segmented_flow_v2(brief, session, plans, state, SimpleNamespace())

    out = capsys.readouterr().out
    assert "segment_video_size" in out, "启发式档必须显式打出，绝不静默"
    assert written == [0, 1], "warning 不得阻断（可证性分级：测不出错就不许拦）"


def test_nan_duration_degrades_to_warning(tmp_path, monkeypatch):
    """fps 报 NaN 时 round(nan) 会抛 ValueError——调用点在剥帧 try 之外，会崩掉陪跑。

    （评审提出但未复现；这里直接钉住守卫：非有限值一律走「量不出来」的 warning 档。）
    """
    from minimax_h3_prompt.tools import preflight

    monkeypatch.setattr(preflight, "video_meta",
                        lambda path: {"frames": 96, "fps": float("nan"), "width": 1280,
                                      "height": 736, "duration_s": float("nan")})
    issues = preflight.check_segment_video(tmp_path / "any.avi", expected_seconds=6.0)
    assert issues, "NaN 时长必须显式降级告警"
    assert {i.severity for i in issues} == {"warning"}
    assert "segment_video_duration_mismatch" not in {i.code for i in issues}


def test_fallback_flow_blocks_and_does_not_mark_segment_done(tmp_path, monkeypatch, capsys):
    """回退路径同样接线：拦下后进度停在上一段，续跑会重跑被拦的那一段。"""
    brief, session = _session(tmp_path)
    state = {"shot_table": "[Shot 1] 深夜空店内……"}
    bad_video = _make_video(tmp_path / "seg1.avi", 4.0)  # 段 1 计划 6s（时间戳 0→6）

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: None)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: BRIDGE_DESC)

    rewritten: list[int] = []

    def fake_rewrite(segment, full_prompt, llm, **kwargs):
        rewritten.append(segment.shot_number)
        return f"REWRITTEN {segment.shot_number}"

    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", fake_rewrite)
    answers = iter(["", str(bad_video)])
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers, ""))

    wizard._run_segmented_flow(brief, session, TWO_SHOT_PROMPT, state=state)

    assert rewritten == [1], "第 2 段被拦下后不得继续重写"
    progress = json.loads((session.directory / "segments" / "progress.json").read_text(encoding="utf-8"))
    assert progress["done"] == 1, "进度不得把被拦下的第 2 段记成已完成"
    out = capsys.readouterr().out
    assert "segment_video_duration_mismatch" in out
    # 回退路径的 position 是 0-based，直接打出去会少一段（评审揪出的 off-by-one）
    assert "第 2/2 段没有拿到输出视频" in out, f"段号串了：{out[-400:]}"
