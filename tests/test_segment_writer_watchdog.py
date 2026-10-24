"""写段接线与逐段输出（issue #70 / ADR 0019）。

接缝：``segment_prompts`` 的四个写段函数（v2 写段 / 回退写段 / 多参 v2 写段 /
多参回退写段）与唯一产物出口 ``ref_segments.run_ref_segmentation``。

断言只落在**外部可见**的东西上：

- 调用次数与重试（旧代码遇到任何异常都直接 ``return None``，一次都不重试）；
- 打印行（失败带的病因与尝试次数、逐段落盘即出的横幅与正文）；
- 落盘文件（第 2 段开写时第 1 段文件必须已经在了——「不等全部写完」的判据）。

绝不发网络请求：假模型只实现 ``invoke(request, config=None)``，超时立即抛出。
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.observability import reporter
from minimax_h3_prompt.ref_segments import run_ref_segmentation
from minimax_h3_prompt.segment_prompts import (
    assemble_ref_segment,
    render_ref_segment_annotations,
    rewrite_ref_segment_prompt,
    rewrite_segment_prompt,
    split_shots_from_prompt,
    write_ref_segment_v2,
    write_segment_v2,
)
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.tools.h3_validator import split_ref_sections

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE_PROMPT = """[Shot 1] Live-action, the plane glides over a wheat field at dusk.
[Shot 2] At 00:05.000, the camera cuts to the plane diving through clouds.
[Shot 3] At 00:10.000, the shot cuts to the plane landing on a sill.

overall_soundscape: soft wind and distant birds.

non_diegetic_music: gentle piano.
"""


def _api_timeout() -> Exception:
    """首字节之前就卡住（openai 3.0.0 实测形态，判据见 issue #61）。"""
    import httpx
    from openai import APITimeoutError

    return APITimeoutError(
        request=httpx.Request("POST", "https://gw.example/v1/chat/completions")
    )


class ScriptedLLM:
    """按调用序分流：条目是字符串（正常回复）或异常实例（抛出）。

    ``config`` 一并记下来：走看门狗的路径必然带 ``callbacks``，直接 ``llm.invoke``
    不会带——这是「接线在不在」的旁证。
    """

    def __init__(self, replies: list):
        self._replies = list(replies)
        self.requests: list[str] = []
        self.configs: list[dict | None] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def invoke(self, request, config=None):
        self.requests.append(request)
        self.configs.append(config)
        index = min(len(self.requests), len(self._replies)) - 1
        reply = self._replies[index]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _v2_plans() -> list:
    return [
        SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                    summary="深夜便利店全景", end_hook="玻璃门刚被顶开窄缝"),
        SegmentPlan(index=1, start_s=4, end_s=10, shots_in_segment=(2,),
                    summary="橘猫进店跳上柜台", end_hook="猫前爪搭上柜台沿"),
    ]


def _good_segment_text() -> str:
    """官方正样本 fixture（4-10s 零 error），让重写循环一轮收工。"""
    return (FIXTURES / "official_shot01_i2va.md").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# 1. 四个写段函数全部走直调看门狗（票面 AC 1）
# ---------------------------------------------------------------------------

def test_v2_write_survives_first_byte_timeout_through_watchdog():
    """首字节前超时 → 看门狗重试一次 → 本段照常写出。

    旧实现异常一律 ``return None``：这条用例在当时会红（段被静默跳过）。
    """
    good = _good_segment_text()
    llm = ScriptedLLM([_api_timeout(), good])
    plans = _v2_plans()

    text = write_segment_v2(plans[1], plans, {"shot_table": "x"}, llm)

    assert text == good
    assert llm.calls == 2, "卡住后没有按看门狗重试"
    assert llm.requests[0] == llm.requests[1], "存活病重发必须逐字同一请求"
    assert llm.configs[0] and llm.configs[0].get("callbacks"), "调用没走直调入口"


def test_fallback_rewrite_survives_first_byte_timeout_through_watchdog():
    """回退写段同走看门狗：超时一次后重试成功，产出不被吞成 None。"""
    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = ScriptedLLM([_api_timeout(), "REWRITTEN"])

    text = rewrite_segment_prompt(segments[1], SAMPLE_PROMPT, llm, position_index=1)

    assert text == "REWRITTEN"
    assert llm.calls == 2
    assert llm.configs[0] and llm.configs[0].get("callbacks")


