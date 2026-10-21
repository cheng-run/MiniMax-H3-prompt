"""``run_agent`` 的卡住止损与调用预算（issue #61）。

接缝只有 ``run_agent(agent, message)``：假 agent 提供 ``.name`` 与
``.invoke(payload, config=None)``，从 ``config["callbacks"]`` 里取出
``FirstByteHandler`` 并调用 ``on_llm_new_token`` —— 这条回调正是生产路径上
「首字节到了」的唯一信号（issue #59 探针：流式下首块 0.46 秒到、块间 gap 最大
0.62 秒，所以首字节就是「服务端在不在干活」的活体信号）。

不 monkeypatch ``ChatOpenAI``、不真等 30 秒：所有等待都在毫秒级。
"""
from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage

from minimax_h3_prompt import agents as agents_module
from minimax_h3_prompt import model_factory
from minimax_h3_prompt.agents import run_agent
from minimax_h3_prompt.config import ModelSettings, config
from minimax_h3_prompt.observability import reporter

ROLE = "role_art_director"


class TimeoutException(Exception):
    """模仿 httpx / httpx2 的基类名。

    为什么按类名判：openai 自带的 http 栈是 **vendored 的 httpx2**，流到一半
    卡住时抛的是 ``httpx2.ReadTimeout``——它不是 ``httpx.ReadTimeout``，也不是
    内建 ``TimeoutError``，更不是 ``APITimeoutError``。两代栈唯一的交集是 MRO 里
    这个叫 ``TimeoutException`` 的类，所以判据必须按类名咬（issue #61 实测）。
    本类刻意叫这个名字：改名会让用例脱离真实形态、判据就失去守卫意义。
    """


class FakeReadTimeout(TimeoutException):
    """「首字节之后流到一半卡住」的形态。"""


def _api_timeout() -> Exception:
    """「首字节之前就卡住」的形态（openai 3.0.0 实测）。"""
    import httpx
    from openai import APITimeoutError

    return APITimeoutError(
        request=httpx.Request("POST", "https://gw.example/v1/chat/completions")
    )


class FakeAgent:
    """最小假 agent：只提供 run_agent 用得到的两样东西（.name 与 .invoke）。

    behaviour(call_no, handler) 返回 invoke 的结果 dict，或抛异常模拟病态。
    """

    name = ROLE

    def __init__(self, behaviour):
        self.calls = 0
        self._behaviour = behaviour

    def invoke(self, payload, config=None):
        self.calls += 1
        callbacks = (config or {}).get("callbacks") or [None]
        return self._behaviour(self.calls, callbacks[0])


@pytest.fixture
def events():
    """订阅全局事件总线，收集 run_agent 发出的事件。"""
    got: list[dict] = []

    def collect(event: dict) -> None:
        got.append(event)

    reporter.subscribe(collect)
    yield got
    reporter.unsubscribe(collect)


@pytest.fixture(autouse=True)
def _fixed_gateway(monkeypatch):
    """报错文案从 config.primary_model 派生网关名——这里给一个可辨认的网关。"""
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


def test_stuck_before_first_byte_retries_until_attempt_cap(events):
    """首字节前连续超时＝卡住：试满上限、每次重试都有事件、报错说人话。"""
    agent = FakeAgent(lambda call_no, handler: (_ for _ in ()).throw(_api_timeout()))

    with pytest.raises(RuntimeError) as excinfo:
        run_agent(agent, "请写美术设定")

    assert agent.calls == model_factory._LLM_MAX_ATTEMPTS
    waits = _wait_events(events)
    states = [e["state"] for e in waits]
    assert states.count("stuck") == model_factory._LLM_MAX_ATTEMPTS
    assert states.count("retrying") == model_factory._LLM_MAX_ATTEMPTS - 1
    # 状态转折的顺序：每次卡住后才谈重试
    assert states[:3] == ["stuck", "retrying", "stuck"]
    msg = str(excinfo.value)
    assert ROLE in msg
    assert f"{model_factory._LLM_MAX_ATTEMPTS} 次" in msg


