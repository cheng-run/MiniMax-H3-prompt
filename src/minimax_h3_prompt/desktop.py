"""桌面外壳入口：``python -m minimax_h3_prompt.desktop``（D5-5 / #240）。

外壳只做三件事（spec #235 §10）：**等后端就绪 → 建窗口 → 关窗退出**。

1. 后端仍由 :func:`minimax_h3_prompt.api.__main__.start` 起：端口、数据根、配置骨架、
   单实例、日志落盘全在那一处收口，这里**不重写**，也不改任何业务端点。
2. **不需要原生桥**：``GET /api/v1/auth/token`` 不设防且同一响应下发 ``wb_session``
   Cookie，前端页面打开即自取并缓存（``frontend/src/api/client.ts`` 的
   ``ensureSessionToken``）。窗口地址因此**不带令牌**，与系统浏览器走同一条引导路径。
3. 关窗即退出：``webview.start()`` 在窗口全部关闭后返回，随后 ``handle.close()``
   停 uvicorn 线程并放单实例锁。Ctrl-C 走同一条收尾（``finally``）。

本模块**顶层不 import pywebview**：单测要在无人值守环境里跑，GUI 依赖只在真正建窗
那一刻才加载（:func:`_load_webview` 是唯一进口，测试替换它即可）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Literal, MutableMapping, Sequence

from . import bootstrap
from .api import __main__ as entry
# WebView2 的版本串与注册表探测点住在 ``winenv``（诊断包读同一份事实）。GUID 在这里
# 以显式 re-export 保留给用例与排障看。
from .winenv import (
    WEBVIEW2_CLIENT_GUID as WEBVIEW2_CLIENT_GUID,
    read_webview2_version,
)

logger = logging.getLogger(__name__)

APP_TITLE = "MiniMax H3 Prompt"

# 就绪探测：``api/routes/health.py`` 的 ``GET /api/v1/health``（本机回环，不设防）。
HEALTH_PATH = "/api/v1/health"
# 默认 30s：本机实测后端 < 2s 起来；给慢磁盘与杀软扫描留余量（spec #235 §10）。
READY_TIMEOUT_S = 30.0
READY_POLL_INTERVAL_S = 0.05
# 单次探测的 HTTP 超时。后端没监听时是「立刻 refused」，这个值只兜「连上了但不回包」。
PROBE_TIMEOUT_S = 2.0

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860


class ReadinessError(RuntimeError):
    """后端没能在超时内就绪，或后端线程在就绪前退出。"""


# --------------------------------------------------------------- 事实与取值来源

def startup_info(handle: object) -> entry.StartupInfo:
    """本次启动的事实：优先 ``app.state.startup``（启动时落下的单一来源），其次句柄。

    外壳不自己拼 ``http://host:port``，也不自己解析版本——两处各算一份就是
    「同一契约两处口径」（#56/#57 的老坑）。
    """
    state = getattr(getattr(handle, "app", None), "state", None)
    info = getattr(state, "startup", None)
    if info is not None:
        return info
    return handle.info  # type: ignore[attr-defined]


def window_title(info: entry.StartupInfo) -> str:
    """窗口标题：产品名 + 版本 + **实际绑定端口**（排障时第一眼要看到它）。

    版本为空（元数据读不到）时只省掉版本段，端口永远在。
    """
    parts = [APP_TITLE]
    if info.version:
        parts.append(info.version)
    return f"{' '.join(parts)}（端口 {info.port}）"


# ------------------------------------------------------------------- 就绪探测

def probe_health(base_url: str, *, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """健康端点是否已回 200；连不上 / 5xx / 畸形地址一律当作「还没好」。

    为什么不区分「没起来」与「起错」：探测阶段两者对用户是同一件事——再等等，或者
    在超时后的报错里看日志。区分它们要读后端日志，那是排障不是探测。
    """
    url = base_url.rstrip("/") + HEALTH_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until_ready(
    base_url: str,
    timeout: float = READY_TIMEOUT_S,
    *,
    interval: float = READY_POLL_INTERVAL_S,
    probe: Callable[[str], bool] | None = None,
    is_alive: Callable[[], bool] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """轮询健康端点直到 200；超时或后端线程先死则抛 :class:`ReadinessError`。

    可注入 ``probe`` / ``is_alive`` / ``clock`` / ``sleep``：单测里不真起窗口也不等墙钟。
    默认函数的绑定放在调用时取（不是 ``def`` 期的默认值），这样用例替换模块函数也生效。
    """
    probe = probe or probe_health
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    while True:
        if probe(base_url):
            return
        if is_alive is not None and not is_alive():
            raise ReadinessError(
                f"后端服务在就绪前退出了（{base_url}）。"
                "请查看 data/logs 下当天的日志，或用 --port 换一个端口后重试。")
        if clock() >= deadline:
            raise ReadinessError(
                f"后端在 {timeout:g}s 内没有就绪（{base_url}）。"
                "端口可能被占用、配置可能写错，或启动过程报错——"
                "详见 data/logs 下当天的日志；也可以先用 --port 0 换一个随机端口重试。")
        sleep(interval)


# --------------------------------------------------------------- 命令行与窗口

# pywebview 的 edgechromium 后端只在 ``webview.settings`` 的这个键非空时，才把
# ``--remote-debugging-port=`` 拼进 WebView2 的浏览器参数（``platforms/edgechromium.py``）。
# 环境变量 ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` 那条路实测不通——pywebview 自己显式
# 赋值 ``AdditionalBrowserArguments``，把环境变量读到的值盖掉了（#236 spike 第 6 节）。
REMOTE_DEBUGGING_SETTING = "REMOTE_DEBUGGING_PORT"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """外壳自己的参数：端口/数据根原样转发，调试端口与就绪超时只属于外壳。"""
    parser = argparse.ArgumentParser(
        prog="python -m minimax_h3_prompt.desktop",
        description="MiniMax H3 Prompt 桌面外壳：等本地服务就绪后打开窗口，关窗即退出。",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="同 ``api`` 入口：监听端口；0 = 自动挑一个空闲端口。",
    )
    parser.add_argument(
        "--data-root", default=None,
        help="同 ``api`` 入口：可写数据根（会话/产物/日志/配置）。",
    )
    parser.add_argument(
        "--debug-port", type=int, default=None,
        help="给 WebView2 开 CDP 远程调试端口（自动化验收用；默认不开）。",
    )
    parser.add_argument(
        "--ready-timeout", type=float, default=READY_TIMEOUT_S,
        help=f"等 /api/v1/health 就绪的上限秒数（默认 {READY_TIMEOUT_S:g}）。",
    )
    return parser.parse_args(argv)


def server_argv(args: argparse.Namespace) -> list[str]:
    """转交给 :func:`api.__main__.start` 的参数。

    只转发那两处属于服务入口的开关——端口与数据根的选择、单实例、配置骨架全在
    ``api.__main__`` 里，外壳不重写（spec #235 §10「只做三件事」）。
    """
    argv: list[str] = []
    if args.port is not None:
        argv += ["--port", str(args.port)]
    if args.data_root:
        argv += ["--data-root", str(args.data_root)]
    return argv


def apply_debug_port(settings: MutableMapping[str, object], debug_port: int | None) -> None:
    """给了调试端口才写这个设置；默认**一个字节都不动**。

    ``webview.settings`` 是只允许改既有键的 ``ImmutableDict``，这个键是预置的，赋值合法。
    """
    if debug_port is None:
        return
    settings[REMOTE_DEBUGGING_SETTING] = int(debug_port)


def _load_webview():
    """延迟 import pywebview。

    单测与 CLI 都不该为了「只是导入外壳」付出 GUI 后端的加载代价；这一处也是用例
    替换窗口库的唯一进口（见 ``tests/test_desktop.py`` 的 ``FakeWebview``）。
    """
    import webview

    return webview


def launch_window(handle: object, *, debug_port: int | None = None) -> None:
    """建窗并阻塞到最后一个窗口关闭。

    地址就是 ``StartupInfo.base_url``：**不带令牌**、不带查询串，与系统浏览器打开的
    是同一个 URL——鉴权由页面自己走 ``/api/v1/auth/token`` + ``wb_session`` Cookie。
    不传 ``js_api``：外壳不承载任何业务，也不引入第二套凭据通道（spec #235 §10）。
    """
    webview = _load_webview()
    apply_debug_port(webview.settings, debug_port)
    info = startup_info(handle)
    webview.create_window(
        window_title(info), url=info.base_url,
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
    )
    # 阻塞：窗口全部关闭后才返回，返回即整个外壳的收尾信号。
    webview.start()


# ------------------------------------------------------------------- 错误提示

def notify_error(title: str, message: str) -> None:
    """把可操作错误交到用户眼前：stderr（控制台可见时）+ Windows 原生消息框。

    为什么要有消息框：冻结的窗口程序双击启动时没有控制台，只写 stderr 等于没写，
    用户看到的就是「双击没反应」。消息框是这一层唯一的可见出口；stderr 保留给
    命令行启动与验收脚本抓文案。
    """
    print(f"[启动失败] {message}", file=sys.stderr, flush=True)
    _message_box(title, message)


def _message_box(title: str, message: str) -> None:
    """Win32 消息框（``MB_ICONERROR | MB_SETFOREGROUND``）；非 Windows 上是空操作。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010 | 0x00010000)
    except Exception as exc:  # noqa: BLE001 - 弹框失败不该再炸出 traceback
        logger.warning("错误消息框没能弹出：%s", exc)


