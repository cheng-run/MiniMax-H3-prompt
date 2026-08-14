"""观测组件：进度事件总线 + token 计量 + 阶段产物落盘。

业务代码 emit 事件、累计 token；UI 层 subscribe 消费。无订阅者时零开销。
模块级单例，避免把观测逻辑层层传参。
"""
from __future__ import annotations

import threading
from pathlib import Path

from .config import config


class Reporter:
    """线程安全事件总线。事件为 dict，必须含 type。"""

    def __init__(self) -> None:
        self._subscribers: list = []
        self._lock = threading.Lock()

    def subscribe(self, callback) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def emit(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:  # 观测层异常不影响主流程
                pass


reporter = Reporter()


class TokenMeter:
    """按角色累计调用次数与 token，按配置单价估算费用。"""

    def __init__(self, price_in: float, price_out: float) -> None:
        self._price_in = price_in
        self._price_out = price_out
        self._lock = threading.Lock()
        self._per_role: dict[str, dict] = {}

    def reset(self) -> None:
        with self._lock:
            self._per_role = {}

    def add(self, role: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self._lock:
            row = self._per_role.setdefault(role, {"calls": 0, "input": 0, "output": 0})
            row["calls"] += 1
            row["input"] += input_tokens
            row["output"] += output_tokens

    def snapshot(self) -> dict:
        with self._lock:
            return {r: dict(v) for r, v in self._per_role.items()}

    def totals(self) -> dict:
        s = self.snapshot()
        input_t = sum(v["input"] for v in s.values())
        output_t = sum(v["output"] for v in s.values())
        calls = sum(v["calls"] for v in s.values())
        cost = input_t / 1e6 * self._price_in + output_t / 1e6 * self._price_out
        return {"calls": calls, "input": input_t, "output": output_t, "cost": cost}

    def format(self) -> str:
        t = self.totals()
        return (f"调用 {t['calls']} 次 | 输入 {t['input']} tok / 输出 {t['output']} tok | "
                f"估算费用 ¥{t['cost']:.4f}")


token_meter = TokenMeter(config.price_per_m_input, config.price_per_m_output)


def extract_usage(messages) -> dict:
    """从一条或一组消息汇总 usage_metadata 的 input/output tokens。"""
    if hasattr(messages, "usage_metadata"):  # 单条消息
        messages = [messages]
    in_t = out_t = 0
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if um:
            in_t += int(um.get("input_tokens", 0) or 0)
            out_t += int(um.get("output_tokens", 0) or 0)
    return {"input": in_t, "output": out_t}


class StageSaver:
    """把每个节点的产物写盘 output/stages/<node>.txt。"""

    def __init__(self, base: Path | None = None) -> None:
        self.base = base or config.stages_path

    def save(self, node: str, fields: dict) -> None:
        if not config.save_stages:
            return
        self.base.mkdir(parents=True, exist_ok=True)
        parts = []
        for k, v in fields.items():
            if isinstance(v, str) and v.strip():
                parts.append(f"# {k}\n{v}")
        text = "\n\n".join(parts) or "（空）"
        safe = node.replace("/", "_").replace(" ", "_")
        (self.base / f"{safe}.txt").write_text(text, encoding="utf-8")


stage_saver = StageSaver()


def record_agent_call(role: str, messages, out: str, duration: float) -> None:
    """记录一次 LLM 调用：token 计量 + 事件上报（agent_done）。"""
    usage = extract_usage(messages)
    token_meter.add(role, usage["input"], usage["output"])
    reporter.emit({
        "type": "agent_done",
        "role": role,
        "duration": duration,
        "out_len": len(out or ""),
        "input": usage["input"],
        "output": usage["output"],
    })
