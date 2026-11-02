"""观测组件：进度事件总线 + token 计量 + 阶段产物落盘。

业务代码 emit 事件、累计 token；UI 层 subscribe 消费。无订阅者时零开销。
模块级单例，避免把观测逻辑层层传参。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .config import config

# 成本表在会话 state 里的键（issue #52）。CONTEXT.md「续跑点」：成本表随续跑点持久化，
# 不随进程蒸发。写入方只有 ``session_store``（每次保存带 live 快照）与两个阶段入口
# （把 stage 跑完后的活账写回 state），恢复口只有一个：续跑入口 ``TokenMeter.restore``。
COST_TABLE_KEY = "cost_table"

# 读图（视觉调用）在成本表里的角色名。带 ``role_`` 前缀与各 agent 的命名同形
# （``agents`` 建的 agent 都叫 ``role_<角色>``），进度面板的 ``role_label`` 一视同仁。
VISION_ROLE = "role_vision"


def format_step_banner(action: str, *, round_no: int = 1, total_rounds: int = 1) -> str:
    """步骤横幅一行：「正在做什么 ＋ 第几轮/N」（issue #67 / ADR 0019）。

    它与心跳是**两种信号**（CONTEXT.md「步骤横幅」）：横幅回答「现在在干什么」，
    心跳（``ui.progress.format_wait_event``）回答「还活着吗」。所以横幅里**不带**已等
    时长——那个数属于心跳行，抄两份就是两处会各自漂移的口径。

    **住在 observability 而不是 ui**：``graph/pipeline.py`` 也要打横幅（qa 复审、组装，
    复审 S2），而 ``graph/`` 不许 import ``ui/``（分层）。这里只是纯字符串格式化、
    不依赖 rich；``ui.progress`` 继续从这里导出，既有 import 面不变。
    """
    return f"▶ {action}（第 {round_no}/{total_rounds} 轮）……"


# 结构化运行日志的开关（体检 §4.5 / P4）：默认开，``H3_RUN_LOG=0`` 关。
# 为什么用环境变量而不是 YAML 键：它要能在**测试**里被 conftest 一次性关掉，
# 也要能在排障时临时打开/关闭而不改仓里的配置。
_RUN_LOG_ENV = "H3_RUN_LOG"
_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def run_log_enabled() -> bool:
    return os.getenv(_RUN_LOG_ENV, "").strip().lower() not in _OFF_VALUES


class RunLog:
    """一次运行一份 JSONL（``logs/<时间>-<阶段>.jsonl``）：事件、耗时、token、错误码。

    为什么要有（体检 §4.5）：617 处 ``print`` 没有一条落盘，出事后无法从盘上复盘
    一次运行；成本、耗时、失败原因只能靠人看屏幕。

    三条不变式：

    1. **只记元数据，不记正文**——提示词与模型输出只记长度（``out_len``），
       避免把用户素材与商业内容写成日志资产；
    2. **写失败不影响主流程**：日志是观测，不是业务（与 ``Reporter.emit`` 同口径），
       但**第一次**失败要在 stderr 说一句，否则「日志悄悄没写」会变成新的静默；
    3. **嵌套运行只留第一份**：阶段 2 在阶段 1 之后开，同一次运行不该有两本账。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._handle: TextIO | None = None
        self._warned = False
        self._started_at = time.monotonic()

    @property
    def path(self) -> Path | None:
        return self._path

    def start(self, path: Path) -> Path | None:
        """开始一份运行日志；已激活时保持原来那份并返回它的路径。"""
        with self._lock:
            if self._handle is not None:
                return self._path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - 生命周期由 stop 管
            except OSError as exc:
                self._warn_once(f"[日志] 无法写入 {path}：{exc}")
                return None
            self._path = path
            self._started_at = time.monotonic()
        self.record("run_start")
        return path

    def stop(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle, self._handle, self._path = self._handle, None, None
        try:
            handle.close()
        except OSError:
            pass

    def record(self, event: str, **fields) -> None:
        """追加一条事件。``event`` 用短名（``agent_done`` / ``error`` / ``stage_end``）。"""
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "elapsed": round(time.monotonic() - self._started_at, 3),
                "event": event,
                **fields,
            }
            try:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
            except (OSError, TypeError, ValueError) as exc:
                self._warn_once(f"[日志] 写运行日志失败：{exc}")

    def _warn_once(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        print(message, file=sys.stderr)


run_log = RunLog()


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
        # 先落运行日志（无活动日志时是空操作），再分发给订阅者：观测层自己的
        # 事件链（cost/进度）与打印共用同一个来源，不另开一条记账路径。
        run_log.record(str(event.get("type", "event")),
                       **{key: value for key, value in event.items() if key != "type"})
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:  # 观测层异常不影响主流程
                pass


reporter = Reporter()


def default_run_log_path(base: Path, stage: str) -> Path:
    """``<base>/logs/<时间戳>-<阶段>.jsonl``：一次运行一份，文件名带微秒防撞。

    ``base`` 取 ``stage_saver.base.parent``（即 output/），测试里被 monkeypatch 成
    tmp_path 时日志跟着落进临时目录，不污染仓库。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return base / "logs" / f"{stamp}-{stage}.jsonl"


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
    """记录一次 LLM 调用：token 计量 + 事件上报（agent_done）。

    ``duration`` 是**这次产出总共花了多久**（agent 调用方应传「从 run_agent
    开始到此刻」，含超时重试），不是单次尝试的耗时——两者在重试过的调用上会
    差出数倍，而用户问的是「这次调用总共花了多久」（spec #59 US5）。
    """
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