def test_timeout_then_success_retries_once(events):
    """超时一次后成功：返回文本，只调用 2 次。"""

    def behaviour(call_no, handler):
        if call_no == 1:
            raise _api_timeout()
        handler.on_llm_new_token("美")
        return {"messages": [AIMessage(content="美术设定")]}

    agent = FakeAgent(behaviour)

    assert run_agent(agent, "请写美术设定") == "美术设定"
    assert agent.calls == 2
    waits = _wait_events(events)
    assert [e["state"] for e in waits] == ["stuck", "retrying", "first_byte"]
    first_byte = [e for e in waits if e["state"] == "first_byte"][0]
    assert first_byte["role"] == ROLE
    assert first_byte["elapsed"] >= 0.0
    retrying = [e for e in waits if e["state"] == "retrying"][0]
    assert retrying["attempt"] == 2
    assert retrying["remaining_budget"] > 0.0


def test_midstream_timeout_is_not_retried(events):
    """首字节后超时＝流了一半：只调用 1 次、没有 retrying、直接报错。"""

    def behaviour(call_no, handler):
        handler.on_llm_new_token("已经流了一半")
        raise FakeReadTimeout("read timed out")

    agent = FakeAgent(behaviour)

    with pytest.raises(RuntimeError) as excinfo:
        run_agent(agent, "请写美术设定")

    assert agent.calls == 1
    waits = _wait_events(events)
    assert [e["state"] for e in waits] == ["first_byte"]
    msg = str(excinfo.value)
    assert "首字节" in msg
    assert "不" in msg and "重试" in msg


def test_non_timeout_exception_is_raised_as_is():
    """其他异常原样上抛，不改类型（别把诊断信息埋进 RuntimeError）。"""

    def behaviour(call_no, handler):
        raise ValueError("boom")

    agent = FakeAgent(behaviour)

    with pytest.raises(ValueError, match="boom"):
        run_agent(agent, "请写美术设定")
    assert agent.calls == 1


def test_budget_exhausted_stops_early(monkeypatch, events):
    """预算耗尽即停：即便次数还没到上限也不再来一轮（墙钟硬顶是算术事实）。

    假 agent sleep 0.05 秒把 0.05 秒预算耗光——这是本文件里唯一允许的等待，
    仍然在毫秒级（票面要求 ≤0.2s）。
    """
    monkeypatch.setattr(model_factory, "_LLM_CALL_BUDGET_SECONDS", 0.05)

    def behaviour(call_no, handler):
        time.sleep(0.05)
        raise _api_timeout()

    agent = FakeAgent(behaviour)

    with pytest.raises(RuntimeError) as excinfo:
        run_agent(agent, "请写美术设定")

    assert agent.calls == 1
    assert "预算" in str(excinfo.value)


def test_attempt_cap_follows_constant(monkeypatch, events):
    """次数上限从常量派生：把上限改成 2，调用次数就必须是 2。

    变异检验：实现若把 3 写死、没读常量，这条会红。
    """
    monkeypatch.setattr(model_factory, "_LLM_MAX_ATTEMPTS", 2)
    agent = FakeAgent(lambda call_no, handler: (_ for _ in ()).throw(_api_timeout()))

    with pytest.raises(RuntimeError):
        run_agent(agent, "请写美术设定")

    assert agent.calls == 2


def test_retry_requires_remaining_budget_for_one_more_read_timeout(monkeypatch, events):
    """剩余预算撑不起一次首字节线时不重试——次数没到也没意义。

    变异检验：只按次数判重试（漏了 `remaining >= 读超时`）的实现会调用 3 次。
    """
    monkeypatch.setattr(model_factory, "_LLM_CALL_BUDGET_SECONDS", 100.0)
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 1000.0)
    agent = FakeAgent(lambda call_no, handler: (_ for _ in ()).throw(_api_timeout()))

    with pytest.raises(RuntimeError) as excinfo:
        run_agent(agent, "请写美术设定")

    assert agent.calls == 1
    assert "预算" in str(excinfo.value)


def test_empty_output_retry_still_happens(events):
    """空产出是内容病，判据独立于卡住：既有的一次重试照旧发生。"""

    def behaviour(call_no, handler):
        handler.on_llm_new_token("")
        return {"messages": [AIMessage(content="" if call_no == 1 else "正文")]}

    agent = FakeAgent(behaviour)

    assert run_agent(agent, "请写美术设定") == "正文"
    assert agent.calls == 2