def test_all_four_write_functions_emit_watchdog_events():
    """四个入口都发同形等待事件（agent_start / first_byte）：裸 ``llm.invoke`` 不会发。

    变异检验：把任一函数改回 ``llm.invoke``，它那一组的角色事件就消失。
    """

    def behaviour(call_no, handler, request):
        handler.on_llm_new_token("段")
        return "正文"

    events: list[dict] = []
    reporter.subscribe(events.append)

    class Model:
        """只实现 run_direct_call 用到的 ``invoke(request, config=...)``。"""

        def invoke(self, request, config=None):
            callbacks = (config or {}).get("callbacks") or [None]
            return behaviour(1, callbacks[0], request)

    try:
        segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
        rewrite_segment_prompt(segments[0], SAMPLE_PROMPT, Model(), position_index=0)
        plans = _v2_plans()
        write_segment_v2(plans[0], plans, {"shot_table": "x"}, Model())
        rewrite_ref_segment_prompt(
            base_prompt=_WHOLE_12S, plan=_ref_plans()[0], total_segments=2,
            total_s=12, shot_texts=["[Shot 1] …"], llm=Model())
        write_ref_segment_v2(
            _ref_plans()[1], _ref_plans(), sections=split_ref_sections(_WHOLE_12S),
            shot_texts=["[Shot 2] …"], task_type="reference generation",
            llm=Model(), total_s=12)
    finally:
        reporter.unsubscribe(events.append)

    roles = {e["role"] for e in events if e.get("type") == "agent_start"}
    assert len(roles) == 4, f"四个写段入口没有各走一遍看门狗：{sorted(roles)}"
    assert all(r.startswith("role_segment") or r.startswith("role_ref_segment")
               for r in roles), f"角色名不像写段：{sorted(roles)}"
    assert any(e.get("type") == "agent_wait" and e.get("state") == "first_byte"
               for e in events)


# ---------------------------------------------------------------------------
# 2. 失败的那一段打印病因与尝试次数（票面 AC 3）
# ---------------------------------------------------------------------------

def test_v2_write_failure_prints_cause_and_attempts(capsys):
    """看门狗用尽重试：返回 None（调用方回退），但先打病因与已试次数。"""
    llm = ScriptedLLM([_api_timeout()])
    plans = _v2_plans()

    assert write_segment_v2(plans[1], plans, {"shot_table": "x"}, llm) is None

    out = capsys.readouterr().out
    assert "第 2 段" in out, "没点名是哪一段"
    assert "写段调用失败" in out
    assert f"已试 {model_factory._LLM_MAX_ATTEMPTS} 次" in out
    assert "卡住" in out, "病因没带到用户眼前"
    assert llm.calls == model_factory._LLM_MAX_ATTEMPTS


def test_fallback_rewrite_failure_prints_cause_and_attempts(capsys):
    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = ScriptedLLM([_api_timeout()])

    assert rewrite_segment_prompt(
        segments[1], SAMPLE_PROMPT, llm, position_index=1) is None

    out = capsys.readouterr().out
    assert "第 2 段回退写段调用失败" in out
    assert f"已试 {model_factory._LLM_MAX_ATTEMPTS} 次" in out


def test_ref_write_failure_falls_back_to_mechanical_but_says_why(capsys, tmp_path):
    """多参写段调用失败：回退机械拼装照旧，但病因与次数当面打出来。

    规划与写段共用同一个必超时的假模型：规划失败也回退机械拆段（#67 已覆盖），
    这里只圈「写段」那句。
    """
    llm = ScriptedLLM([_api_timeout()])
    result = run_ref_segmentation(
        whole_prompt=_WHOLE_12S, total_s=12.0, out_dir=tmp_path, llm=llm)

    out = capsys.readouterr().out
    assert "多参回退写段调用失败" in out
    assert f"已试 {model_factory._LLM_MAX_ATTEMPTS} 次" in out
    # 回退本身是对的：产物照样完整落盘
    assert len(result.texts) == len(result.plans)
    for text in result.texts:
        assert text.strip()


# ---------------------------------------------------------------------------
# 3. 每写完一段立刻落盘并打印（票面 AC 2）
# ---------------------------------------------------------------------------

