"""直调等待看门狗入口（issue #67 / ADR 0019）。

接缝只有 ``agents.run_direct_call(llm, request, role=...)``：一次直接模型调用，
外加与 ``run_agent`` **同形**的等待事件（首字节 / 生成中 / 卡住 / 重试）。

为什么要这个入口：拆段/写段用同一个流式客户端却**绕过 run_agent**，没有首字节
事件、没有重试、没有预算，异常被吞成 ``None`` 后静默降级（ADR 0019 的根因）。
所以本文件的断言都落在**外部可见**的三样东西上：

- 返回结果（``ok`` / ``text`` / ``error`` / ``attempts``）——失败不再返回 ``None``；
- 事件序列（记录型回调抓全局事件总线）；
- 成本表（直调路径的 token 进账，ADR 0019「后果」第一条）。

绝不发网络请求：假模型只实现 ``invoke(request, config=None)``，等待都在毫秒级。
"""
from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage

from minimax_h3_prompt import agents as agents_module
from minimax_h3_prompt import model_factory
from minimax_h3_prompt.agents import run_direct_call
from minimax_h3_prompt.config import ModelSettings, config
from minimax_h3_prompt.observability import reporter, token_meter

ROLE = "role_segment_planner"


class TimeoutException(Exception):
    """模仿 httpx / vendored httpx2 的基类名（判据按 MRO 里的类名咬，issue #61）。"""


class FakeReadTimeout(TimeoutException):
    """首字节之后流到一半卡住。"""


def _api_timeout() -> Exception:
    """首字节之前就卡住（openai 3.0.0 实测形态）。"""
    import httpx
    from openai import APITimeoutError

    return APITimeoutError(
        request=httpx.Request("POST", "https://gw.example/v1/chat/completions")
    )


class FakeModel:
    """最小假模型：只提供入口用得到的 ``invoke(request, config=...)``。

    ``behaviour(call_no, handler, request)`` 返回响应对象，或抛异常模拟病态。
    handler 从 ``config["callbacks"]`` 取——生产路径上「首字节到了」的唯一信号。
    """

    def __init__(self, behaviour):
        self.calls = 0
        self.requests: list[str] = []
        self._behaviour = behaviour

    def invoke(self, request, config=None):
        self.calls += 1
        self.requests.append(request)
        callbacks = (config or {}).get("callbacks") or [None]
        return self._behaviour(self.calls, callbacks[0], request)


@pytest.fixture
def events():
    """订阅全局事件总线，收集本次调用发出的事件。"""
    got: list[dict] = []

    def collect(event: dict) -> None:
        got.append(event)

    reporter.subscribe(collect)
    yield got
    reporter.unsubscribe(collect)


@pytest.fixture(autouse=True)
def _fixed_gateway(monkeypatch):
    """报错文案里的网关名从 config 派生——给一个可辨认的网关。"""
    monkeypatch.setattr(
        config,
        "primary_model",
        ModelSettings(
            provider="dashscope",
            model="fake-model",
            base_url="https://api.gateway-a.example/v1",
            api_key_env="FAKE_API_KEY",
            purpose="主模型",
        ),
    )


def _wait_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "agent_wait"]


def test_success_reports_first_byte_and_returns_text(events):
    """成功：首字节事件按预期发出、结果带原文，事件与角色调用同形。"""

    def behaviour(call_no, handler, request):
        handler.on_llm_new_token("12")
        return AIMessage(content='{"segments": []}')

    result = run_direct_call(FakeModel(behaviour), "请拆分", role=ROLE)

    assert result.ok is True
    assert result.error is None
    assert result.text == '{"segments": []}'
    assert result.attempts == 1

    states = [e["state"] for e in _wait_events(events)]
    assert states == ["first_byte"]
    assert _wait_events(events)[0]["role"] == ROLE
    # 开始/完成与角色调用同形（agent_start / agent_done），面板与文本监听器直接可用
    types = [e["type"] for e in events]
    assert types[0] == "agent_start"
    assert "agent_done" in types


