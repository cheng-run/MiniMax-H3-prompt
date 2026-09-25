"""桥接帧读图接线（issue #12）：剥出的桥接帧必须在写本段提示词之前完成读图并进入 state。

缺陷根因有两层：
1. 提示词侧（``_frame_anchor_note``）：中间段拿不到桥接帧读图结果（由
   ``test_bridge_frame_anchor.py`` 覆盖）；
2. **接线侧（本文件）**：运行时根本没人去读桥接帧——``audit_frame_images`` 只读
   用户提交的首/尾帧，剥出的 ``bridge_frames/shot-NN-start.png`` 从未被读图；
   且两条分段路径都是「先写段、后剥帧」，写段 LLM 在时序上也不可能拿到读图结果。

修复契约：剥帧后立刻用视觉模型读图，写进 ``state["bridge_frame_descriptions"]``
（``{"segment": <0-based 段号>, "description": ...}``）；写段必须发生在其后
（v2 路径的预取因此推迟到剥帧之后——预取收益建立在「拿不到桥接帧」之上，
正确性优先）。读图失败/剥帧被跳过 → 诚实降级（无记录 + 显式警告，绝不静默）。
"""
import copy
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import segment_prompts, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.tools import frame_auditor, reference_auditor
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


def _session(tmp_path):
    brief = Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    return brief, session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_COMPLETED,
    )


def _plans3():
    return [
        SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,), summary="店员守店", end_hook="右手停在台面"),
        SegmentPlan(index=1, start_s=6, end_s=10, shots_in_segment=(2,), summary="猫冲进店内", end_hook="猫落上柜台"),
        SegmentPlan(index=2, start_s=10, end_s=18, shots_in_segment=(3, 4), summary="蹭手与抚摸", end_hook="手停在猫背上"),
    ]


def _silence(monkeypatch):
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")


# ---------------------------------------------------------------------------
# frame_auditor.describe_bridge_frame：桥接帧读图入口
# ---------------------------------------------------------------------------

def test_describe_bridge_frame_reads_image(tmp_path, monkeypatch):
    image = tmp_path / "bridge.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    seen = {}

    def fake_describe(path, instruction=None):
        seen["path"] = Path(path)
        seen["instruction"] = instruction
        return BRIDGE_DESC

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    assert frame_auditor.describe_bridge_frame(image) == BRIDGE_DESC
    assert seen["path"] == image
    assert seen["instruction"], "桥接帧读图必须用定制提示词（强调开场状态）"


def test_describe_bridge_frame_failure_degrades_loudly(tmp_path, monkeypatch, capsys):
    """读图失败只降级（返回 None），但必须打印警告——绝不静默失败。"""
    image = tmp_path / "bridge.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def boom(path, instruction=None):
        raise RuntimeError("vision model down")

    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    assert frame_auditor.describe_bridge_frame(image) is None
    assert "警告" in capsys.readouterr().out


def test_describe_bridge_frame_missing_file_fails_loudly(tmp_path, monkeypatch, capsys):
    image = tmp_path / "missing.png"  # 不存在

    monkeypatch.setattr(reference_auditor, "describe_image", lambda *a, **k: "不会到这里")
    assert frame_auditor.describe_bridge_frame(image) is None
    assert "警告" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# v2 路径接线：剥帧 → 读图 → 进 state → 才写段
# ---------------------------------------------------------------------------

def test_v2_flow_describes_bridge_frame_before_writing_segment(tmp_path, monkeypatch):
    """写段 2/3 的请求组装时，state 必须已携带对应段的桥接帧读图结果（时序断言）。"""
    brief, session = _session(tmp_path)
    state = {"shot_table": "[Shot 1] 深夜空店内……"}

    video = tmp_path / "prev-seg.mp4"
    video.write_bytes(b"fake video")

    def fake_extract(video_path, output_path):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return out

    monkeypatch.setattr(frame_auditor, "extract_last_frame", fake_extract)
    monkeypatch.setattr(reference_auditor, "describe_image", lambda path, instruction=None: BRIDGE_DESC)
    # 序列：段1末确认("") → 段1视频路径(为段2剥帧) → 段2末确认("")
    #       → 段2视频路径(为段3剥帧) → 段3末确认("")
    answers = iter(["", str(video), "", str(video), ""])
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers))

    snapshots: list[dict] = []

    def fake_write(plan, all_plans, state_arg, llm):
        snapshots.append(copy.deepcopy(state_arg))
        return f"SEGMENT {plan.index + 1}"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)

    wizard._run_segmented_flow_v2(session, _plans3(), state, SimpleNamespace())

    assert len(snapshots) == 3, "三段都应被写出"
    assert "bridge_frame_descriptions" not in snapshots[0], "段 1 没有桥接帧，不得伪造记录"
    assert snapshots[1].get("bridge_frame_descriptions") == [
        {"segment": 1, "description": BRIDGE_DESC},
    ], "写段 2 时必须已拿到段 2 桥接帧的读图结果"
    assert snapshots[2].get("bridge_frame_descriptions") == [
        {"segment": 1, "description": BRIDGE_DESC},
        {"segment": 2, "description": BRIDGE_DESC},
    ], "写段 3 时必须已拿到段 3 桥接帧的读图结果"


