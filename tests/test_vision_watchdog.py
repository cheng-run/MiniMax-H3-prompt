"""读图客户端的等待看门狗（issue #71 / ADR 0019）。

接缝是 ``reference_auditor.describe_image(path)``：用户能在终端看到的只有它发出的
**等待事件**与失败文案。为什么要治：``[读图校对]`` 打印之后原先静默调用非流式读图
（``timeout=900``），「卡住」与「慢慢生成」完全不可分；素材循环又不在事件订阅里，
连结束事件都没人打印（issue #64 问题 4 的实测现场）。

本文件的断言落在三样外部可见的东西上：

- 返回的读图文本（成功时）；
- 事件序列（首字节 / 卡住 / 重试）——证明它真的走了流式判据；
- 终端文案（网关不支持流式时**说清楚**并回退为「只报已等时长」）。

绝不发网络请求：``_build_vision_model`` 一律打桩，假模型只实现
``invoke(request, config=...)`` 并把首字节喂给 config 里的回调。
"""
from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage
from PIL import Image

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.observability import VISION_ROLE, reporter, token_meter
from minimax_h3_prompt.tools import reference_auditor


class TimeoutException(Exception):
    """模仿 httpx / vendored httpx2 的基类名（判据按 MRO 里的类名咬，issue #61）。"""


def _api_timeout() -> Exception:
    """首字节之前就卡住（openai 3.0.0 实测形态）。"""
    import httpx
    from openai import APITimeoutError

    return APITimeoutError(
        request=httpx.Request("POST", "https://gw.example/v1/chat/completions")
    )


class FakeVisionModel:
    """最小假视觉模型：``invoke(request, config=...)`` 是唯一被调用的面。

    ``behaviour(call_no, handler, request)`` 返回响应对象或抛异常。handler 从
    ``config["callbacks"]`` 取——生产路径上「首字节到了」的唯一信号。
    """

    def __init__(self, behaviour):
        self.calls = 0
        self.requests: list[object] = []
        self._behaviour = behaviour

    def invoke(self, request, config=None):
        self.calls += 1
        self.requests.append(request)
        callbacks = (config or {}).get("callbacks") or [None]
        return self._behaviour(self.calls, callbacks[0], request)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "ref.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
    return path


@pytest.fixture
def events():
    got: list[dict] = []
    reporter.subscribe(got.append)
    yield got
    reporter.unsubscribe(got.append)


def _wait_states(events: list[dict]) -> list[str]:
    return [e["state"] for e in events if e.get("type") == "agent_wait"]


def _patch_models(monkeypatch, *, stream, fallback=None) -> None:
    """按 ``streaming`` 选假模型：读图客户端与回退客户端是两条构造路径。"""
    def build(streaming: bool = True):
        return stream if streaming else fallback
    monkeypatch.setattr(reference_auditor, "_build_vision_model", build)


# ---------------------------------------------------------------------------
# AC：读图走流式，首字节前超时判卡住
# ---------------------------------------------------------------------------

def test_success_streams_first_byte_and_returns_text(image, monkeypatch, events):
    """成功：读图走流式（首字节事件发出），返回模型原文。"""

    def behaviour(call_no, handler, request):
        handler.on_llm_new_token("画面")
        return AIMessage(content='{"claims": []}')

    _patch_models(monkeypatch, stream=FakeVisionModel(behaviour))

    text = reference_auditor.describe_image(image)

    assert text == '{"claims": []}'
    assert _wait_states(events) == ["first_byte"]
    assert events[0]["role"] == VISION_ROLE, "读图没用自己的角色名（成本表看不出贵在哪）"


def test_success_counts_the_reading_into_the_cost_table(image, monkeypatch):
    """读图仍是真实计费：走统一入口后 token 照旧进成本表（issue #52 不回退）。"""
    before = token_meter.snapshot().get(VISION_ROLE, {"calls": 0, "input": 0, "output": 0})

    def behaviour(call_no, handler, request):
        handler.on_llm_new_token("ok")
        return AIMessage(
            content="读图结果",
            usage_metadata={"input_tokens": 321, "output_tokens": 12, "total_tokens": 333},
        )

    _patch_models(monkeypatch, stream=FakeVisionModel(behaviour))
    reference_auditor.describe_image(image)

    after = token_meter.snapshot()[VISION_ROLE]
    assert (after["calls"] - before["calls"],
            after["input"] - before["input"],
            after["output"] - before["output"]) == (1, 321, 12)