def test_success_records_tokens_into_cost_table(events):
    """直调路径的 token 进成本表（ADR 0019「后果」：成本数字上升是预期变化）。

    用假响应携带 usage_metadata：入口若忘了记账，成本表此行就是空的——
    这正是本仓「开流式不开 usage 上报」那类「看着还在、数是空的」坑的同形。
    """
    before = token_meter.snapshot().get(ROLE, {"calls": 0, "input": 0, "output": 0})

    def behaviour(call_no, handler, request):
        handler.on_llm_new_token("ok")
        return AIMessage(
            content="段表",
            usage_metadata={"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
        )

    run_direct_call(FakeModel(behaviour), "请拆分", role=ROLE)

    after = token_meter.snapshot()[ROLE]
    assert after["calls"] == before["calls"] + 1
    assert after["input"] == before["input"] + 123
    assert after["output"] == before["output"] + 45


def test_stuck_before_first_byte_retries_until_attempt_cap(events):
    """首字节前连续超时＝卡住：试满上限，结果带病因与尝试次数，不让异常漏出去。"""
    model = FakeModel(lambda call_no, handler, request: (_ for _ in ()).throw(_api_timeout()))

    result = run_direct_call(model, "请拆分", role=ROLE)

    assert result.ok is False
    assert result.response is None
    assert result.attempts == model_factory._LLM_MAX_ATTEMPTS
    assert model.calls == model_factory._LLM_MAX_ATTEMPTS
    assert result.error and "卡住" in result.error
    assert f"{model_factory._LLM_MAX_ATTEMPTS} 次" in result.error

    states = [e["state"] for e in _wait_events(events)]
    assert states.count("stuck") == model_factory._LLM_MAX_ATTEMPTS
    assert states.count("retrying") == model_factory._LLM_MAX_ATTEMPTS - 1


def test_timeout_then_success_retries_once(events):
    """超时一次后成功：只调用 2 次，结果 ok 且 attempts=2。"""

    def behaviour(call_no, handler, request):
        if call_no == 1:
            raise _api_timeout()
        handler.on_llm_new_token("段")
        return AIMessage(content="段表")

    model = FakeModel(behaviour)
    result = run_direct_call(model, "请拆分", role=ROLE)

    assert result.ok is True
    assert result.text == "段表"
    assert result.attempts == 2
    assert model.calls == 2
    assert [e["state"] for e in _wait_events(events)] == ["stuck", "retrying", "first_byte"]


def test_midstream_timeout_is_not_retried(events):
    """首字节后断流不重试：只调用 1 次，结果说清「已经流了一半」。"""

    def behaviour(call_no, handler, request):
        handler.on_llm_new_token("已经流了一半")
        raise FakeReadTimeout("read timed out")

    model = FakeModel(behaviour)
    result = run_direct_call(model, "请拆分", role=ROLE)

    assert result.ok is False
    assert result.attempts == 1
    assert model.calls == 1
    assert "首字节" in result.error and "不" in result.error
    assert [e["state"] for e in _wait_events(events)] == ["first_byte"]


def test_non_timeout_error_returns_failure_with_cause():
    """非超时异常：不重试、不抛出，病因原样交回调用方。"""

    def behaviour(call_no, handler, request):
        raise ValueError("boom")

    model = FakeModel(behaviour)
    result = run_direct_call(model, "请拆分", role=ROLE)

    assert result.ok is False
    assert result.attempts == 1
    assert model.calls == 1
    assert "boom" in result.error


def test_budget_exhausted_stops_early(monkeypatch):
    """预算耗尽即停：次数没到上限也不再来一轮（墙钟硬顶是算术事实）。"""
    monkeypatch.setattr(model_factory, "_LLM_CALL_BUDGET_SECONDS", 0.05)

    def behaviour(call_no, handler, request):
        time.sleep(0.05)
        raise _api_timeout()

    model = FakeModel(behaviour)
    result = run_direct_call(model, "请拆分", role=ROLE)

    assert result.ok is False
    assert model.calls == 1
    assert "预算" in result.error


def test_attempt_cap_follows_constant(monkeypatch):
    """次数上限从常量派生（变异检验：写死 3 会红）。"""
    monkeypatch.setattr(model_factory, "_LLM_MAX_ATTEMPTS", 2)
    model = FakeModel(lambda call_no, handler, request: (_ for _ in ()).throw(_api_timeout()))

    result = run_direct_call(model, "请拆分", role=ROLE)

    assert result.ok is False
    assert result.attempts == 2
    assert model.calls == 2


def test_request_is_sent_verbatim_on_retry():
    """重试重发同一请求（不追加内容病那种提示：直调路径只管存活病）。"""
    seen: list[str] = []

    def behaviour(call_no, handler, request):
        seen.append(request)
        raise _api_timeout()

    run_direct_call(FakeModel(behaviour), "原始请求", role=ROLE)

    assert len(seen) == model_factory._LLM_MAX_ATTEMPTS
    assert all(r == "原始请求" for r in seen)


def test_generating_events_are_throttled_by_module_constant(monkeypatch, events):
    """生成中的节流间隔与 run_agent 同源（改常量即改节奏，别写死字面量）。"""
    monkeypatch.setattr(agents_module, "_WAIT_PROGRESS_INTERVAL_SECONDS", 0.0)

    def behaviour(call_no, handler, request):
        for _ in range(3):
            handler.on_llm_new_token("你好")
        return AIMessage(content="ok")

    run_direct_call(FakeModel(behaviour), "请拆分", role=ROLE)

    generating = [e for e in _wait_events(events) if e["state"] == "generating"]
    assert generating
    assert generating[-1]["chunks"] == 3
    assert generating[-1]["bytes"] == 18  # 3 块 × 2 字符 × 3 字节（口径同 FirstByteHandler）


# ---------------------------------------------------------------------------
# 拆段接线（票面 AC：开始即打印步骤横幅；等待期间有「已等 Xs」心跳）
# ---------------------------------------------------------------------------

def test_segmented_flow_prints_step_banner_then_watchdog_lines(capsys, monkeypatch, tmp_path):
    """拆段开始即出步骤横幅，卡住/重试行由同一作用域打到终端。

    这条用例走**真实**的 ``plan_segments``（只把模型换成假对象）：接线若只接了
    横幅、没订阅事件，卡住行就没人打印；若只订阅事件、没打横幅，第一句断言即红。
    """
    from types import SimpleNamespace

    from minimax_h3_prompt import model_factory, session_store
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.ui import wizard

    plan_json = (
        '{"segments": ['
        '{"index": 0, "start_s": 0, "end_s": 9, "shots_in_segment": [1],'
        ' "summary": "A", "end_hook": "hook A"},'
        '{"index": 1, "start_s": 9, "end_s": 18, "shots_in_segment": [2],'
        ' "summary": "B", "end_hook": "hook B"}]}'
    )

    def behaviour(call_no, handler, request):
        if call_no == 1:
            raise _api_timeout()
        handler.on_llm_new_token("{")
        return AIMessage(content=plan_json)

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: FakeModel(behaviour))
    seen: dict = {}

    def fake_v2(session, plans, state, llm, summary=None, plans_from_cache=False):
        seen["plans"] = plans
        return True

    monkeypatch.setattr(wizard, "_run_segmented_flow_v2", fake_v2)

    brief = Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫")
    gen = tmp_path / "GEN001"
    gen.mkdir()
    session = session_store.SessionState(
        directory=gen, brief=brief, stage_state={"shot_table": "[Shot 1] 空店……"},
        status=session_store.STATUS_SEGMENTED_RUNNING)

    wizard._run_segmented_flow(
        brief, session, "For the target video…\n\n[Shot 1] a\n[Shot 2] b\n",
        state={"shot_table": "[Shot 1] 空店……"})

    out = capsys.readouterr().out
    assert "▶ 正在分段规划（第 1/1 轮）……" in out
    assert "判定卡住" in out
    assert "正在重试第 1 次" in out
    assert "第 1/1 轮" in out and out.index("第 1/1 轮") < out.index("判定卡住")
    assert len(seen["plans"]) == 2, "重试成功后应继续走 v2 写段"