def test_ref_segments_are_written_and_printed_one_by_one(tmp_path):
    """第 2 段开写的那一刻，第 1 段的文件与提示词已经出去了。

    判据取在两处（都在调用现场）：
    - 文件：第 2 次写段请求到达时 ``segment-01.md`` 已存在；
    - 打印：此刻的 stdout 里已经有第 1 段的正文。
    旧实现把 ``texts`` 攒在循环外、最后统一落盘——两条都会红。
    """
    plan_json = json.dumps({"segments": [
        {"index": 0, "start_s": 0, "end_s": 6, "shots_in_segment": [1],
         "summary": "A", "start_state": "The path is empty.", "end_hook": "She turns."},
        {"index": 1, "start_s": 6, "end_s": 12, "shots_in_segment": [2],
         "summary": "B", "start_state": "She holds the key.", "end_hook": "He bows."},
    ]})
    snapshots: list[dict] = []
    buf = io.StringIO()

    class Model:
        def __init__(self):
            self.write_calls = 0

        def invoke(self, request, config=None):
            if "分段规划师" in request:
                return plan_json
            self.write_calls += 1
            if self.write_calls == 2:
                snapshots.append({
                    "file_exists": (tmp_path / "segments" / "segment-01.md").is_file(),
                    "printed": buf.getvalue(),
                })
            marker = "FIRST-SEGMENT-BODY" if self.write_calls == 1 else "SECOND-SEGMENT-BODY"
            return _ref_doc(marker)

    with contextlib.redirect_stdout(buf):
        result = run_ref_segmentation(
            whole_prompt=_WHOLE_12S, total_s=12.0, out_dir=tmp_path, llm=Model())

    assert len(snapshots) == 1
    assert snapshots[0]["file_exists"], "第 1 段没落盘就开始写第 2 段（还是攒完一起出）"
    assert "FIRST-SEGMENT-BODY" in snapshots[0]["printed"], "第 1 段提示词没在开写第 2 段前打印"

    out = buf.getvalue()
    # 逐段都有步骤横幅（第几段/共几段）
    assert "▶ 正在写第 1/2 段" in out and "▶ 正在写第 2/2 段" in out
    # 正文按段落盘顺序出现在终端
    assert out.index("FIRST-SEGMENT-BODY") < out.index("SECOND-SEGMENT-BODY")
    for position in (1, 2):
        assert (tmp_path / "segments" / f"segment-{position:02d}.md").is_file()
    assert result.texts[0] == (tmp_path / "segments" / "segment-01.md").read_text(
        encoding="utf-8")


def test_segment_is_written_only_after_the_previous_one_passes(tmp_path, monkeypatch):
    """按需写段（issue #70 裁定）：段 2 的写段调用发生在段 1 交回通过之后。

    判据取在**写段调用现场**：第 2 次写段请求到达时，终端上必须已经有段 1 的通过行。
    这同时钉住两条票面要求——「轮到哪段才写哪段」（#55 AC1 不推翻）与「写完立刻落盘
    并打印」（正文出现在它自己的交回口之前）。

    走**向导真实入口**（``_deliver_ref_segments`` → ``_run_ref_segment_handoff``）：只打桩
    模型与交互，这样「交付阶段批量前置写完」那种回退会在准备口就被抓住。
    变异检验：把写段挪回准备阶段（``prepare_ref_segments`` 里先 ``write_all_ref_segments``），
    本用例立刻红。
    """
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.ui import wizard

    plan_json = json.dumps({"segments": [
        {"index": 0, "start_s": 0, "end_s": 6, "shots_in_segment": [1],
         "summary": "A", "start_state": "The path is empty.", "end_hook": "She turns."},
        {"index": 1, "start_s": 6, "end_s": 12, "shots_in_segment": [2],
         "summary": "B", "start_state": "She holds the key.", "end_hook": "He bows."},
    ]})
    buf = io.StringIO()
    snapshots: list[str] = []

    class Model:
        def __init__(self):
            self.writes = 0

        def invoke(self, request, config=None):
            if "分段规划师" in request:
                return plan_json
            self.writes += 1
            if self.writes == 2:
                snapshots.append(buf.getvalue())
            return _ref_doc(f"BODY-{self.writes}")

    model = Model()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    gen = tmp_path / "GEN001"
    gen.mkdir()
    brief = Brief(mode="ref", duration=12.0, style="写实", plot="红裙女孩递钥匙")
    job = wizard._deliver_ref_segments({}, brief, _WHOLE_12S, gen)

    videos: list[str] = []
    for index in (1, 2):
        path = tmp_path / f"seg{index}.mp4"
        path.write_bytes(b"video")
        videos.append(str(path))
    answers = iter(videos)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_capture_ref_segment_frames",
                        lambda *a, **k: ("first.png", "last.png", "首帧画面", "末帧画面"))
    monkeypatch.setattr(wizard, "_judge_ref_segment_semantics",
                        lambda **k: wizard.GATE_ACCEPT)

    with contextlib.redirect_stdout(buf):
        finished = wizard._run_ref_segment_handoff(job, gen)

    out = buf.getvalue()
    passed_line = "第 1/2 段已交回并通过可证层与语义层校验"
    assert finished is True
    assert len(snapshots) == 1, "段 2 的写段调用没发生"
    assert passed_line in snapshots[0], (
        "段 2 在段 1 通过之前就开写了（写段仍在交付阶段批量前置）")
    # 写完立刻出词：每段正文出现在它自己的交回口之前
    assert out.index("BODY-1") < out.index(passed_line) < out.index("BODY-2")
    # 每段都有步骤横幅（第几段/共几段）
    assert "▶ 正在写第 1/2 段" in out and "▶ 正在写第 2/2 段" in out
    for position in (1, 2):
        assert (gen / "segments" / f"segment-{position:02d}.md").is_file()


