"""导演台路线入口（issue #101）：路线判据、模式选择与落会话状态。

父 spec #100 把三个缝定在：路线判据、产物级准备口、两个纯函数。本票只覆盖第一条
（路线判据）与「起步可选、选择落盘」——段规划、提示词方言、导演包是 #102–#104。

测试只断言外部可观察行为：判据返回值、Brief 字段、会话文件读回结果、向导问过什么。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from minimax_h3_prompt.brief_parser import (
    DIRECTOR_MODES,
    Brief,
    is_director_mode,
    validate_director_mode,
)
from minimax_h3_prompt.session_store import load_session, save_session
from minimax_h3_prompt.ui import wizard


# ---------------------------------------------------------------------------
# 缝 1：路线判据只有一处真源，大小写变体被拒绝
# ---------------------------------------------------------------------------

def test_is_director_mode_is_strict_single_source():
    """路线判据唯一真源（父 spec #100）：``is_director_mode`` 与 ``Brief.is_director`` 同义。

    只认精确小写 ``director``：``DIRECTOR`` / ``Director`` 是拼错的路线值，静默放宽
    等于把一条本该报错的路读成导演台——与 ``is_ref_mode`` 同一口径（父 spec #77）。
    """
    assert is_director_mode("director") is True
    for mode in ("base", "ref", "DIRECTOR", "Director", "", " director", "director ", "directors"):
        assert is_director_mode(mode) is False, f"{mode!r} 被误判为导演台路线"
    assert Brief(mode="director").is_director is True
    assert Brief(mode="ref").is_director is False
    assert Brief(mode="base").is_director is False


def test_director_modes_are_the_seven_official_values():
    """七种模式是路线契约（父 spec #100「七种生成模式之一」），序列本身是真源。"""
    assert DIRECTOR_MODES == ("t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v", "mixed")


def test_director_mode_value_rejects_case_variants_and_unknowns():
    """模式值同样是精确小写数据：拼错/大小写变体必须当场报错，不许静默回落。"""
    for mode in DIRECTOR_MODES:
        assert validate_director_mode(mode) == mode
    for bad in ("T2V", "T2v", "R2V", "MIXED", "", " t2v", "t2v ", "t2va", "fl2va", "xxx"):
        with pytest.raises(ValueError):
            validate_director_mode(bad)


# ---------------------------------------------------------------------------
# 缝 3：选择落进会话状态，重启后后续步骤仍读得到
# ---------------------------------------------------------------------------

def test_director_selection_survives_session_roundtrip(tmp_path):
    """路线与模式都活过重启（父 spec #100 AC2），且**只有 brief 一份**。

    ``brief.director_mode`` 是数据真源；``stage_state`` 里**刻意不存第二份**（与
    ``mode``/``variant`` 同族）——两处各存一份时，一次改写就能让向导读 brief、节点读
    state，各说各话且不报错。图节点读到的那份由 ``run_stage1`` 每次从 brief 现取。
    """
    generation_dir = tmp_path / "GEN001"
    brief = Brief(mode="director", variant="T2VA", director_mode="r2v",
                  duration=30.0, plot="主题")
    save_session(generation_dir, brief, {"director_mode": "r2v", "script": "分场剧本"})

    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.brief.mode == "director"
    assert loaded.brief.is_director is True
    assert loaded.brief.director_mode == "r2v"
    assert "director_mode" not in loaded.stage_state, \
        "stage_state 里落了一份与 brief 平行的模式副本——两处口径会各说各话"