def test_segmented_flow_prints_failure_cause_and_attempts_before_fallback(
        capsys, monkeypatch, tmp_path):
    """看门狗用尽重试 → 明说「原因＋已试 N 次」再回退机械拆分（不再静默）。"""
    from minimax_h3_prompt import model_factory, session_store
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt import segment_planner
    from minimax_h3_prompt.ui import wizard

    monkeypatch.setattr(
        model_factory, "build_chat_model",
        lambda: FakeModel(lambda call_no, handler, request: (_ for _ in ()).throw(_api_timeout())))

    # 回退路径的后续问答与剥帧不属于本用例的题目：单镜头提示词让它在「拆不出多段」
    # 处提前收尾，只把失败诊断这一句圈出来。
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")

    brief = Brief(mode="base", variant="I2VA", duration=9.0, style="写实", plot="便利店橘猫")
    gen = tmp_path / "GEN001"
    gen.mkdir()
    session = session_store.SessionState(
        directory=gen, brief=brief, stage_state={"shot_table": "[Shot 1] 空店……"},
        status=session_store.STATUS_SEGMENTED_RUNNING)

    wizard._run_segmented_flow(brief, session, "[Shot 1] a\n",
                               state={"shot_table": "[Shot 1] 空店……"})

    out = capsys.readouterr().out
    assert "[提示] 分段规划失败" in out
    assert "卡住" in out
    assert f"{model_factory._LLM_MAX_ATTEMPTS} 次" in out
    assert "回退到按 [Shot N] 机械拆分" in out


