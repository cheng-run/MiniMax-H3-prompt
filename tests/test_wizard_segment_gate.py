"""分段提示词的交付前校验接线。

bug（2026-09-22，GEN003 实测）：分段是唯一「产出即交付」的路径——``write_segment_v2``
不做任何校验，向导写完直接打印给用户去粘贴，validator 从没被调用过。
而 validator 本就能用真实产物把好坏分开（段 2/3 报 LAST_TIMESTAMP_TOO_CLOSE_TO_END，
段 1/4 干净）——规则在、接线不在。两条分段路径（规划式与回退式）都要接。
"""
import threading
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import model_factory, segment_prompts, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.ui import wizard

FIXTURES = Path(__file__).parent / "fixtures"

TWO_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _bad() -> str:
    """真实坏产物：GEN003 段 2。"""
    return (FIXTURES / "segment_absolute_timestamps_bad.md").read_text(encoding="utf-8")


def _good() -> str:
    """官方正样本（合规）。"""
    return (FIXTURES / "official_shot01_i2va.md").read_text(encoding="utf-8")


def _session(tmp_path):
    brief = Brief(mode="base", variant="I2VA", duration=10.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    return brief, session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_COMPLETED,
    )


def _plans():
    return [SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                        summary="深夜便利店全景，店员打盹", end_hook="玻璃门刚被顶开窄缝")]


def _silence(monkeypatch):
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)


def test_segmented_flow_surfaces_validator_errors(tmp_path, monkeypatch, capsys):
    """不合格提示词必须当面示警（含 issue code），不得静默交付给用户去喂 H3。"""
    brief, session = _session(tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: _bad())
    _silence(monkeypatch)

    wizard._run_segmented_flow_v2(session, _plans(), {}, SimpleNamespace())

    out = capsys.readouterr().out
    assert "LAST_TIMESTAMP_TOO_CLOSE_TO_END" in out, "不合格提示词被静默交付了"
    # 示警后不得再打「✓ 已生成，可立即复制到 H3」——那会当场抵消示警
    assert "可立即复制到 H3" not in out


def test_segmented_flow_quiet_on_compliant_segment(tmp_path, monkeypatch, capsys):
    """合规提示词不打扰用户（校验通过就不该出现告警块），且正常给 ✓。"""
    brief, session = _session(tmp_path)
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: _good())
    _silence(monkeypatch)

    wizard._run_segmented_flow_v2(session, _plans(), {}, SimpleNamespace())

    out = capsys.readouterr().out
    assert "校验未通过" not in out
    assert "校验提醒" not in out
    assert "可立即复制到 H3" in out


def test_fallback_flow_gates_output(tmp_path, monkeypatch, capsys):
    """回退路径（规划失败的降级路）同样是「产出即交付」，同样要过闸门。"""
    brief, session = _session(tmp_path)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: _bad())
    _silence(monkeypatch)

    # 无 shot_table → 规划不触发 → 走机械拆分 + 逐段重写的回退路径
    wizard._run_segmented_flow(brief, session, TWO_SHOT_PROMPT, state={})

    out = capsys.readouterr().out
    assert "校验未通过" in out, "回退路径把不合格提示词静默交付了"


def test_prefetch_exception_does_not_hang_the_flow(tmp_path, monkeypatch, capsys):
    """预取线程里的异常必须落进 prefetch[position]，否则 take() 会永远自旋。"""
    brief, session = _session(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("LLM 炸了")

    monkeypatch.setattr(segment_prompts, "write_segment_v2", boom)
    _silence(monkeypatch)

    finished: list[bool] = []
    thread = threading.Thread(
        target=lambda: (
            wizard._run_segmented_flow_v2(session, _plans(), {}, SimpleNamespace()),
            finished.append(True),
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    assert finished, "预取线程异常后向导卡死在 take() 的等待循环里"
    assert "生成失败，跳过" in capsys.readouterr().out
