"""长视频每一段都带上用户修订（issue #26）：分段两处请求 ＋ 写段前的并排核对块。

票面缺陷：分段写段请求收了一个**从未被引用**的 brief 参数、分段规划函数根本没有这个
入参——于是修订进不了任何一段，而长视频正是本项目的主用途。

本文件从**真实入口**（``_run_segmented_flow``，两条产出路径都在它里面分叉）驱动，
只打桩 LLM 与两个必经闸门，断言全落在外部产物上：真正发给模型的请求文本、
屏幕上打出来的核对块。
"""
from __future__ import annotations

from types import SimpleNamespace

from minimax_h3_prompt import model_factory, segment_planner, segment_prompts, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.ui import wizard

REVISION_TEXT = "汉服太朴素了，参考现代汉服，颜值高"
REVISION_PRIORITY_MARKER = "首帧读图结果 > 用户累积修订 > 分镜表"
FIRST_FRAME_DESC = "画面为竖构图：室内灯光明亮，一只橘色虎斑猫站在门口地砖上。"

# 回退路径（机械拆分）需要一条多镜头整片提示词
TWO_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _session(tmp_path):
    brief = Brief(mode="base", variant="I2VA", duration=12.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    return brief, session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_SEGMENTED_RUNNING,
    )


def _plans2():
    return [
        SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,),
                    summary="店员守店", end_hook="右手停在台面"),
        SegmentPlan(index=1, start_s=6, end_s=12, shots_in_segment=(2,),
                    summary="猫冲进店内", end_hook="猫落上柜台"),
    ]


def _state(**extra):
    """阶段 2 之后的 state：既带累积修订（真源），也带关键帧读图结果。"""
    state = {
        "shot_table": "[Shot 1] 深夜空店内……\n\n[Shot 2] At 00:06.000，橘猫顶开玻璃门……",
        "user_revisions": [{"round": 1, "layer": "frame", "text": REVISION_TEXT}],
        "fl2va_frame_descriptions": [{"role": "first", "description": FIRST_FRAME_DESC}],
    }
    state.update(extra)
    return state


def _stub_flow(monkeypatch):
    """放行两个必经闸门（桥接帧交接校验、末段尾帧核验）并挡住真实模型构造。

    本文件考的是**请求文本与屏幕核对块**，闸门行为由各自的专测文件承担。
    """
    monkeypatch.setattr(wizard, "_acquire_bridge_frame", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_verify_last_frame", lambda *a, **k: True)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    # 交互输入静音：段末「已生成好」确认（回车）与重显入口都不再读 stdin
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)


def test_v2_write_requests_carry_revisions_for_every_segment(tmp_path, monkeypatch):
    """v2 规划式路径：**每一段**真正发给模型的请求里都有修订与优先序。"""
    brief, session = _session(tmp_path)
    _stub_flow(monkeypatch)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans2())

    requests: list[str] = []

    def fake_write(plan, all_plans, state_arg, llm):
        # 用**真的**组装函数：断言落在真正喂给模型的文本上，而不是落在 state 上
        requests.append(segment_prompts.build_segment_v2_request(plan, all_plans, state_arg))
        return "SEGMENT"

    monkeypatch.setattr(segment_prompts, "write_segment_v2", fake_write)

    wizard._run_segmented_flow(brief, session, "prompt", None, _state())

    assert len(requests) == 2, "两段都应被写出"
    for index, request in enumerate(requests, 1):
        assert REVISION_TEXT in request, f"第 {index} 段写段请求没带用户修订"
        assert REVISION_PRIORITY_MARKER in request, f"第 {index} 段写段请求没带注入优先序"


def test_fallback_write_requests_carry_revisions_for_every_segment(tmp_path, monkeypatch):
    """规划失败回退的机械拆分路径同样要带——两条产出路径漏一条等于没修。"""
    brief, session = _session(tmp_path)
    _stub_flow(monkeypatch)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: None)

    requests: list[str] = []

    class _RecorderLLM:
        """只记录请求、不回话的假模型：真 ``rewrite_segment_prompt`` 会把它收到的请求交出来。"""

        def invoke(self, request):
            requests.append(request)
            return "REWRITTEN"

    # 先留住真函数再打桩：桩里再按模块名调它就成了自我递归
    real_rewrite = segment_prompts.rewrite_segment_prompt

    def fake_rewrite(segment, full_prompt, llm, **kwargs):
        return real_rewrite(segment, full_prompt, _RecorderLLM(), **kwargs)

    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", fake_rewrite)

    wizard._run_segmented_flow(brief, session, TWO_SHOT_PROMPT, None, _state())

    assert len(requests) == 2, "回退路径应拆出两段"
    for index, request in enumerate(requests, 1):
        assert REVISION_TEXT in request, f"回退路径第 {index} 段写段请求没带用户修订"
        assert REVISION_PRIORITY_MARKER in request, f"回退路径第 {index} 段没带注入优先序"


def test_planner_request_carries_revisions_through_the_real_call_site(tmp_path, monkeypatch):
    """接线断言：向导必须**真的**把累积修订交给分段规划（默认空参会让它静默无修订）。"""
    brief, session = _session(tmp_path)
    _stub_flow(monkeypatch)
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "SEGMENT")

    seen: list[str] = []

    def fake_plan(shot_table, total_s, llm, *, frame_context="", revision_context=""):
        seen.append(revision_context)
        return _plans2()

    monkeypatch.setattr(segment_planner, "plan_segments", fake_plan)

    wizard._run_segmented_flow(brief, session, "prompt", None, _state())

    assert seen, "分段规划没有被调用"
    assert REVISION_TEXT in seen[0], "向导没把累积修订交给分段规划"
    assert REVISION_PRIORITY_MARKER in seen[0], "交给规划层的修订块缺优先序声明"


def test_display_pairs_revisions_with_frame_read_before_writing(tmp_path, monkeypatch, capsys):
    """写段前把「累积修订」与「关键帧读图结果」并排摆一次，冲突由人自己发现。"""
    brief, session = _session(tmp_path)
    _stub_flow(monkeypatch)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans2())
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "SEGMENT")

    wizard._run_segmented_flow(brief, session, "prompt", None, _state())

    out = capsys.readouterr().out
    assert REVISION_TEXT in out, "累积修订没有当面摆出来"
    assert FIRST_FRAME_DESC in out, "首帧读图结果没有并排摆出来"
    # 摆出来的必须是**请求里那块**：含优先序声明，用户才知道冲突时系统会怎么裁
    assert REVISION_PRIORITY_MARKER in out, "核对块没告诉用户冲突时按什么裁定"
    assert out.index(REVISION_TEXT) < out.index("第 1/2 段"), "核对块必须在写第 1 段之前"


def test_no_revisions_means_no_check_block(tmp_path, monkeypatch, capsys):
    """没有修订就没有可冲突的对象：不打空块（否则每次分段都多一段噪声）。"""
    brief, session = _session(tmp_path)
    _stub_flow(monkeypatch)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans2())
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "SEGMENT")

    wizard._run_segmented_flow(brief, session, "prompt", None,
                               _state(user_revisions=[]))

    out = capsys.readouterr().out
    assert REVISION_TEXT not in out
    assert "写段前核对" not in out
