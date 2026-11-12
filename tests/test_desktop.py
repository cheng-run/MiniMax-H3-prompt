"""桌面外壳（D5-5 / #240）：标题取值、就绪探测、启动异常映射、关窗收尾、调试端口。

这一组用例**不建真窗口**：pywebview 的 ``create_window`` / ``start`` 全部走注入的假模块，
注册表读取也走注入的读函数。真实窗口与 EXE 的验收在 #242（CDP）与人工点验里。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import bootstrap, desktop
from minimax_h3_prompt.api import __main__ as entry


def make_info(*, port: int = 51234, version: str = "0.9.9",
              data_root: Path | None = None) -> entry.StartupInfo:
    return entry.StartupInfo(
        host="127.0.0.1",
        port=port,
        base_url=f"http://127.0.0.1:{port}/",
        data_root=data_root or Path("data"),
        frozen=True,
        version=version,
    )


class FakeHandle:
    """``entry.start`` 的替身：只提供外壳用得到的三样东西 + 记录 ``close``。"""

    def __init__(self, info: entry.StartupInfo, *, startup: entry.StartupInfo | None = None,
                 events: list[str] | None = None, alive: bool = True) -> None:
        self.info = info
        self.app = SimpleNamespace(
            state=SimpleNamespace(startup=startup) if startup is not None else SimpleNamespace())
        self.events = events if events is not None else []
        self.closed = 0
        self.alive = alive
        # 就绪探测会拿它判「后端是否还活着」。
        self.thread = SimpleNamespace(is_alive=lambda: self.alive)

    @property
    def base_url(self) -> str:
        return self.info.base_url

    def close(self) -> None:
        self.closed += 1
        self.events.append("close")


# --------------------------------------------------------------- 标题与地址来源

def test_window_title_carries_version_and_port_from_startup_info() -> None:
    """版本与端口都取自 ``StartupInfo``，外壳不自己重算（#240）。"""
    title = desktop.window_title(make_info(port=51234, version="0.9.9"))
    assert "0.9.9" in title
    assert "51234" in title


def test_window_title_without_version_still_names_the_port() -> None:
    """版本读不到时标题不能变成空壳：端口是排障必备信息。"""
    title = desktop.window_title(make_info(port=5001, version=""))
    assert "5001" in title
    assert title.strip() != ""


def test_startup_info_prefers_app_state_snapshot() -> None:
    """``app.state.startup`` 是启动落下的单一来源；外壳优先读它，其次才退回句柄。"""
    handle = FakeHandle(make_info(port=1111, version="from-handle"),
                        startup=make_info(port=2222, version="from-state"))
    info = desktop.startup_info(handle)
    assert info.port == 2222
    assert info.version == "from-state"


def test_startup_info_falls_back_to_handle_when_state_is_empty() -> None:
    handle = FakeHandle(make_info(port=3333, version="only-handle"))
    assert desktop.startup_info(handle) is handle.info


# ------------------------------------------------------------------- 就绪探测

def _start_health_server(failures: int):
    """起一个回环上的假健康端点：前 ``failures`` 次回 500，之后回 200。"""
    state = {"hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - 标准库约定
            state["hits"] += 1
            if state["hits"] <= failures:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"not ready")
                return
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # 测试不刷屏
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/"
    return server, state, base_url


def test_wait_until_ready_keeps_polling_until_health_returns_200() -> None:
    """先 500 后 200：探测必须继续等，不能第一次失败就放弃。"""
    server, state, base_url = _start_health_server(failures=2)
    try:
        desktop.wait_until_ready(base_url, timeout=10.0, interval=0.01)
        assert state["hits"] >= 3
    finally:
        server.shutdown()
        server.server_close()


def test_wait_until_ready_times_out_with_actionable_error() -> None:
    """超时的报错要能照着做：点名地址、时长，并指向日志。"""
    with pytest.raises(desktop.ReadinessError) as excinfo:
        desktop.wait_until_ready(
            "http://127.0.0.1:59999/", timeout=0.2, interval=0.02,
            probe=lambda url: False)
    message = str(excinfo.value)
    assert "http://127.0.0.1:59999/" in message
    assert "0.2" in message
    assert "日志" in message or "logs" in message


def test_wait_until_ready_reports_backend_thread_death() -> None:
    """后端线程提前退出时立刻报错，不白等满超时。"""
    with pytest.raises(desktop.ReadinessError) as excinfo:
        desktop.wait_until_ready(
            "http://127.0.0.1:59998/", timeout=30.0, interval=0.02,
            probe=lambda url: False, is_alive=lambda: False)
    assert "退出" in str(excinfo.value)


