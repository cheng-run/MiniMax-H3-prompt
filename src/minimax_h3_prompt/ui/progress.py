"""实时进度面板：rich Live 渲染每个角色状态/token 累计。

正确用法：`Live(get_renderable=...)` —— rich 每次刷新都回调取最新状态；
管线跑在后台线程，事件只更新状态（加锁）；`transient=True` 跑完自动清除面板并恢复终端，
不残留残帧、不污染后续 rich/questionary 输出。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..observability import reporter, token_meter


def role_label(role: str) -> str:
    """剥掉 agent name 的 role_ 前缀，得到用户可读的角色名。"""
    return role.removeprefix("role_") if role.startswith("role_") else role


def fmt_clock(seconds: float) -> str:
    """秒数 → m:ss。"""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class TextProgress:
    """文本行进度监听器：每个角色开始/完成时打一行带耗时的进度。

    与 LiveProgress（rich Live 面板）不同：普通 print 行持久留在终端、
    之后可直接接 input() 交互，适合向导等纯交互场景。
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._t0 = clock()

    def __call__(self, event: dict) -> None:
        t = event.get("type")
        if t not in ("agent_start", "agent_done"):
            return
        name = role_label(str(event.get("role", "模型")))
        if t == "agent_start":
            print(f"  ▶ {name} 正在处理……", flush=True)
        else:
            duration = float(event.get("duration", 0))
            print(f"  ✓ {name} 完成（{duration:.1f}s | 累计 {fmt_clock(self._clock() - self._t0)}）", flush=True)


class LiveProgress:
    def __init__(self, title: str = "MiniMax-H3 多智能体管线", console: Console | None = None) -> None:
        self._title = title
        self._console = console or Console()
        self._lock = threading.Lock()
        self._roles: dict[str, dict] = {}
        self._order: list[str] = []
        self._t0 = time.time()

    def _on_event(self, event: dict) -> None:
        """只更新状态（线程安全）。渲染交给 Live 的 get_renderable 回调。"""
        t = event.get("type")
        with self._lock:
            if t == "agent_start":
                role = event["role"]
                row = self._roles.setdefault(role, {"status": "…", "duration": 0, "out_len": 0})
                if role not in self._order:
                    self._order.append(role)
                row["status"] = "运行中"
            elif t == "agent_done":
                role = event["role"]
                row = self._roles.setdefault(role, {"status": "", "duration": 0, "out_len": 0})
                if role not in self._order:
                    self._order.append(role)
                row["status"] = "✓"
                row["duration"] = round(float(event.get("duration", 0)), 1)
                row["out_len"] = int(event.get("out_len", 0))

    def _render(self) -> Panel:
        """每次刷新被 rich 回调，读取最新状态。"""
        with self._lock:
            snapshot = [(r, dict(self._roles[r])) for r in self._order]
        table = Table(box=None, expand=False, show_header=True, header_style="bold")
        table.add_column("角色")
        table.add_column("状态", justify="center")
        table.add_column("耗时", justify="right")
        table.add_column("产出", justify="right")
        for role, row in snapshot:
            st = Text(row["status"])
            st.style = "green" if row["status"] == "✓" else ("yellow" if row["status"] == "运行中" else "dim")
            table.add_row(role_label(role), st, f"{row['duration']}s", str(row["out_len"]))
        meter = token_meter.format()
        return Panel(
            Group(table, Text(f"\n⏱ {fmt_clock(time.time() - self._t0)} | {meter}")),
            title=self._title,
            border_style="cyan",
        )

    def run(self, pipeline_call: Callable[[], str]) -> str:
        """订阅事件，后台线程跑管线，Live 实时渲染；返回管线结果。"""
        reporter.subscribe(self._on_event)
        result: dict = {}

        def worker() -> None:
            try:
                result["value"] = pipeline_call()
            except BaseException as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            with Live(
                get_renderable=self._render,
                console=self._console,
                auto_refresh=True,
                refresh_per_second=4,
                transient=True,  # 跑完清除面板、恢复终端
            ):
                while t.is_alive():
                    time.sleep(0.1)
                t.join()
        finally:
            reporter.unsubscribe(self._on_event)

        if "error" in result:
            raise result["error"]
        return result.get("value", "")