# --------------------------------------------------------------- WebView2 检测

# 版本串与注册表探测点都住在 ``winenv``（顶部 import）；这里只留「拦不拦」的判定下限。
# 「过旧」的下限：Evergreen 是滚动升级的，主版本 < 100（2022 年前）意味着运行时被
# 锁在了好几年前，现代 CSS/JS 特性已经不可靠。**只在这一档拦**；版本串读不懂时按
# 「不可判定」放行（宁可警告不硬阻断，与 H3 产物帧数表的既有口径一致）。
MIN_WEBVIEW2_MAJOR = 100


@dataclass(frozen=True)
class WebView2Status:
    """一次检测的结论：``ok`` / ``missing`` / ``outdated`` / ``unknown``。

    ``unknown`` 是「判断不了」（非 Windows、注册表读不到但也不确定没装、版本串读不懂），
    与 ``missing`` 分开——前者继续启动只记警告，后者必须拦下并给安装指引。
    """

    state: Literal["ok", "missing", "outdated", "unknown"]
    version: str = ""


def _major_version(text: str) -> int | None:
    head = text.strip().split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def detect_webview2(
    *,
    platform: str | None = None,
    reader: Callable[[str, str, str], str | None] | None = None,
) -> WebView2Status:
    """启动前的 WebView2 兜底检测（spec #235 §10；目标机 Win11 预装，这层是兜底）。"""
    if (platform or sys.platform) != "win32":
        return WebView2Status("unknown")
    version = read_webview2_version(platform=platform, reader=reader)
    if not version:
        return WebView2Status("missing")
    major = _major_version(version)
    if major is None:
        return WebView2Status("unknown", version)
    if major < MIN_WEBVIEW2_MAJOR:
        return WebView2Status("outdated", version)
    return WebView2Status("ok", version)