# --------------------------------------------------------- 启动异常 → 退出码与文案

class FakeWebview:
    """pywebview 的替身：只记录调用顺序与建窗参数，不碰任何 GUI。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        # 与真库同形的预置键：默认 ``None``，只有给了 --debug-port 才被赋值。
        self.settings = {"REMOTE_DEBUGGING_PORT": None}
        self.window_kwargs: dict[str, object] | None = None
        self.debug_port_at_start: object = "尚未 start"

    def create_window(self, title: str, **kwargs: object):
        self.events.append("create_window")
        self.window_kwargs = {"title": title, **kwargs}
        return SimpleNamespace(title=title)

    def start(self, func=None, args=None, **kwargs):  # 真库阻塞到最后一个窗口关闭
        self.events.append("webview.start")
        self.debug_port_at_start = self.settings["REMOTE_DEBUGGING_PORT"]


def install_shell_stubs(monkeypatch, *, handle=None, raise_exc=None,
                        probe=None, webview=None, status=None, events=None):
    """把 ``main`` 的四个外部接缝换成桩：起服务、健康探测、窗口库、WebView2 检测。"""
    events = events if events is not None else []
    webview = webview if webview is not None else FakeWebview(events)
    messages: list[tuple[str, str]] = []
    calls: list[str] = []

    def fake_start(argv):
        calls.append("start")
        events.append("start")
        if raise_exc is not None:
            raise raise_exc
        return handle

    monkeypatch.setattr(desktop.entry, "start", fake_start)
    monkeypatch.setattr(
        desktop, "probe_health",
        probe if probe is not None else lambda url, **kw: (events.append("probe"), True)[1])
    monkeypatch.setattr(desktop, "_load_webview", lambda: webview)
    monkeypatch.setattr(
        desktop, "detect_webview2",
        lambda **kw: status if status is not None else desktop.WebView2Status("ok", "157.0"))
    monkeypatch.setattr(
        desktop, "notify_error", lambda title, message: messages.append((title, message)))
    return SimpleNamespace(events=events, webview=webview, messages=messages, calls=calls)


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (lambda: entry.PortInUseError("端口 9123 已被占用，服务没有启动。"),
         entry.EXIT_PORT_IN_USE),
        (lambda: entry.AlreadyRunningError("数据根 D:/x 已经有一个服务在运行。"),
         entry.EXIT_ALREADY_RUNNING),
        (lambda: entry.StartupError("服务启动失败（端口 9123）：uvicorn 在就绪前退出。"),
         entry.EXIT_STARTUP_FAILED),
        (lambda: bootstrap.ConfigBootstrapError("配置模板缺失：config/agent.yaml"),
         entry.EXIT_STARTUP_FAILED),
    ],
    ids=["port-in-use", "already-running", "startup-failed", "config-bootstrap"],
)
def test_startup_errors_map_to_exit_codes_and_keep_their_message(monkeypatch, factory, code) -> None:
    """四类启动失败各给既有退出码；提示里保留原始可操作信息，不吞成裸 traceback。"""
    exc = factory()
    stubs = install_shell_stubs(monkeypatch, raise_exc=exc)

    assert desktop.main([]) == code
    assert stubs.messages, "启动失败必须给可操作提示"
    _, message = stubs.messages[-1]
    assert str(exc) in message
    assert "create_window" not in stubs.events, "起不来就不该建窗"
    assert "probe" not in stubs.events, "起不来就不该探测"
    assert stubs.webview.window_kwargs is None


# ------------------------------------------------------------------- 关窗收尾

def test_window_close_runs_probe_then_window_then_close(monkeypatch) -> None:
    """顺序契约：起服务 → 探测就绪 → 建窗 → start（阻塞）→ 关窗后 close 放锁。"""
    events: list[str] = []
    info = make_info(port=51234, version="1.2.3")
    handle = FakeHandle(info, events=events)
    stubs = install_shell_stubs(monkeypatch, handle=handle, events=events)

    assert desktop.main([]) == 0

    assert events == ["start", "probe", "create_window", "webview.start", "close"]
    assert handle.closed == 1
    kwargs = stubs.webview.window_kwargs
    assert kwargs is not None
    assert kwargs["url"] == info.base_url
    # 不加原生桥：令牌仍由页面自己走 /api/v1/auth/token + Cookie 引导。
    assert "js_api" not in kwargs
    # 地址里不带令牌（D3 的约束）。
    assert "token" not in str(kwargs["url"])
    assert "?" not in str(kwargs["url"])
    assert "1.2.3" in str(kwargs["title"])
    assert "51234" in str(kwargs["title"])


def test_readiness_timeout_never_builds_a_window_and_still_closes(monkeypatch) -> None:
    """后端没就绪：不开空白窗口，给可操作错误，服务照常收掉。"""
    events: list[str] = []
    handle = FakeHandle(make_info(port=51235), events=events)
    stubs = install_shell_stubs(
        monkeypatch, handle=handle, probe=lambda url, **kw: False)

    assert desktop.main(["--ready-timeout", "0.05"]) == entry.EXIT_STARTUP_FAILED

    assert "create_window" not in events
    assert handle.closed == 1
    _, message = stubs.messages[-1]
    assert "http://127.0.0.1:51235/" in message


def test_backend_dying_before_ready_is_reported_as_such(monkeypatch) -> None:
    """后端线程提前退出：报「退出了」而不是干等超时。"""
    handle = FakeHandle(make_info(port=51236), alive=False)
    stubs = install_shell_stubs(
        monkeypatch, handle=handle, probe=lambda url, **kw: False)

    assert desktop.main(["--ready-timeout", "30"]) == entry.EXIT_STARTUP_FAILED
    _, message = stubs.messages[-1]
    assert "退出" in message


# ------------------------------------------------------------------- 调试端口

def test_apply_debug_port_leaves_settings_untouched_without_a_flag() -> None:
    """默认不开调试端口：不设这个键，WebView2 就不会多开一个 CDP 监听。"""
    settings: dict[str, object] = {"REMOTE_DEBUGGING_PORT": None}
    desktop.apply_debug_port(settings, None)
    assert settings == {"REMOTE_DEBUGGING_PORT": None}


def test_apply_debug_port_sets_the_pywebview_setting_key() -> None:
    """必须走 ``webview.settings['REMOTE_DEBUGGING_PORT']``：环境变量那条实测不通（#236）。"""
    settings: dict[str, object] = {"REMOTE_DEBUGGING_PORT": None}
    desktop.apply_debug_port(settings, 9333)
    assert desktop.REMOTE_DEBUGGING_SETTING == "REMOTE_DEBUGGING_PORT"
    assert settings["REMOTE_DEBUGGING_PORT"] == 9333


