"""observability 组件单测：Reporter / TokenMeter / StageSaver / extract_usage。"""
from __future__ import annotations

import threading

import pytest
from langchain_core.messages import AIMessage

from minimax_h3_prompt.config import config
from minimax_h3_prompt.observability import (
    Reporter,
    StageSaver,
    TokenMeter,
    extract_usage,
    token_meter,
)


@pytest.fixture(autouse=True)
def _reset_meter():
    token_meter.reset()
    yield
    token_meter.reset()


class TestReporter:
    def test_subscribe_emit_unsubscribe(self):
        r = Reporter()
        got = []
        r.subscribe(lambda ev: got.append(ev["x"]))
        r.emit({"x": 1})
        r.emit({"x": 2})
        assert got == [1, 2]
        cb = lambda ev: got.append("nope")  # noqa: E731
        r.subscribe(cb)
        r.unsubscribe(cb)
        r.emit({"x": 3})
        assert got == [1, 2, 3]

    def test_emit_exception_not_propagate(self):
        r = Reporter()

        def bad(ev):
            raise RuntimeError("boom")

        r.subscribe(bad)
        r.emit({"x": 1})  # 订阅者异常不应影响主流程

    def test_thread_safe_emit(self):
        r = Reporter()
        got = []
        lock = threading.Lock()

        def cb(ev):
            with lock:
                got.append(ev["n"])

        r.subscribe(cb)

        def worker():
            for i in range(50):
                r.emit({"n": i})

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(got) == 200


class TestTokenMeter:
    def test_add_and_totals(self):
        tm = TokenMeter(0.5, 1.5)
        tm.add("a", 100, 50)
        tm.add("a", 100, 50)
        tm.add("b", 200, 0)
        t = tm.totals()
        assert t["calls"] == 3
        assert t["input"] == 400
        assert t["output"] == 100
        # 400/1e6*0.5 + 100/1e6*1.5 = 0.00035
        assert abs(t["cost"] - 0.00035) < 1e-9

    def test_reset(self):
        tm = TokenMeter(0.5, 1.5)
        tm.add("a", 1, 1)
        tm.reset()
        assert tm.totals()["calls"] == 0

    def test_snapshot_roles(self):
        tm = TokenMeter(0.5, 1.5)
        tm.add("导演", 10, 5)
        assert "导演" in tm.snapshot()


class TestExtractUsage:
    def test_single_message(self):
        m = AIMessage(content="x", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        assert extract_usage(m) == {"input": 7, "output": 3}

    def test_list_messages(self):
        msgs = [
            AIMessage(content="x", usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}),
            AIMessage(content="y", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}),
        ]
        assert extract_usage(msgs) == {"input": 4, "output": 6}

    def test_no_usage(self):
        assert extract_usage([]) == {"input": 0, "output": 0}


class TestStageSaver:
    def test_save(self, tmp_path):
        config.save_stages = True
        s = StageSaver(tmp_path)
        s.save("导演", {"导演阐述": "hello"})
        assert (tmp_path / "导演.txt").exists()
        assert "hello" in (tmp_path / "导演.txt").read_text(encoding="utf-8")

    def test_save_empty(self, tmp_path):
        config.save_stages = True
        s = StageSaver(tmp_path)
        s.save("空节点", {"a": ""})
        assert "（空）" in (tmp_path / "空节点.txt").read_text(encoding="utf-8")


def test_percent_used_and_remaining():
    from minimax_h3_prompt.observability import TokenMeter

    meter = TokenMeter(0.5, 1.5)
    meter.add("role_a", input_tokens=300_000, output_tokens=200_000)
    assert meter.percent_used(1_000_000) == pytest.approx(0.5)
    assert meter.remaining_tokens(1_000_000) == 500_000
    assert meter.percent_used(0) == 0.0
    assert meter.remaining_tokens(0) == 0