def test_v2_flow_without_bridge_video_degrades_without_record(tmp_path, monkeypatch, capsys):
    """拿不到输出视频：state 不加记录（绝不伪造），且流程**停在这里**。

    原契约是「跳过剥帧 → 降级照常走完」。ADR 0002 否掉了那个 opt-out：它等于闸门可被
    静默绕过，且下一段会带单图锚定行去声明一张并不存在的 Picture 1。现在非末段的视频
    是必经环节，给不出就停（进度不推进）。
    """
    brief, session = _session(tmp_path)
    _silence(monkeypatch)  # _prompt 恒返回 ""：用户拿不出视频

    snapshots: list[dict] = []

    def fake_write(plan, all_plans, state_arg, llm):
        snapshots.append(copy.deepcopy(state_arg))
        return f"SEGMENT {plan.index + 1}"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)

    wizard._run_segmented_flow_v2(session, _plans3(), {}, SimpleNamespace())

    assert len(snapshots) == 1, "拿不到视频就不得继续写下一段"
    assert all("bridge_frame_descriptions" not in s for s in snapshots), \
        "没有剥到帧时不得伪造桥接帧描述"
    assert "没有拿到输出视频" in capsys.readouterr().out


def test_v2_flow_reading_failure_degrades_loudly(tmp_path, monkeypatch, capsys):
    """读图炸了：流程继续（降级），但警告必须打出来——绝不静默。"""
    brief, session = _session(tmp_path)
    video = tmp_path / "prev-seg.mp4"
    video.write_bytes(b"fake video")

    def fake_extract(video_path, output_path):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return out

    monkeypatch.setattr(frame_auditor, "extract_last_frame", fake_extract)

    def boom(path, instruction=None):
        raise RuntimeError("vision model down")

    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    # 序列：段1末确认("") → 段1视频路径(为段2剥帧) → 段2末确认("")
    #       → 段2视频路径(为段3剥帧) → 段3末确认("")
    answers = iter(["", str(video), "", str(video), ""])
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers))

    snapshots: list[dict] = []

    def fake_write(plan, all_plans, state_arg, llm):
        snapshots.append(copy.deepcopy(state_arg))
        return f"SEGMENT {plan.index + 1}"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)

    wizard._run_segmented_flow_v2(session, _plans3(), {}, SimpleNamespace())

    assert len(snapshots) == 3, "读图失败不得中断分段流程"
    assert all(not s.get("bridge_frame_descriptions") for s in snapshots), "失败时不得写入半截描述"
    assert "警告" in capsys.readouterr().out, "读图失败必须当面示警"


# ---------------------------------------------------------------------------
# 回退路径（规划失败）同样要接线——改主路径就以为改完了的坑已踩过一次
# ---------------------------------------------------------------------------

def test_fallback_flow_describes_bridge_frame_before_rewriting(tmp_path, monkeypatch):
    """回退路径剥帧读图后，重写请求必须拿到对应段的桥接帧读图结果。"""
    from minimax_h3_prompt import model_factory, segment_planner

    brief, session = _session(tmp_path)
    state = {"shot_table": "[Shot 1] 深夜空店内……"}

    # 强制走回退路径（规划失败），绝不触发真实 LLM
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *args, **kwargs: None)

    video = tmp_path / "prev-seg.mp4"
    video.write_bytes(b"fake video")

    def fake_extract(video_path, output_path):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return out

    monkeypatch.setattr(frame_auditor, "extract_last_frame", fake_extract)
    monkeypatch.setattr(reference_auditor, "describe_image", lambda path, instruction=None: BRIDGE_DESC)
    # 序列：段 1 末确认("") → 段 2 剥帧视频路径 → 段 2 末确认("")
    answers = iter(["", str(video), ""])
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers))

    calls: list[dict] = []

    def fake_rewrite(segment, full_prompt, llm, **kwargs):
        calls.append({"shot_number": segment.shot_number,
                      "state": copy.deepcopy(kwargs.get("state")),
                      "position_index": kwargs.get("position_index")})
        return f"REWRITTEN {segment.shot_number}"

    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", fake_rewrite)

    wizard._run_segmented_flow(brief, session, TWO_SHOT_PROMPT, state=state)

    assert len(calls) == 2
    assert "bridge_frame_descriptions" not in calls[0]["state"], "段 1 没有桥接帧，不得伪造记录"
    assert calls[0]["position_index"] == 0
    assert calls[1]["state"].get("bridge_frame_descriptions") == [
        {"segment": 1, "description": BRIDGE_DESC},
    ], "回退路径重写段 2 时必须已拿到段 2 桥接帧的读图结果"
    assert calls[1]["position_index"] == 1, "回退路径也要按段号取桥接帧读图结果"