def test_debug_port_flag_reaches_webview_before_start(monkeypatch) -> None:
    stubs = install_shell_stubs(monkeypatch, handle=FakeHandle(make_info()))
    assert desktop.main(["--debug-port", "9333"]) == 0
    assert stubs.webview.debug_port_at_start == 9333


def test_no_debug_port_flag_keeps_the_setting_unset(monkeypatch) -> None:
    stubs = install_shell_stubs(monkeypatch, handle=FakeHandle(make_info()))
    assert desktop.main([]) == 0
    assert stubs.webview.debug_port_at_start is None


# --------------------------------------------------------------- 参数透传

def test_server_argv_forwards_only_the_api_flags() -> None:
    """外壳自己的参数（--debug-port / --ready-timeout）不能漏给 ``entry.start``。"""
    args = desktop.parse_args(
        ["--port", "9123", "--data-root", "D:/data", "--debug-port", "9333",
         "--ready-timeout", "5"])
    assert desktop.server_argv(args) == ["--port", "9123", "--data-root", "D:/data"]
    assert args.debug_port == 9333
    assert args.ready_timeout == 5


def test_shell_passes_port_and_data_root_through_to_the_server(monkeypatch) -> None:
    """端口/数据根仍是 ``api.__main__`` 的活：外壳原样转发，不另算一份。"""
    install_shell_stubs(monkeypatch, handle=FakeHandle(make_info()))
    seen: list[list[str]] = []
    original = desktop.entry.start

    def recording_start(argv):
        seen.append(list(argv))
        return original(argv)

    monkeypatch.setattr(desktop.entry, "start", recording_start)
    assert desktop.main(["--port", "0", "--data-root", "D:/fake-data"]) == 0
    assert seen == [["--port", "0", "--data-root", "D:/fake-data"]]


# ------------------------------------------------------------------- WebView2

