"""开场首帧读图的三处 CLI 调用点（#323）。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. ``ui.wizard_director.prepare_opening_still_description``——无图或模式没有首帧槽
   返回 ``(True, "")`` 且不读图；有图读一次并返回描述；读图失败打印图路径与病因并
   返回 ``(False, "")``；
2. 收图之后**立刻**读一次，并把「这张图被读懂成了什么」打给用户
   （``[已读图] 开场首帧画面：…``，与全片尾帧同一处手感）；
3. 续跑跳过收集步骤时，规划前仍取到描述（命中缓存不重读），规划请求与开场段写段
   请求带同一份描述；
4. 改词请求带同一份描述；
5. 读图失败停在规划/写段之前（不调模型），收集处则返回 ``False`` 停下。

视觉调用一律打桩（照抄 ``tests/test_director_film_end.py`` 的 ``_patch_describe``）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import SessionState
# 与全片尾帧共用同一份流程桩（记录型模型、续跑会话、打包与陪跑的替身）。
from test_director_film_end import (
    _RecordLLM, _resumed_fl2v_session, _stub_flow_tail)

# 断言用的描述：开场首帧的读图结果，与全片尾帧那份刻意不同——两个 sidecar 落在同一
# 棵树里，字符串相同就分不清「哪份描述进了哪个请求」。
_OPENING = "一个男人背对镜头立在门前，右侧是旧木格长窗，桌上没有杯子。"
_FILM_END = "男人已经走进院门，院门在身后关上。"


def _session(tmp_path, mode="fl2v", duration=20.0):
    directory = tmp_path / "GEN001"
    directory.mkdir(parents=True, exist_ok=True)
    return SessionState(
        directory=directory,
        brief=Brief(mode="director", variant="T2VA", director_mode=mode,
                    duration=duration, plot="快过年返乡", language="Chinese"),
        stage_state={}, status="awaiting_frames")


def _start_image(session_dir):
    path = session_dir / "director" / "assets" / "01" / "start.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"opening-still-bytes")
    return path


def _start_sidecar(session_dir):
    return session_dir / "director" / "assets" / "01" / "start.meta.json"


def _patch_describe(monkeypatch, behaviour):
    """打桩唯一视觉调用口；返回记录每次调用路径的列表。"""
    from minimax_h3_prompt.tools import reference_auditor

    calls: list = []

    def fake_describe(path, instruction=None):
        calls.append(path)
        return behaviour(path)

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return calls


def _by_image(path):
    """按图分派描述：全片尾帧与开场首帧同时被读时也分得清。"""
    return _FILM_END if "film-end" in str(path) else _OPENING


# ---------------------------------------------------------------------------
# 统一出口：无图/无首帧槽不读；有图读一次；失败打印路径与病因
# ---------------------------------------------------------------------------

def test_prepare_returns_empty_without_reading_when_there_is_no_image(
        tmp_path, monkeypatch):
    """盘上没有开场首帧图：返回 ``(True, "")``，不调视觉模型、不落盘。"""
    from minimax_h3_prompt.ui.wizard_director import (
        prepare_opening_still_description,
    )

    session = _session(tmp_path)
    calls = _patch_describe(monkeypatch, _by_image)

    assert prepare_opening_still_description(session.directory, "fl2v") == (True, "")
    assert calls == [], "没有图就不许读图"
    assert not _start_sidecar(session.directory).exists()


@pytest.mark.parametrize("mode", ["t2v", "r2v"])
def test_prepare_does_not_read_for_modes_without_a_first_frame_slot(
        tmp_path, monkeypatch, mode):
    """模式没有首帧槽：盘上就是有张闲置的 start 图也不读（判据问给图契约）。"""
    from minimax_h3_prompt.ui.wizard_director import (
        prepare_opening_still_description,
    )

    session = _session(tmp_path, mode=mode)
    _start_image(session.directory)
    calls = _patch_describe(monkeypatch, _by_image)

    assert prepare_opening_still_description(session.directory, mode) == (True, "")
    assert calls == [], "无首帧槽的模式不许读图"
    assert not _start_sidecar(session.directory).exists()


@pytest.mark.parametrize("mode,expected", [
    ("t2v", False), ("r2v", False), ("i2v", True), ("fl2v", True)])
def test_facts_flag_a_start_frame_only_where_the_contract_has_a_first_slot(
        tmp_path, monkeypatch, mode, expected):
    """``start_frame`` 与 ``film_end`` 同一口径：无首帧槽的模式盘上残留 start 图也不置真。

    只判盘面不看模式时，t2v/r2v 会把一张本模式挂不上的图当成「本段有首帧图」，
    给图行与起点块在同一份请求里互相打架（spec #317 a-2）。
    """
    from minimax_h3_prompt.ui.wizard_director import prepare_director_still_facts

    session = _session(tmp_path, mode=mode)
    _start_image(session.directory)
    calls = _patch_describe(monkeypatch, _by_image)

    facts = prepare_director_still_facts(session.directory, mode)

    assert facts.ok
    assert facts.start_frame is expected
    if expected:
        assert facts.start_frame_description == _OPENING
        assert calls, "有首帧槽的模式应真的读一次图"
    else:
        assert facts.start_frame_description == ""
        assert calls == [], "无首帧槽的模式不许读图，更不该把这张图当成事实"


def test_prepare_reads_the_image_once_and_cache_holds_on_the_second_call(
        tmp_path, monkeypatch):
    """有图：读一次并返回描述；第二次（续跑/改词）命中 sidecar，不重复付费。"""
    from minimax_h3_prompt.ui.wizard_director import (
        prepare_opening_still_description,
    )

    session = _session(tmp_path)
    image = _start_image(session.directory)
    calls = _patch_describe(monkeypatch, _by_image)

    assert prepare_opening_still_description(session.directory, "fl2v") == (
        True, _OPENING)
    assert calls == [image], "要读的正是盘上那张开场首帧图"
    assert _start_sidecar(session.directory).is_file()

    def _boom(path):
        raise AssertionError("图没换又读了图：续跑重复付费")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", _boom)
    assert prepare_opening_still_description(session.directory, "fl2v") == (
        True, _OPENING)


def test_prepare_reports_the_image_path_and_cause_when_reading_fails(
        tmp_path, monkeypatch, capsys):
    """读图失败：返回 ``(False, "")``，打印里指明哪张图、什么病因，且不落盘。"""
    from minimax_h3_prompt.ui.wizard_director import (
        prepare_opening_still_description,
    )

    session = _session(tmp_path)
    image = _start_image(session.directory)

    def fail(path):
        raise RuntimeError("视觉网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)

    assert prepare_opening_still_description(session.directory, "fl2v") == (False, "")
    out = capsys.readouterr().out
    assert str(image) in out, "失败时要打印是哪张图"
    assert "视觉网关 502" in out, "原始病因不能被吞"
    assert not _start_sidecar(session.directory).exists(), "失败不许落 sidecar"


# ---------------------------------------------------------------------------
# 收图之后：立刻读一次并打给用户；失败停下
# ---------------------------------------------------------------------------

def _patch_collect_inputs(monkeypatch, answers, confirms=()):
    from minimax_h3_prompt.ui import wizard

    queue = list(answers)
    confirm_queue = list(confirms)
    monkeypatch.setattr(wizard, "_prompt", lambda text="": queue.pop(0))
    monkeypatch.setattr(wizard, "_confirm",
                        lambda text="", default=False: confirm_queue.pop(0))
    monkeypatch.setattr(wizard, "_user_revision_loop", lambda *a, **k: "")


def test_collecting_reads_the_opening_still_right_away_and_prints_it(
        tmp_path, monkeypatch, capsys):
    """收图之后立刻读一次并打印 ``[已读图] 开场首帧画面：…``（与全片尾帧同一手感）。"""
    from minimax_h3_prompt.ui import wizard

    session = _session(tmp_path)
    first = tmp_path / "first.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    _patch_collect_inputs(monkeypatch, [str(first), ""], confirms=[True])
    calls = _patch_describe(monkeypatch, _by_image)

    assert wizard._collect_director_opening_still(session, config=None) is True
    assert calls == [session.directory / "director" / "assets" / "01" / "start.png"]
    out = capsys.readouterr().out
    assert f"[已读图] 开场首帧画面：{_OPENING}" in out, \
        "读到的画面要当场打给用户（他据此判断要不要换图）"
    assert _start_sidecar(session.directory).is_file()


def test_collecting_stops_when_the_opening_still_cannot_be_read(
        tmp_path, monkeypatch, capsys):
    """读图失败：收集处返回 False 停下，提示指明图、病因与「重跑会回到这一步」。"""
    from minimax_h3_prompt.ui import wizard

    session = _session(tmp_path)
    first = tmp_path / "first.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    _patch_collect_inputs(monkeypatch, [str(first), ""], confirms=[True])

    def fail(path):
        raise RuntimeError("视觉网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)

    assert wizard._collect_director_opening_still(session, config=None) is False
    out = capsys.readouterr().out
    assert "start.png" in out, "失败时要指明是哪张图"
    assert "视觉网关 502" in out
    assert "重跑向导会回到这一步" in out, "要告诉用户修好后重跑能回到这一步"
    assert not _start_sidecar(session.directory).exists(), "失败不许落 sidecar"


# ---------------------------------------------------------------------------
# 规划前：续跑补读 / 命中缓存；规划与写段带同一份描述；失败停在规划之前
# ---------------------------------------------------------------------------


def test_wizard_flow_reads_the_opening_still_before_planning_on_resume(
        tmp_path, monkeypatch):
    """续跑跳过收集步骤时，规划前补读一次；规划与开场段写段请求带同一份描述。"""
    from minimax_h3_prompt.ui import wizard

    session = _resumed_fl2v_session(tmp_path)
    llm = _RecordLLM()
    _patch_describe(monkeypatch, _by_image)
    _stub_flow_tail(monkeypatch, llm)

    assert wizard._run_director_flow(session) == 0

    planner_request = next(r for r in llm.requests if "分段规划师" in r)
    assert _OPENING in planner_request, "规划请求没带开场首帧的画面事实"
    assert "以画面为准" in planner_request
    opening_request = next(r for r in llm.requests if "Segment 1/3" in r)
    assert _OPENING in opening_request, "开场段的写段请求没带起点画面描述"
    # 同一份描述：规划与写段读到的是同一段文字（不是各读一次各写一份）。
    assert planner_request.count(_OPENING) == opening_request.count(_OPENING) == 1
    for request in llm.requests:
        if "Segment 2/3" in request or "Segment 3/3" in request:
            assert _OPENING not in request, "非开场段不该带起点画面约束"


def test_wizard_flow_reuses_the_cached_opening_description_on_resume(
        tmp_path, monkeypatch):
    """续跑时 sidecar 已在：规划前命中缓存，**不重复读图**，请求里仍带同一份。"""
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )
    from minimax_h3_prompt.ui import wizard

    session = _resumed_fl2v_session(tmp_path)
    _patch_describe(monkeypatch, _by_image)
    # 先按真实路径落一次 sidecar（相当于上一轮收集时已经读过）。
    assert get_opening_still_description(session.directory) == _OPENING

    def fake_describe(path):
        assert "film-end" in str(path), "开场首帧命中缓存后不该再读第二遍"
        return _FILM_END

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fake_describe)
    llm = _RecordLLM()
    _stub_flow_tail(monkeypatch, llm)

    assert wizard._run_director_flow(session) == 0
    planner_request = next(r for r in llm.requests if "分段规划师" in r)
    assert _OPENING in planner_request


def test_wizard_flow_stops_before_planning_when_the_opening_read_fails(
        tmp_path, monkeypatch, capsys):
    """开场首帧读图失败：停在段规划之前——不调模型、不落段表（全片尾帧照常）。"""
    from minimax_h3_prompt.director.planning import plan_path
    from minimax_h3_prompt.ui import wizard

    session = _resumed_fl2v_session(tmp_path)

    def fail(path):
        if "film-end" in str(path):
            return _FILM_END
        raise RuntimeError("视觉网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)
    llm = _RecordLLM()
    _stub_flow_tail(monkeypatch, llm)

    assert wizard._run_director_flow(session) == 1
    out = capsys.readouterr().out
    assert "开场首帧图读图失败" in out and "视觉网关 502" in out
    assert str(session.directory / "director" / "assets" / "01" / "start.png") in out
    assert llm.requests == [], "读图失败后不许再调模型（不规划、不写段）"
    assert not plan_path(session.directory).exists()
    assert not _start_sidecar(session.directory).exists()


def test_wizard_flow_does_not_read_the_opening_still_for_t2v(
        tmp_path, monkeypatch):
    """无首帧槽的模式：盘上留着 start 图也不读、不打印、不报错（规划照常跑完）。

    ``get_opening_still_description`` 一旦被调用就报红——三处调用点共用同一个出口，
    出口被模式闸挡住，整条流程就都不读。
    """
    from test_director_prompts import (
        _DirectorFlowLLM, _director_session, _PLAN_JSON)
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.director.planning import parse_director_plan
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    session.directory.mkdir(parents=True, exist_ok=True)
    _start_image(session.directory)

    def _explode(*args, **kwargs):
        raise AssertionError("t2v 没有首帧槽却读了开场首帧图")

    monkeypatch.setattr(
        "minimax_h3_prompt.director.opening_still.get_opening_still_description",
        _explode)
    plan = parse_director_plan(_PLAN_JSON, 30.0, mode="t2v")
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: _DirectorFlowLLM(plan))
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_flow(session) == 0


# ---------------------------------------------------------------------------
# 写段前（改词）：带同一份描述；失败返回 retry、原词不动
# ---------------------------------------------------------------------------

def _rewrite_session(tmp_path):
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from test_director_companion import _director_brief, _plan_types

    directory = tmp_path / "GEN001"
    plan = _plan_types(("opening",), mode="i2v")
    write_plan_json(plan_path(directory), plan)
    path = segment_path(directory, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("段 1 的提示词\n", encoding="utf-8")
    _start_image(directory)
    session = SessionState(
        directory=directory,
        brief=_director_brief(director_mode="i2v", duration=4.0,
                              director_node_id="365", director_cache_name=""),
        stage_state={}, status="awaiting_frames")
    return plan, session


def test_rewrite_request_carries_the_opening_description(tmp_path, monkeypatch):
    """按 e 改词也带同一份起点画面描述，改一次不把它改丢。"""
    from test_director_companion import _REWRITTEN_BODY, _RewritingLLM, _run_config
    from minimax_h3_prompt.ui import wizard

    plan, session = _rewrite_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    _patch_describe(monkeypatch, _by_image)
    llm = _RewritingLLM(_REWRITTEN_BODY)

    wizard._rewrite_director_segment_round(
        plan, plan.segments[0], session, config=config, llm=llm)

    assert llm.requests, "改词出口没有发出请求"
    assert _OPENING in llm.requests[0], "改词请求没带开场首帧的画面事实"
    assert "以画面为准" in llm.requests[0]


def test_rewrite_stops_before_calling_the_model_when_the_opening_read_fails(
        tmp_path, monkeypatch, capsys):
    """改词前读图失败：返回 retry、模型一次都没调、段文件保持原样。"""
    from test_director_companion import _REWRITTEN_BODY, _RewritingLLM, _run_config
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.ui import wizard

    plan, session = _rewrite_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def fail(path):
        raise RuntimeError("视觉网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)
    llm = _RewritingLLM(_REWRITTEN_BODY)

    outcome = wizard._rewrite_director_segment_round(
        plan, plan.segments[0], session, config=config, llm=llm)

    assert outcome == "retry"
    assert llm.requests == [], "读图失败后不许再调模型"
    assert segment_path(session.directory, 1).read_text(encoding="utf-8") == "段 1 的提示词\n"
    out = capsys.readouterr().out
    assert "视觉网关 502" in out and "start.png" in out
