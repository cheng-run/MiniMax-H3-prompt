"""本地服务启动入口：``python -m minimax_h3_prompt.api``。

三件事在这里收口（spec #235 §5）：

1. **端口**：开发形态仍是 8000（Vite 代理写死 ``127.0.0.1:8000``，不能跟着随机）；
   冻结形态默认挑一个空闲端口。``--port N`` 指定，``--port 0`` = 自动；**显式指定被占用
   时报错退出并点名该端口**，不静默换端口。
2. **数据根**：``--data-root`` 可覆盖（spec §6「验收与调试用」）。**argv 必须在任何业务
   import 之前解析**——``config = Config()`` 是模块级单例，连 ``api.app`` 一被 import 就
   会建出来（``observability`` 在模块顶层 ``from .config import config``），迟到的覆盖
   不会回头改已经算好的路径。所以本模块顶层只 import 轻量的 ``uvicorn`` 与 ``paths``。
3. **单实例**：同一数据根起第二个实例时提示后退出（``single_instance``）。

实际绑定的端口不能从 ``uvicorn.run()`` 拿（它不返回监听信息），因此自建
``uvicorn.Server`` + ``Config``，启动后从 ``server.servers[0].sockets[0]`` 读端口。
实际地址的**单一来源**是 :class:`StartupInfo`（同时挂到 ``app.state.startup`` /
``app.state.base_url``），启动日志与后续窗口标题（D5-5）都读它，不各算一份。

只绑回环是本地鉴权的第一层（``api/auth.py`` 的 Host/Origin 白名单与启动令牌在它之后）：
这一行改错，后面两层全白搭。
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import uvicorn

from .. import paths
from . import single_instance

HOST = "127.0.0.1"
# 开发形态的固定端口：前端 Vite 代理写死这个值（``frontend/vite.config.ts``）。
DEV_PORT = 8000
# ``--port 0`` / 冻结态默认：让内核分配一个空闲端口。
PORT_AUTO = 0

# 启动失败的各档退出码：验收脚本与窗口外壳按它区分「端口被占」「已有实例」「起不来」。
EXIT_STARTUP_FAILED = 1
EXIT_PORT_IN_USE = 2
EXIT_ALREADY_RUNNING = 3

# 等 uvicorn 报告 started 的上限。本机实测启动 < 2s；60s 是给慢磁盘/杀软扫描留的余量。
_STARTUP_TIMEOUT_S = 60.0

logger = logging.getLogger(__name__)


class PortInUseError(RuntimeError):
    """显式指定的端口已被占用。"""


class AlreadyRunningError(RuntimeError):
    """同一数据根已有实例在跑。"""


class StartupError(RuntimeError):
    """服务没能在超时内起来（绑定失败、后端异常退出等）。"""


@dataclass(frozen=True)
class StartupInfo:
    """一次启动的事实：实际地址的单一来源（启动日志、``app.state``、D5-5 窗口标题）。

    冻结/开发形态也在这里——诊断包（D5-4）与窗口外壳要按它决定文案，不再各自去问
    ``sys.frozen``。
    """

    host: str
    port: int
    base_url: str
    data_root: Path
    frozen: bool

    def startup_line(self) -> str:
        """启动日志行（字段名就是验收脚本读的契约，改字段名等于改验收）。"""
        form = "冻结" if self.frozen else "开发"
        return f"[启动] 地址={self.base_url}；数据根={self.data_root}；形态={form}"


class ServerHandle:
    """运行中的服务：``info`` 是实际地址，``stop()`` 收服务，``close()`` 连单实例锁一起放。"""

    def __init__(self, app: object, server: uvicorn.Server, info: StartupInfo,
                 thread: threading.Thread, lock: single_instance.InstanceLock) -> None:
        self.app = app
        self.server = server
        self.info = info
        self.thread = thread
        self._lock = lock

    @property
    def base_url(self) -> str:
        return self.info.base_url

    @property
    def port(self) -> int:
        return self.info.port

    def stop(self) -> None:
        """请求 uvicorn 退出并等它收尾（幂等）。"""
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=30)

    def wait(self) -> None:
        """阻塞到服务退出（Ctrl-C 抛 ``KeyboardInterrupt``，由 :func:`main` 收）。"""
        self.thread.join()

    def close(self) -> None:
        """收服务并释放单实例锁（幂等）。"""
        self.stop()
        self._lock.release()


def _force_utf8_stdio() -> None:
    """把标准输出/错误切到 UTF-8。

    为什么必须做：Windows 控制台默认 GBK，而阶段 1 的进度横幅含 ``▶``（U+25B6）
    等字符；一次 ``print`` 编不出来就会把整个后台 job 炸成 ``UnicodeEncodeError``
    （#221 真实跑实测）。CLI 一直有 ``PYTHONIOENCODING`` 兜着才没暴露，服务化后
    不能再依赖启动环境。``errors="replace"`` 保证即使遇到不可编码字符也只降级显示，
    不允许「打印失败」变成「任务失败」。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:  # pragma: no cover - 非常规流
            print(f"[警告] 无法切换标准流编码：{exc}", file=sys.stderr)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m minimax_h3_prompt.api",
        description="MiniMax H3 Prompt 本地服务（只绑 127.0.0.1）。",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="监听端口；0 = 自动挑一个空闲端口（默认：开发形态 8000，冻结形态自动）。",
    )
    parser.add_argument(
        "--data-root", default=None,
        help="可写数据根（会话/产物/日志/配置）；默认：开发形态仓库根，冻结形态 EXE 同级 data/。",
    )
    return parser.parse_args(argv)