def test_single_segment_route_is_validated_with_the_direct_output_ruler(tmp_path):
    """≤15s 的续跑作业按**直出口径**校验：不许拿分段校验器报两条结构性假错。

    直出文本是整片六段式原样（不套分段注记），拿 ``validate_ref_segment`` 去判它，
    ``SEGMENT_TIMELINE_MISSING`` / ``SEGMENT_STATE_MISSING`` 是必挂的假错（issue #56
    复审栽过同一形态）。口径跟着 ``annotated`` 走，判据只住 ``validate_segment_text``。
    """
    from minimax_h3_prompt.ref_segments import load_ref_segments_for_resume
    from minimax_h3_prompt.segment_planner import SegmentPlan

    gen = tmp_path / "GEN001"
    (gen / "segments").mkdir(parents=True)
    plan = SegmentPlan(index=0, start_s=0, end_s=8, shots_in_segment=(1, 2),
                       summary="整片", end_hook="He bows.", start_state="She holds it.")
    (gen / "segments" / "plan.json").write_text(
        json.dumps([plan.to_dict()], ensure_ascii=False), encoding="utf-8")
    (gen / "video-prompt.md").write_text(_WHOLE_12S, encoding="utf-8")
    (gen / "segments" / "segment-01.md").write_text(_WHOLE_12S, encoding="utf-8")

    job = load_ref_segments_for_resume(gen, total_s=8.0)
    assert job.annotated is False
    _text, issues = job.write_segment(1)
    codes = {issue.code for issue in issues}
    assert not ({"SEGMENT_TIMELINE_MISSING", "SEGMENT_STATE_MISSING"} & codes), \
        f"直出文本被按分段口径判了假错：{sorted(codes)}"


def test_resume_job_reuses_written_segments_without_calling_the_model(tmp_path):
    """续跑（#58 语义保持）：已落盘的段直接复用——模型一次都不调。

    按需写段之后，续跑里「没轮到的段」本来就没有文件（正常形态），由陪跑现场写；
    有文件的段一律复用，不重付 LLM（重出还会与已通过的段脱节）。
    """
    from minimax_h3_prompt.ref_segments import load_ref_segments_for_resume

    gen = tmp_path / "GEN001"
    (gen / "segments").mkdir(parents=True)
    plans = _ref_plans()
    (gen / "segments" / "plan.json").write_text(
        json.dumps([plan.to_dict() for plan in plans], ensure_ascii=False),
        encoding="utf-8")
    (gen / "video-prompt.md").write_text(_WHOLE_12S, encoding="utf-8")
    (gen / "segments" / "segment-01.md").write_text("已落盘的旧词", encoding="utf-8")

    class RecordingModel:
        def __init__(self):
            self.calls = 0

        def invoke(self, request, config=None):
            self.calls += 1
            return _ref_doc("FRESH-DOC-BODY")

    model = RecordingModel()
    job = load_ref_segments_for_resume(gen, total_s=12.0, llm=model)

    text, _issues = job.write_segment(1)
    assert text == "已落盘的旧词"
    assert model.calls == 0, "续跑对已落盘的段还去调模型（重付 LLM）"
    # 没轮到的段照样能现场写：写它才会调模型（一次）
    fresh, _fresh_issues = job.write_segment(2)
    assert "FRESH-DOC-BODY" in fresh
    assert model.calls == 1