def test_empty_output_retry_eats_the_same_budget(monkeypatch, events):
    """空产出重试也吃同一份预算：预算已耗尽就不再来一轮。"""
    monkeypatch.setattr(model_factory, "_LLM_CALL_BUDGET_SECONDS", 0.0)

    def behaviour(call_no, handler):
        handler.on_llm_new_token("")
        return {"messages": [AIMessage(content="")]}

    agent = FakeAgent(behaviour)

    assert run_agent(agent, "请写美术设定") == ""
    assert agent.calls == 1


def test_failure_message_derives_gateway_and_numbers(monkeypatch, events):
    """报错文案里的网关名与超时数字都从配置/常量派生，不再写死 DashScope。

    这里把读超时设成 20、预算设成 11：剩余 11 秒撑不起一次 20 秒的首字节线，
    于是走「预算不够」分支——一句文案里同时能看到两个派生来的数字。
    """
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 20.0)
    monkeypatch.setattr(model_factory, "_LLM_CALL_BUDGET_SECONDS", 11.0)
    agent = FakeAgent(lambda call_no, handler: (_ for _ in ()).throw(_api_timeout()))

    with pytest.raises(RuntimeError) as excinfo:
        run_agent(agent, "请写美术设定")

    msg = str(excinfo.value)
    assert "api.gateway-a.example" in msg
    assert "20 秒内" in msg
    assert "11 秒预算" in msg
    assert "DashScope" not in msg

    # 换 base_url → 文案里的网关名跟着变（同一句文案只有一处，改配置即改文案）
    monkeypatch.setattr(
        config,
        "primary_model",
        ModelSettings(
            provider="dashscope",
            model="fake-model",
            base_url="https://other-node.example:8443/v1",
            api_key_env="FAKE_API_KEY",
            purpose="主模型",
        ),
    )
    with pytest.raises(RuntimeError) as excinfo2:
        run_agent(FakeAgent(lambda n, h: (_ for _ in ()).throw(_api_timeout())), "请写美术设定")

    assert "other-node.example" in str(excinfo2.value)
    assert "api.gateway-a.example" not in str(excinfo2.value)


def test_failure_message_falls_back_to_provider_without_base_url(monkeypatch, events):
    """取不到 base_url 的 host 时退回 provider（文案不许出现空网关名）。"""
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 20.0)
    monkeypatch.setattr(model_factory, "_LLM_CALL_BUDGET_SECONDS", 11.0)
    monkeypatch.setattr(
        config,
        "primary_model",
        ModelSettings(
            provider="mygateway",
            model="fake-model",
            base_url=None,
            api_key_env="FAKE_API_KEY",
            purpose="主模型",
        ),
    )
    agent = FakeAgent(lambda call_no, handler: (_ for _ in ()).throw(_api_timeout()))

    with pytest.raises(RuntimeError) as excinfo:
        run_agent(agent, "请写美术设定")

    assert "mygateway" in str(excinfo.value)


def test_generating_events_are_throttled(events):
    """默认 10 秒节流：一串瞬时到达的块只报首字节，不刷屏。"""

    def behaviour(call_no, handler):
        for _ in range(50):
            handler.on_llm_new_token("x")
        return {"messages": [AIMessage(content="ok")]}

    run_agent(FakeAgent(behaviour), "请写美术设定")

    assert [e["state"] for e in _wait_events(events)] == ["first_byte"]


def test_generating_event_reports_chunks_and_bytes(monkeypatch, events):
    """节流到点后上报累计块数与字节数（#62 按这两个数渲染进度）。

    把间隔改成 0：每块都到点，于是能数清累计值；变异检验——实现若忘了累计
    chunks/bytes，或把间隔写成字面量（不读常量），这条会红。
    """
    monkeypatch.setattr(agents_module, "_WAIT_PROGRESS_INTERVAL_SECONDS", 0.0)

    def behaviour(call_no, handler):
        for _ in range(3):
            handler.on_llm_new_token("ab")
        return {"messages": [AIMessage(content="ok")]}

    run_agent(FakeAgent(behaviour), "请写美术设定")

    generating = [e for e in _wait_events(events) if e["state"] == "generating"]
    assert generating  # 间隔为 0 时每块都发
    assert generating[-1]["chunks"] == 3
    assert generating[-1]["bytes"] == 6
    assert generating[-1]["role"] == ROLE
    assert generating[-1]["elapsed"] >= 0.0
