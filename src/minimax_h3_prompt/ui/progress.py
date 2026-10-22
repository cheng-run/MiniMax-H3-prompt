"""实时进度面板：rich Live 渲染每个角色状态/token 累计。

正确用法：`Live(get_renderable=...)` —— rich 每次刷新都回调取最新状态；
管线跑在后台线程，事件只更新状态（加锁）；`transient=True` 跑完自动清除面板并恢复终端，
不残留残帧、不污染后续 rich/questionary 输出。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
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


def _fmt_bytes(n: int) -> str:
    """字节数 → 带单位的短串；1000 起进位到 kB。

    入参是 ``FirstByteHandler`` 累计的 **UTF-8 字节数**（不是字符数：中文一字
    3 字节）。上游若退回 ``len(token)``，这里的「kB」就会是假的——复审实测
    「你好世界」4 字符 = 12 字节。

    用 1000 而不是 1024：用户对「kB」的直觉是千进制（issue #59 探针 2 的
    426049 字节按千进制显示为 426.0 kB）。
    """
    if n >= 1000:
        return f"{n / 1000:.1f} kB"
    return f"{n} B"


def format_wait_event(event: dict) -> str | None:
    """「还在等」状态转折 → 一行给终端看的文本（issue #62）。

    **事件与渲染分开**是票面硬条：状态由调用层判定后发事件，这里只做格式化。
    所以每个数字都取自事件字段，渲染层不自己算——尤其 ``retry``：重试序号由发
    事件的一侧派生（``attempt - 1``），这里再减一次就会与 ``attempt`` 错位。

    未知 ``state`` 返回 None（安静忽略）：将来加状态时，旧渲染器不许打半句。
    """
    state = event.get("state")
    name = role_label(str(event.get("role", "模型")))
    elapsed = float(event.get("elapsed", 0.0) or 0.0)
    if state == "first_byte":
        return f"⏳ {name} 已有响应（首字节 {elapsed:.1f}s）"
    if state == "generating":
        chunks = int(event.get("chunks", 0) or 0)
        nbytes = int(event.get("bytes", 0) or 0)
        return (f"… {name} 生成中（{chunks} 块 / {_fmt_bytes(nbytes)}，"
                f"已等 {elapsed:.0f}s）")
    if state == "stuck":
        attempt = int(event.get("attempt", 0) or 0)
        # 秒数取事件里的 elapsed：生产路径上它≈首字节线，渲染层不重抄那个常量
        return f"⏱ {name} {elapsed:.0f} 秒没有响应，判定卡住（第 {attempt} 次尝试）"
    if state == "retrying":
        retry = int(event.get("retry", 0) or 0)
        attempt = int(event.get("attempt", 0) or 0)
        remaining = float(event.get("remaining_budget", 0.0) or 0.0)
        return (f"↻ {name} 正在重试第 {retry} 次（第 {attempt} 次尝试，"
                f"剩 {remaining:.0f}s 预算）")
    return None


def format_step_banner(action: str, *, round_no: int = 1, total_rounds: int = 1) -> str:
    """步骤横幅一行：「正在做什么 ＋ 第几轮/N」（issue #67 / ADR 0019）。

    它与心跳是**两种信号**（CONTEXT.md「步骤横幅」）：横幅回答「现在在干什么」，
    心跳（``format_wait_event``）回答「还活着吗」。所以横幅里**不带**已等时长——
    那个数属于心跳行，抄两份就是两处会各自漂移的口径。
    """
    return f"▶ {action}（第 {round_no}/{total_rounds} 轮）……"


@contextmanager
def step_scope(action: str, *, round_no: int = 1, total_rounds: int = 1):
    """打印步骤横幅，并在作用域内订阅等待心跳；退出（含异常）必解绑。

    为什么要有这个组合：直调路径的事件只对**已订阅者**可见。拆段与素材循环原先
    都在进展作用域之外，于是「判定卡住 / 正在重试第 N 次」这些最有用的行没人打印
    ——用户只看到白等（ADR 0019）。横幅与监听器成对存在，就不会出现「打了横幅
    却听不到心跳」的半套接线。
    """
    print(format_step_banner(action, round_no=round_no, total_rounds=total_rounds),
          flush=True)
    listener = TextProgress()
    reporter.subscribe(listener)
    try:
        yield listener
    finally:
        reporter.unsubscribe(listener)


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
        # 等待期也要有声音（issue #62）：超时的调用永远不会「完成」，只靠开始/完成
        # 两种事件时，用户盯着一行「正在处理……」十几分钟，看不出服务端还在不在动。
        if t == "agent_wait":
            line = format_wait_event(event)
            if line:
                print(f"  {line}", flush=True)
            return
        if t not in ("agent_start", "agent_done"):
            return
        name = role_label(str(event.get("role", "模型")))
        if t == "agent_start":
            print(f"  ▶ {name} 正在处理……", flush=True)
        else:
            duration = float(event.get("duration", 0))
            in_t = int(event.get("input", 0) or 0)
            out_t = int(event.get("output", 0) or 0)
            print(
                f"  ✓ {name} 完成（{duration:.1f}s | 输入 {in_t} tok / 输出 {out_t} tok | "
                f"累计 {fmt_clock(self._clock() - self._t0)}）",
                flush=True,
            )


def _status_style(status: str) -> str:
    """状态列配色：完成绿、卡住红、其余进行中黄、空/初始暗。

    从状态文本派生（面板里唯一可读的就是它），不另存一份 style 状态——两处口径
    会在加状态时静默分叉。
    """
    if status == "✓":
        return "green"
    if status == "卡住":
        return "red"
    if status and status != "…":
        return "yellow"
    return "dim"


class LiveProgress:
    def __init__(self, title: str = "MiniMax-H3 多智能体管线", console: Console | None = None) -> None:
        self._title = title
        self._console = console or Console()
        self._lock = threading.Lock()
        self._roles: dict[str, dict] = {}
        self._order: list[str] = []
        self._t0 = time.time()

    def __call__(self, event: dict) -> None:
        """监听器入口：与 ``TextProgress`` 同形，可直接 ``reporter.subscribe(progress)``。"""
        self._on_event(event)

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
            elif t == "agent_wait":
                self._apply_wait(event)
            elif t == "agent_done":
                role = event["role"]
                row = self._roles.setdefault(role, {"status": "", "duration": 0, "out_len": 0})
                if role not in self._order:
                    self._order.append(role)
                row["status"] = "✓"
                row["duration"] = round(float(event.get("duration", 0)), 1)
                row["out_len"] = int(event.get("out_len", 0))

    def _apply_wait(self, event: dict) -> None:
        """等待转折 → 状态列（issue #62）。**调用方须持有 ``self._lock``**。

        ``agent_start`` 现在每次 ``run_agent`` 只发一次（重试不重发），所以面板不能
        停在「运行中」：那正是用户看不出「第几次重试」的盲区。重试序号直接取事件
        的 ``retry`` 字段，渲染层不自己减（裁定 2）。
        """
        role = event["role"]
        row = self._roles.setdefault(role, {"status": "", "duration": 0, "out_len": 0})
        if role not in self._order:
            self._order.append(role)
        state = event.get("state")
        if state == "first_byte":
            row["status"] = "首字节"
        elif state == "generating":
            row["status"] = f"生成中 {int(event.get('chunks', 0) or 0)} 块"
        elif state == "stuck":
            row["status"] = "卡住"
        elif state == "retrying":
            row["status"] = f"重试 {int(event.get('retry', 0) or 0)}"

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
            st.style = _status_style(row["status"])
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