def test_detect_webview2_reads_the_version_and_calls_it_ok() -> None:
    status = desktop.detect_webview2(platform="win32", reader=lambda *a: "157.0.4341.42")
    assert status == desktop.WebView2Status("ok", "157.0.4341.42")
    assert status.state == "ok"
    assert status.version == "157.0.4341.42"


def test_detect_webview2_reports_missing_when_no_key_has_pv() -> None:
    status = desktop.detect_webview2(platform="win32", reader=lambda *a: None)
    assert status.state == "missing"
    assert status.version == ""


def test_detect_webview2_flags_a_too_old_runtime() -> None:
    """主版本低于下限：拦下，而不是让用户对着白窗口猜。"""
    status = desktop.detect_webview2(
        platform="win32", reader=lambda *a: f"{desktop.MIN_WEBVIEW2_MAJOR - 1}.0.1.0")
    assert status.state == "outdated"
    assert status.version.startswith(str(desktop.MIN_WEBVIEW2_MAJOR - 1))


def test_detect_webview2_treats_an_unparsable_version_as_unknown() -> None:
    """版本串读不懂时不硬阻断（宁可警告不硬阻断），把原串留给日志。"""
    status = desktop.detect_webview2(platform="win32", reader=lambda *a: "unknown-build")
    assert status.state == "unknown"
    assert status.version == "unknown-build"


def test_detect_webview2_is_undecidable_off_windows_without_touching_the_registry() -> None:
    """非 Windows 必须安全返回「不可判定」，且不去读注册表（winreg 在那儿根本 import 不了）。"""
    calls: list[tuple[str, str, str]] = []
    status = desktop.detect_webview2(
        platform="linux", reader=lambda *a: calls.append(a) or "157.0.0.0")
    assert status.state == "unknown"
    assert calls == []


def test_read_webview2_version_probes_wow6432node_and_both_registry_views() -> None:
    """两个视图都试：只认 64 位视图会漏掉最常见的 WOW6432Node 落点（票面要求）。"""
    seen: list[tuple[str, str, str]] = []

    def reader(hive, path, view):
        seen.append((hive, path, view))
        return None

    assert desktop.read_webview2_version(platform="win32", reader=reader) is None
    assert any("WOW6432Node" in path for _, path, _ in seen)
    assert {view for _, _, view in seen} >= {"32", "64"}
    assert all(desktop.WEBVIEW2_CLIENT_GUID in path for _, path, _ in seen)


def test_missing_webview2_blocks_launch_with_install_instructions(monkeypatch) -> None:
    """缺运行时：给「怎么装」并退出，一个服务进程都不起（也就没有白屏）。"""
    stubs = install_shell_stubs(monkeypatch, status=desktop.WebView2Status("missing"))

    assert desktop.main([]) == entry.EXIT_STARTUP_FAILED
    assert stubs.calls == [], "WebView2 缺失时不该起服务"
    _, message = stubs.messages[-1]
    assert "WebView2" in message
    assert "developer.microsoft.com" in message


def test_outdated_webview2_blocks_launch_too(monkeypatch) -> None:
    stubs = install_shell_stubs(
        monkeypatch, status=desktop.WebView2Status("outdated", "89.0.0.0"))
    assert desktop.main([]) == entry.EXIT_STARTUP_FAILED
    assert stubs.calls == []
    _, message = stubs.messages[-1]
    assert "89.0.0.0" in message


def test_unknown_webview2_does_not_block_launch(monkeypatch) -> None:
    stubs = install_shell_stubs(
        monkeypatch, handle=FakeHandle(make_info()), status=desktop.WebView2Status("unknown"))
    assert desktop.main([]) == 0
    assert stubs.webview.window_kwargs is not None


# ------------------------------------------------------------- 窗口层失败兜底

def test_window_failure_is_reported_and_the_server_still_closes(monkeypatch) -> None:
    """WebView2 装坏了导致建窗抛错：给可操作提示、收服务，不裸 traceback。"""
    handle = FakeHandle(make_info())

    class ExplodingWebview(FakeWebview):
        def create_window(self, title: str, **kwargs: object):
            raise RuntimeError("WebView2 runtime crashed")

    stubs = install_shell_stubs(
        monkeypatch, handle=handle, webview=ExplodingWebview([]))

    assert desktop.main([]) == entry.EXIT_STARTUP_FAILED
    assert handle.closed == 1
    _, message = stubs.messages[-1]
    assert "窗口没能打开" in message
    assert "WebView2 runtime crashed" in message