def test_director_session_without_mode_is_loud(tmp_path):
    """会话文件写着 director 却没有模式 → 当场报错，不许当成「模式未知」继续跑。

    后续步骤按模式分派（#103 提示词方言、#102 段规划），模式为空意味着**没有**方言可
    选；静默回落任何一档都是拿用户没选过的口径出词。
    """
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, Brief(mode="base", variant="T2VA", plot="主题"), {})
    session_file = generation_dir / "session-state.json"
    session_file.write_text(
        session_file.read_text(encoding="utf-8").replace('"mode": "base"', '"mode": "director"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_session(generation_dir)


def test_director_session_with_bad_mode_value_is_loud(tmp_path):
    """模式值写脏（大小写变体）也必须报错：入口严格，读回同样严格。"""
    generation_dir = tmp_path / "GEN001"
    brief = Brief(mode="director", variant="T2VA", director_mode="r2v", plot="主题")
    save_session(generation_dir, brief, {})
    session_file = generation_dir / "session-state.json"
    session_file.write_text(
        session_file.read_text(encoding="utf-8").replace(
            '"director_mode": "r2v"', '"director_mode": "R2V"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_session(generation_dir)


def test_session_route_value_case_variant_is_loud(tmp_path):
    """路线值本身写脏（Director/DIRECTOR）同样不许静默：判据会把 ``Director`` 当成
    非导演台，随后按 base 读回——用户以为在跑导演台、实际在跑首尾帧链（issue #101 AC3）。
    """
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, Brief(mode="base", variant="T2VA", plot="主题"), {})
    session_file = generation_dir / "session-state.json"
    session_file.write_text(
        session_file.read_text(encoding="utf-8").replace('"mode": "base"', '"mode": "Director"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_session(generation_dir)


# ---------------------------------------------------------------------------
# 缝 2：选择进得了图 state（LangGraph 只搬运 schema 声明过的键）
# ---------------------------------------------------------------------------

def test_director_mode_is_declared_in_state_and_not_duplicated_in_persistence():
    """state 声明必须有它（节点读得到）；持久化白名单**必须没有**它（brief 是唯一副本）。"""
    from minimax_h3_prompt import session_store
    from minimax_h3_prompt.graph.state import PipelineState

    assert "director_mode" in PipelineState.__annotations__, \
        "PipelineState 没声明 director_mode——LangGraph 会静默把它丢掉"
    assert "director_mode" not in session_store._STATE_KEYS, \
        "白名单里又抄了一份模式——同一契约两处口径（见 save_session 注释）"


def test_run_stage1_feeds_director_mode_into_the_graph(tmp_path, monkeypatch):
    """``run_stage1`` 的初始 state 必须带着选定的模式（父 spec #100 AC2「后续步骤可读到」）。

    图只搬运 ``PipelineState`` 声明过的键；用一张记录初始 state 的假图钉住「喂进去了
    没有」，不跑任何 LLM。
    """
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.graph import pipeline

    captured: dict = {}

    class _RecordingGraph:
        def stream(self, initial, stream_mode="updates"):
            captured.update(initial)
            return iter(())

    monkeypatch.setattr(pipeline, "build_pipeline_graph",
                        lambda *a, **k: (_RecordingGraph(), {}))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path)

    config = SimpleNamespace(common_sense_qa=False)
    brief = Brief(mode="director", variant="T2VA", director_mode="r2v", plot="主题")
    pipeline.run_stage1(brief, config)

    assert captured["mode"] == "director"
    assert captured["director_mode"] == "r2v"


# ---------------------------------------------------------------------------
# 缝 4：起步可选导演台路线与七种模式之一
# ---------------------------------------------------------------------------

def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(prompts)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def test_choose_generation_mode_accepts_director_route(monkeypatch):
    """方式菜单加第五项导演台；前四项（含回车默认首尾帧）一字不变。"""
    _fake_inputs(monkeypatch, ["5"])
    assert wizard._choose_generation_mode() == "DIRECTOR"
    for raw, expected in (("1", "I2VA"), ("2", "L2VA"), ("3", "FL2VA"), ("4", "REF2VA"), ("", "FL2VA")):
        _fake_inputs(monkeypatch, [raw])
        assert wizard._choose_generation_mode() == expected


def test_choose_director_mode_covers_the_seven_modes(monkeypatch):
    """七种模式都能选到、编号与 ``DIRECTOR_MODES`` 同序；空输入与坏输入重问，不默认。"""
    for index, mode in enumerate(DIRECTOR_MODES, 1):
        _fake_inputs(monkeypatch, [str(index)])
        assert wizard._choose_director_mode() == mode
    _fake_inputs(monkeypatch, ["", "9", "7"])
    assert wizard._choose_director_mode() == "mixed"


def test_director_mode_labels_derive_from_the_constant():
    """菜单文案的键集合必须等于 ``DIRECTOR_MODES``（守卫形态从常量派生）。

    少写一条时菜单只会少一行、编号整体前移——用户以为选的是 i2v、实际拿到 fl2v，
    而且没有任何断言会红。这里让「常量加了值、文案没跟」当场变红。
    """
    assert set(wizard._DIRECTOR_MODE_LABELS) == set(DIRECTOR_MODES)



def test_brief_file_entry_refuses_director_route():
    """brief 文件入口本票不开：读到 director 当场报错，不许静默按 base 跑。

    文件里没有「选模式」的问答位，`--brief` 快路径也没有打包/陪跑接线（#104–#108）。
    静默接受会让一条导演台 brief 走进首尾帧链，且到阶段 2 才露馅——本仓「规则在、
    接线不在」的典型形态。
    """
    from minimax_h3_prompt.brief_parser import parse_brief_text

    for mode_line in ("模式: director", "模式: DIRECTOR", "模式: Director"):
        with pytest.raises(ValueError):
            parse_brief_text(f"{mode_line}\n## 剧情\n雨夜旧信")



TOPIC = "雨夜旧信，青年在便利店门口拆开信封。"


def _cfg(tmp_path, **overrides):
    base = dict(default_duration=5.0, default_language="Chinese",
                sessions_root=tmp_path / "sessions", max_clarification_rounds=0)
    base.update(overrides)
    return SimpleNamespace(**base)


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _stub_stage1(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage1(brief, config, **kwargs):
        if captured is not None:
            captured["brief"] = brief
        return {"script": "分场剧本"}, object(), {"prompt_engineer": object()}

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)


def test_phase1_new_director_route_records_route_and_mode(tmp_path, monkeypatch):
    """选导演台 + r2v：brief 落 mode/director_mode，会话文件读回来仍是这两个值。

    variant 必须是 T2VA 占位：导演台模式与帧变体是两套口径，带着 FL2VA 之类进管道
    会让老帧链判据（``FRAME_VARIANTS``）把导演台会话读成首尾帧（ADR 0024）。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    captured: dict = {}
    _stub_stage1(monkeypatch, captured)
    # 主题 / 时长 / 风格 / 方式 5=导演台 / 模式 4=r2v
    _fake_inputs(monkeypatch, [TOPIC, "", "", "5", "4"])

    session = wizard._phase1_new(_cfg(tmp_path))

    assert captured["brief"].mode == "director"
    assert captured["brief"].director_mode == "r2v"
    assert captured["brief"].variant == "T2VA"
    assert session is not None
    loaded = load_session(session.directory)
    assert loaded is not None
    assert loaded.brief.mode == "director"
    assert loaded.brief.director_mode == "r2v"


def test_run_wizard_director_does_not_enter_base_stage2(tmp_path, monkeypatch, capsys):
    """选完导演台后不得落进首尾帧链的阶段 2（本票唯一的自加护栏）。

    `run_wizard` 原先只认 ref 一个分叉，第三条路线会静默掉进 base 的收帧/出词流程。
    #102 起这个分叉改走 `_run_director_flow`（段规划 → director/plan.json → 出站段表），
    这里只钉**路由**：导演台会话必须交给它，绝不给另一条路线的阶段 2。
    """
    from minimax_h3_prompt.session_store import STATUS_AWAITING_FRAMES, SessionState

    session = SessionState(
        directory=tmp_path / "GEN001",
        brief=Brief(mode="director", variant="T2VA", director_mode="t2v", plot=TOPIC),
        stage_state={}, status=STATUS_AWAITING_FRAMES,
    )
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_resume_awaiting_frames", lambda config, s: s)

    def _boom(*a, **k):
        raise AssertionError("导演台会话落进了首尾帧链的阶段 2")

    monkeypatch.setattr(wizard, "_phase2_collect_and_finish", _boom)
    monkeypatch.setattr(wizard, "_phase2_ref_finish", _boom)
    routed: list = []
    monkeypatch.setattr(wizard, "_run_director_flow",
                        lambda s: routed.append(s) or 0)

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    assert routed and routed[0] is session
    capsys.readouterr()


def test_resume_director_session_skips_base_revision_loop(tmp_path, monkeypatch):
    """重启续接导演台会话时，两条续接入口都不许走进 base 的人机修改循环。

    否则停止点在「阶段 1 完成、等帧图」的导演台会话会被当成首尾帧链续接：补展
    fl2va 提示词、要用户交帧图——正是父 spec #100 风险清单里「拿另一条路线跑它」。
    """
    from minimax_h3_prompt.graph.pipeline import _STAGE1_CHAIN
    from minimax_h3_prompt.session_store import (
        STATUS_AWAITING_FRAMES, STATUS_STAGE1_RUNNING, load_session, save_session,
    )

    def _boom(*a, **k):
        raise AssertionError("导演台会话走进了 base 的人机修改循环")

    monkeypatch.setattr(wizard, "_user_revision_loop", _boom)

    # 入口一：阶段 1 已完成、等帧图（`_resume_awaiting_frames`）
    awaiting_dir = tmp_path / "awaiting" / "GEN001"
    save_session(awaiting_dir,
                 Brief(mode="director", variant="T2VA", director_mode="i2v", plot=TOPIC),
                 {}, status=STATUS_AWAITING_FRAMES)
    assert wizard._resume_awaiting_frames(_cfg(tmp_path), load_session(awaiting_dir)) is not None

    # 入口二：阶段 1 中途中断、节点已全部跑完只差落盘（`_resume_stage1` 的收尾格）
    running_dir = tmp_path / "running" / "GEN001"
    save_session(running_dir,
                 Brief(mode="director", variant="T2VA", director_mode="fl2v", plot=TOPIC),
                 {"_progress": {"last_completed_node": _STAGE1_CHAIN[-1]}},
                 status=STATUS_STAGE1_RUNNING)
    assert wizard._resume_stage1(_cfg(tmp_path), load_session(running_dir)) is not None