def test_revision_round_goes_through_watchdog_and_retries(capsys):
    """「修改提示词」一轮也是直调长调用：超时一次后重试成功（旧实现直接抛穿）。"""
    from minimax_h3_prompt.ref_segments import revise_ref_segment_body

    llm = ScriptedLLM([_api_timeout(), "The girl lowers the key onto the table."])

    new = revise_ref_segment_body(_ref_doc("BEFORE"), "罐子应该已经放下", llm)

    assert "lowers the key onto the table" in new
    assert llm.calls == 2, "改写没走看门狗（卡住后没有重试）"


def test_revision_round_failure_raises_cause_and_attempts():
    """用尽重试：抛出的文案必须带病因与已试次数——调用方把它原样打在用户面前。"""
    from minimax_h3_prompt.ref_segments import revise_ref_segment_body

    llm = ScriptedLLM([_api_timeout()])

    with pytest.raises(RuntimeError) as excinfo:
        revise_ref_segment_body(_ref_doc("BEFORE"), "罐子应该已经放下", llm)

    message = str(excinfo.value)
    assert f"已试 {model_factory._LLM_MAX_ATTEMPTS} 次" in message
    assert "卡住" in message


def test_heartbeat_is_not_printed_twice_under_the_callers_scope(tmp_path, capsys):
    """外层 step_scope 已订阅时，写段层不再开第二个作用域：同一事件只许打一行。

    两个调用方（向导 / --brief 快路径）都把整个交付包在 ``step_scope`` 里。写段层若
    再订阅一次，用户会看到每条心跳成双——像是重试了两次，实为同一条事件。
    """
    from minimax_h3_prompt.ui.progress import step_scope

    class Model:
        def invoke(self, request, config=None):
            callbacks = (config or {}).get("callbacks") or []
            if callbacks:
                callbacks[0].on_llm_new_token("段")
            if "分段规划师" in request:
                return json.dumps({"segments": [
                    {"index": 0, "start_s": 0, "end_s": 12, "shots_in_segment": [1, 2],
                     "summary": "A", "start_state": "The path is empty.",
                     "end_hook": "She turns."},
                ]})
            return _ref_doc("ONLY-SEGMENT")

    with step_scope("正在按镜头切点拆分执行段"):
        run_ref_segmentation(
            whole_prompt=_WHOLE_12S, total_s=12.0, out_dir=tmp_path, llm=Model())

    out = capsys.readouterr().out
    assert out.count("⏳ ref_segment_writer 已有响应") == 1, (
        f"写段心跳被打了两遍（写段层又开了一个 step_scope）：\n{out}")
    assert out.count("⏳ segment_planner 已有响应") == 1, (
        f"拆段心跳被打了两遍：\n{out}")


# ---------------------------------------------------------------------------
# 夹具：多参整片（12s，两镜）与一段合法的自包含六段式
# ---------------------------------------------------------------------------

_WHOLE_12S = """subject_definitions:
<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with long dark hair and a red dress.

summary:
[reference generation] The target video shows 红裙女孩 handing the bronze key over.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the red dress is retained.

detailed_description:
The target video is in a cinematic autumn style with warm soft lighting.
[Shot 1] A medium shot frames the garden path. 红裙女孩 walks in holding 青铜钥匙.
[Shot 2] At 00:06.000, the shot cuts to a close-up as she hands 青铜钥匙 to the boy.

overall_soundscape:
Soft outdoor ambience with light footsteps continues.

non_diegetic_music:
N/A
"""


def _ref_plans() -> list:
    return [
        SegmentPlan(index=0, start_s=0, end_s=6, shots_in_segment=(1,),
                    summary="女孩走到花园", start_state="The path is empty.",
                    end_hook="She turns the key over."),
        SegmentPlan(index=1, start_s=6, end_s=12, shots_in_segment=(2,),
                    summary="她递出钥匙", start_state="She holds the key.",
                    end_hook="He bows his head."),
    ]


def _ref_doc(marker: str) -> str:
    """写段打桩的正样本：合法六段式，marker 进 summary 便于按段辨认。"""
    return assemble_ref_segment(
        subject_definitions="PLACEHOLDER",
        retention_analysis="PLACEHOLDER",
        task_type="reference generation",
        summary_body=f"{marker} The girl hands the bronze key over.",
        detailed_body=(
            "The target video is in a cinematic autumn style with warm soft lighting.\n"
            "[Shot 1] A medium shot frames the garden path. 红裙女孩 walks in."),
        overall_soundscape="Soft outdoor ambience with light footsteps continues.",
        non_diegetic_music="N/A",
        annotations=render_ref_segment_annotations(
            1, 2, 0, 6, 12, "the path is empty", "he bows his head"),
    )
