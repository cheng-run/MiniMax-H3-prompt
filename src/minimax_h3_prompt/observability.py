"""观测组件：进度事件总线 + token 计量 + 阶段产物落盘。

业务代码 emit 事件、累计 token；UI 层 subscribe 消费。无订阅者时零开销。
模块级单例，避免把观测逻辑层层传参。
"""
from __future__ import annotations

import threading
from pathlib import Path

from .config import config

# 成本表在会话 state 里的键（issue #52）。CONTEXT.md「续跑点」：成本表随续跑点持久化，
# 不随进程蒸发。写入方只有 ``session_store``（每次保存带 live 快照）与两个阶段入口
# （把 stage 跑完后的活账写回 state），恢复口只有一个：续跑入口 ``TokenMeter.restore``。
COST_TABLE_KEY = "cost_table"

# 读图（视觉调用）在成本表里的角色名。带 ``role_`` 前缀与各 agent 的命名同形
# （``agents`` 建的 agent 都叫 ``role_<角色>``），进度面板的 ``role_label`` 一视同仁。
VISION_ROLE = "role_vision"


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

    def restore(self, snapshot: dict | None) -> None:
        """把一份成本表快照接回计量表（issue #52：续跑接着累计，不归零）。

        ``None`` ＝ 旧会话没有这张表——空操作，**不是清零**：本进程已经累计的账照旧
        （把它当清零会让「阶段入口不清零」这条本轮裁定从后门被推翻）。

        快照是**替换**语义（文件里那张就是当时全量账），只在续跑入口调。错形要响：
        会话文件是我们自己写的，错形只可能是损坏或外人改写，吞掉就是账被悄悄清零——
        与画布/视频输出三元组的严格读回同一条纪律（``session_store``）。
        """
        if snapshot is None:
            return
        if not isinstance(snapshot, dict):
            raise ValueError(
                f"成本表快照错形：{snapshot!r}（应为 角色→{{calls,input,output}} 的对象）")
        rows: dict[str, dict] = {}
        for role, row in snapshot.items():
            if not isinstance(role, str) or not isinstance(row, dict):
                raise ValueError(f"成本表快照条目错形：{role!r} → {row!r}")
            fields: dict[str, int] = {}
            for key in ("calls", "input", "output"):
                # 三个字段**必须齐整**（复审）：`row.get(key, 0)` 会把缺字段的行静默补成
                # 0——那正是「账少了一笔而没人知道」，与本函数 docstring 的「错形要响」
                # 和 `session_store._video_output_from_dict`「条目须齐整、不许静默强转」
                # 的先例都相左。多出来的键忽略（前向兼容），缺的不行。
                if key not in row:
                    raise ValueError(f"成本表快照条目缺字段：{role}[{key}]")
                value = row[key]
                # bool 是 int 的子类：True 混进 token 数比报错更难查，显式挡掉
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"成本表快照条目错形：{role}[{key}] = {value!r}")
                fields[key] = value
            rows[role] = fields
        with self._lock:
            self._per_role = rows

    def totals(self) -> dict:
        s = self.snapshot()
        input_t = sum(v["input"] for v in s.values())
        output_t = sum(v["output"] for v in s.values())
        calls = sum(v["calls"] for v in s.values())
        cost = input_t / 1e6 * self._price_in + output_t / 1e6 * self._price_out
        return {"calls": calls, "input": input_t, "output": output_t, "cost": cost}

    def percent_used(self, budget_tokens: int) -> float:
        """已用 token 占预算的比例（0–1）。预算为 0 时返回 0.0。"""
        if budget_tokens <= 0:
            return 0.0
        totals = self.totals()
        used = totals["input"] + totals["output"]
        return min(1.0, used / budget_tokens)

    def remaining_tokens(self, budget_tokens: int) -> int:
        """剩余 token 数（不小于 0）。"""
        if budget_tokens <= 0:
            return 0
        totals = self.totals()
        return max(0, budget_tokens - totals["input"] - totals["output"])

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


def record_vision_call(response, out: str, duration: float) -> None:
    """记录一次读图（视觉调用）——issue #52：读图也是真实计费，不许留在账外。

    走 ``record_agent_call`` 同一条路（计量 + agent_done 事件）：读图和 agent 调用在
    「花了多少钱」这件事上没有区别，各写一套计量就是第二份可写来源。视觉模型与文本
    模型不同价，但本仓的计价口径是**单一单价估算**（spec #47 Out of Scope：不做精确
    计价），故沿用同一张单价，只把角色单列成 ``role_vision`` 以便看出贵在哪。
    """
    record_agent_call(VISION_ROLE, [response], out, duration)