def test_ref_delivery_prints_banner_and_heartbeat_from_wizard(capsys, monkeypatch, tmp_path):
    """多参交付（向导路径）：拆段横幅与等待心跳都在，报告照常打出来。"""
    from types import SimpleNamespace

    from minimax_h3_prompt import ref_segments
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.ui import wizard

    def fake_prepare(**kwargs):
        # 模拟规划期间真模型发出的等待事件：没有订阅者的话，用户看不到这一行。
        reporter.emit({"type": "agent_wait", "role": ROLE, "state": "generating",
                       "elapsed": 15.0, "chunks": 3, "bytes": 30})
        return SimpleNamespace(), "[多参分段] 报告正文"

    # 向导走**准备**口（按需写段，issue #70）：段正文在陪跑轮到时才写。
    monkeypatch.setattr(ref_segments, "prepare_ref_segments", fake_prepare)
    brief = Brief(mode="ref", duration=30.0, style="写实", plot="花园递钥匙")

    wizard._deliver_ref_segments({}, brief, "PROMPT", tmp_path)

    out = capsys.readouterr().out
    assert "▶ 正在按镜头切点拆分执行段（第 1/1 轮）……" in out
    assert "已等 15s" in out
    assert "[多参分段] 报告正文" in out


def test_ref_delivery_prints_banner_and_heartbeat_from_cli(capsys, monkeypatch, tmp_path):
    """--brief 快路径与向导共用同一份反馈：两条入口都不许静默。"""
    from types import SimpleNamespace

    from minimax_h3_prompt import ref_segments
    from minimax_h3_prompt.brief_parser import Brief

    def fake_deliver(**kwargs):
        reporter.emit({"type": "agent_wait", "role": ROLE, "state": "stuck",
                       "elapsed": 30.0, "attempt": 1})
        return SimpleNamespace(), "[多参分段] 报告正文"

    monkeypatch.setattr(ref_segments, "deliver_ref_segments", fake_deliver)
    brief = Brief(mode="ref", duration=30.0, style="写实", plot="花园递钥匙")

    import minimax_h3_prompt.main as main_module  # 包 __init__ 里同名函数会遮蔽属性

    main_module._deliver_ref_segments_cli(brief, "PROMPT", tmp_path)

    out = capsys.readouterr().out
    assert "第 1/1 轮" in out
    assert "判定卡住" in out