def webview2_help_text(status: WebView2Status) -> str:
    """「怎么装」比「缺了什么」更重要：给可直接照做的三步。"""
    if status.state == "outdated":
        head = f"WebView2 运行时版本过旧（当前 {status.version}）。"
    else:
        head = "没有找到 Microsoft Edge WebView2 运行时。"
    return (
        f"{head}本程序用系统自带的 WebView2 显示界面。请先装好最新的 "
        "Microsoft Edge WebView2 Runtime（Evergreen）再重新打开：\n"
        "  1) 打开 https://developer.microsoft.com/microsoft-edge/webview2/\n"
        "  2) 下载「Evergreen Standalone Installer」并安装\n"
        "  3) 重新双击程序\n"
        "Windows 11 一般已预装；出现这条提示通常是运行时被卸载或系统被精简。"
    )


# --------------------------------------------------------------------- 入口

def _fail(code: int, message: str) -> int:
    """提示 + 退出码：外壳的所有失败路径都从这里出去（不抛裸 traceback）。"""
    notify_error(APP_TITLE, message)
    return code


def _backend_alive(handle: object) -> bool:
    """uvicorn 线程是否还活着（后端异常退出时不必等满超时）。"""
    is_alive = getattr(getattr(handle, "thread", None), "is_alive", None)
    return is_alive() if callable(is_alive) else True


def main(argv: Sequence[str] | None = None) -> int:
    """起服务 → 等就绪 → 开窗（阻塞）→ 关窗收尾；返回进程退出码。

    退出码沿用 ``api.__main__`` 的三档（端口被占 2 / 已有实例 3 / 起不来 1）；
    WebView2 缺失与就绪超时同属「起不来」，因此也是 1。
    """
    # 与 ``api.__main__.main`` 同一条纪律：stdio 编码由入口自己负责（#221/#239）。
    # 冻结成窗口程序后 stdout/stderr 可能是 None，``_force_utf8_stdio`` 会跳过它们。
    entry._force_utf8_stdio()
    args = parse_args(argv)

    status = detect_webview2()
    if status.state in ("missing", "outdated"):
        return _fail(entry.EXIT_STARTUP_FAILED, webview2_help_text(status))
    if status.state == "unknown":
        logger.warning("WebView2 版本不可判定，继续启动；若窗口打不开请按安装指引处理。")

    try:
        handle = entry.start(server_argv(args))
    except entry.PortInUseError as exc:
        return _fail(entry.EXIT_PORT_IN_USE, str(exc))
    except entry.AlreadyRunningError as exc:
        return _fail(entry.EXIT_ALREADY_RUNNING, str(exc))
    except entry.StartupError as exc:
        return _fail(entry.EXIT_STARTUP_FAILED, str(exc))
    except bootstrap.ConfigBootstrapError as exc:
        return _fail(entry.EXIT_STARTUP_FAILED, str(exc))

    try:
        try:
            wait_until_ready(
                handle.base_url, args.ready_timeout,
                is_alive=lambda: _backend_alive(handle),
            )
        except ReadinessError as exc:
            return _fail(entry.EXIT_STARTUP_FAILED, str(exc))
        try:
            launch_window(handle, debug_port=args.debug_port)
        except Exception as exc:  # noqa: BLE001 - 窗口层失败要给提示，不能裸 traceback
            return _fail(
                entry.EXIT_STARTUP_FAILED,
                f"窗口没能打开：{exc}\n"
                "若提示与 WebView2 有关，请按上面的安装指引处理；"
                "否则见 data/logs 下当天的日志。")
        return 0
    except KeyboardInterrupt:
        # Ctrl-C 与关窗同一条收尾：finally 里的 close() 停服务、放单实例锁。
        return 0
    finally:
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