def test_first_byte_timeout_is_judged_stuck_and_retried(image, monkeypatch, events):
    """首字节前超时＝卡住：按看门狗重试，结果仍能读出来，事件里能看见自救。"""

    def behaviour(call_no, handler, request):
        if call_no == 1:
            raise _api_timeout()
        handler.on_llm_new_token("读图结果")
        return AIMessage(content="读图结果")

    model = FakeVisionModel(behaviour)
    _patch_models(monkeypatch, stream=model)

    text = reference_auditor.describe_image(image)

    assert text == "读图结果"
    assert model.calls == 2, "首字节前的卡住没有重试（又回到白等 900 秒）"
    assert _wait_states(events) == ["stuck", "retrying", "first_byte"]


def test_stuck_reading_does_not_fall_back_to_nonstreaming(image, monkeypatch, events):
    """卡住不许回退非流式：那会把「判出卡住」再变回 900 秒白等（回退只治「不支持流式」）。"""
    fallback = FakeVisionModel(
        lambda call_no, handler, request: AIMessage(content="不该到这里"))
    stream = FakeVisionModel(
        lambda call_no, handler, request: (_ for _ in ()).throw(_api_timeout()))
    _patch_models(monkeypatch, stream=stream, fallback=fallback)

    with pytest.raises(RuntimeError, match="卡住"):
        reference_auditor.describe_image(image)

    assert stream.calls == model_factory._LLM_MAX_ATTEMPTS
    assert fallback.calls == 0, "卡住时回退了非流式（等于把卡住藏回 900 秒里）"


# ---------------------------------------------------------------------------
# AC：网关不支持流式时报错说清楚，流程不静默
# ---------------------------------------------------------------------------

def test_streaming_unsupported_reports_and_falls_back(image, monkeypatch, capsys):
    """流式被判不支持 → 明说「网关不支持流式」并回退非流式，只能报已等时长。"""

    def stream_behaviour(call_no, handler, request):
        raise ValueError("streaming is not supported by this gateway")

    def fallback_behaviour(call_no, handler, request):
        time.sleep(0.06)  # 让心跳有机会打一行（间隔由下面的常量压到 20ms）
        return AIMessage(content="回退读到的画面")

    stream = FakeVisionModel(stream_behaviour)
    fallback = FakeVisionModel(fallback_behaviour)
    _patch_models(monkeypatch, stream=stream, fallback=fallback)
    monkeypatch.setattr(reference_auditor, "_FALLBACK_HEARTBEAT_SECONDS", 0.02)

    text = reference_auditor.describe_image(image)

    assert text == "回退读到的画面"
    assert stream.calls == 1
    assert fallback.calls == 1, "没有回退非流式读图（流程会静默失败）"
    out = capsys.readouterr().out
    assert "网关" in out and "流式" in out, "网关不支持流式没有说清楚"
    assert "回退" in out, "没交代接下来怎么办"
    assert "已等" in out, "回退路径只剩 900 秒白等，连已等时长都不报"


def test_empty_stream_is_treated_as_unsupported_and_falls_back(image, monkeypatch, capsys):
    """网关吞掉 stream 参数时流是**空的**（不抛错）：这才最容易被静默放过。"""

    def stream_behaviour(call_no, handler, request):
        return AIMessage(content="")  # 一个块都没有

    fallback = FakeVisionModel(
        lambda call_no, handler, request: AIMessage(content="回退读到的画面"))
    _patch_models(monkeypatch, stream=FakeVisionModel(stream_behaviour), fallback=fallback)

    text = reference_auditor.describe_image(image)

    assert text == "回退读到的画面"
    assert fallback.calls == 1, "空流被当成功收下了（读图结果静默为空）"
    out = capsys.readouterr().out
    assert "流式" in out and ("空" in out or "没有" in out)


def test_fallback_failure_reports_both_causes(image, monkeypatch):
    """回退也失败：两条病因都在报错里，不许只报一半。"""

    def stream_behaviour(call_no, handler, request):
        raise ValueError("stream rejected")

    def fallback_behaviour(call_no, handler, request):
        raise RuntimeError("non-stream rejected")

    _patch_models(
        monkeypatch,
        stream=FakeVisionModel(stream_behaviour),
        fallback=FakeVisionModel(fallback_behaviour),
    )

    with pytest.raises(RuntimeError) as excinfo:
        reference_auditor.describe_image(image)

    message = str(excinfo.value)
    assert "stream rejected" in message and "non-stream rejected" in message