def default_port() -> int:
    """没给 ``--port`` 时的端口：开发形态 8000，冻结形态自动。"""
    return PORT_AUTO if paths.is_frozen() else DEV_PORT


def resolve_port(requested: int | None) -> int:
    """把命令行取值翻成实际要绑的端口；``None`` 走形态默认。"""
    return default_port() if requested is None else requested


def ensure_port_available(host: str, port: int) -> None:
    """显式端口的事先探测：被占用时立刻报错，不让 uvicorn 静默退出。

    ``port == 0`` 不用探测（内核分配）。探测与真正 bind 之间存在竞态，因此
    :func:`_wait_until_started` 还会在 uvicorn 启动失败时补一次带端口的报错。
    """
    if port == PORT_AUTO:
        return
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise PortInUseError(
                f"端口 {port} 已被占用，服务没有启动。请关掉占用它的程序，"
                f"或用 --port 换一个端口（--port 0 = 自动）。") from exc


def _wait_until_started(server: uvicorn.Server, thread: threading.Thread, port: int) -> None:
    """等 uvicorn 报告 started；线程提前退出（绑定失败等）就当启动失败。"""
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while not server.started:
        if not thread.is_alive():
            raise StartupError(
                f"服务启动失败（端口 {port}）：uvicorn 在就绪前退出。"
                "端口可能刚好被别的进程抢走，或后端初始化报错——详见上面的日志。")
        if time.monotonic() > deadline:
            raise StartupError(
                f"服务在 {_STARTUP_TIMEOUT_S:.0f}s 内没有就绪（端口 {port}），已放弃启动。")
        time.sleep(0.02)


def start(argv: Sequence[str] | None = None) -> ServerHandle:
    """解析 argv → 定数据根 → 起服务，返回带**实际地址**的句柄。

    失败时抛 :class:`PortInUseError` / :class:`AlreadyRunningError` /
    :class:`StartupError`；成功时调用方负责 ``wait()`` 与 ``close()``。
    """
    _force_utf8_stdio()
    args = parse_args(argv)
    # 顺序是硬约束：数据根必须在 import ``.app``（连带 config 单例）之前落下。
    paths.set_data_root(args.data_root)
    port = resolve_port(args.port)
    ensure_port_available(HOST, port)

    lock = single_instance.acquire(paths.data_root())
    if not lock.acquired:
        lock.release()
        raise AlreadyRunningError(lock.message)

    try:
        from .app import create_app

        app = create_app()
        config = uvicorn.Config(app, host=HOST, port=port, log_level="info")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="uvicorn-server", daemon=True)
        thread.start()
        _wait_until_started(server, thread, port)
        # 实际端口从监听 socket 读：``--port 0`` 时这里是内核分配的那个数。
        bound_port = server.servers[0].sockets[0].getsockname()[1]
        info = StartupInfo(
            host=HOST,
            port=bound_port,
            base_url=f"http://{HOST}:{bound_port}/",
            data_root=paths.data_root(),
            frozen=paths.is_frozen(),
        )
        # 单一来源：窗口标题（D5-5）与诊断包（D5-4）都读这两处，不各自重算端口。
        app.state.startup = info
        app.state.base_url = info.base_url
        logger.info("%s", info.startup_line())
        print(info.startup_line(), flush=True)
        return ServerHandle(app, server, info, thread, lock)
    except BaseException:
        # 起不来的路径（含 Ctrl-C）不许把互斥体句柄漏在进程里。
        lock.release()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """启动并阻塞到退出；返回进程退出码。"""
    try:
        handle = start(argv)
    except PortInUseError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr, flush=True)
        return EXIT_PORT_IN_USE
    except AlreadyRunningError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr, flush=True)
        return EXIT_ALREADY_RUNNING
    except StartupError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr, flush=True)
        return EXIT_STARTUP_FAILED
    try:
        handle.wait()
    except KeyboardInterrupt:
        handle.stop()
    finally:
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
