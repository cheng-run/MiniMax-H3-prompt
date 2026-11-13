"""一键启动：起服务并打开浏览器，其余细节都在 ``api.__main__.start`` 里收口。

用法（仓库根，任意 shell）：

    .venv/Scripts/python.exe scripts/start_web.py

或双击仓库根的 ``start_web.cmd``。这里**只做三件事**：补前端构建产物、起服务、开浏览器；
端口、数据根、单实例、日志全走 ``api.__main__`` 的既有实现，不另写一套。

为什么要有这个文件：开发态的三种起法（Vite dev + 后端、同源 prod、桌面外壳）各自的
前置条件不一样，用户每次都得记 ``PYTHONPATH``、端口、要不要先 build。这里把「本机能
直接看界面」这条最短路径固定下来。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
DIST_INDEX = ROOT / "frontend" / "dist" / "index.html"
NODE_MODULES = ROOT / "frontend" / "node_modules"
DEV_PORT = 8000
HEALTH_PATH = "/api/v1/health"
PROBE_TIMEOUT_S = 1.5


def _probe(url: str) -> bool:
    """服务在不在：只认 /api/v1/health 有响应。"""
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_S):
            return True
    except urllib.error.URLError, OSError:
        return False


def _build_frontend(rebuild: bool) -> None:
    """缺产物才构建；显式 ``--rebuild`` 强制重来。

    ``node_modules`` 缺失时不自动安装——依赖下载按用户口径必须由人执行，这里只给
    可操作提示。``--no-install`` 也保证不会悄悄联网拉包。
    """
    if DIST_INDEX.is_file() and not rebuild:
        return
    if not NODE_MODULES.is_dir():
        raise SystemExit(
            f"[启动失败] 缺前端依赖：{NODE_MODULES}\n"
            "先在 frontend/ 目录执行 npx pnpm@11.7.0 install（下载需你本人执行）"
        )
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("[启动失败] 找不到 npx（Node 未安装或不在 PATH）")
    print("[build] 构建前端产物 frontend/dist ...", flush=True)
    proc = subprocess.run([npx, "--no-install", "vite", "build"], cwd=str(ROOT / "frontend"))
    if proc.returncode != 0:
        raise SystemExit(f"[启动失败] 前端构建退出码 {proc.returncode}")


def _open_browser(url: str) -> None:
    """开浏览器；开不起来也要把地址打出来，别让「双击没反应」变成哑巴。"""
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 浏览器坏了不该连服务一起停
        opened = False
    if not opened:
        print(f"[start] 没能自动打开浏览器，请手动访问：{url}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键启动 MiniMax-H3-prompt")
    parser.add_argument("--no-browser", action="store_true", help="只起服务，不开浏览器")
    parser.add_argument("--rebuild", action="store_true", help="强制重建前端产物")
    args, rest = parser.parse_known_args(argv)

    # 包没装进环境，等价于命令行里的 PYTHONPATH=src；放在这里而不是要求用户每次带上。
    sys.path.insert(0, str(ROOT / "src"))

    from minimax_h3_prompt.api.__main__ import (  # noqa: PLC0415
        AlreadyRunningError,
        PortInUseError,
        StartupError,
        start,
    )
    from minimax_h3_prompt import bootstrap  # noqa: PLC0415

    _build_frontend(args.rebuild)

    running = f"http://127.0.0.1:{DEV_PORT}"
    if _probe(running + HEALTH_PATH):
        # 已有实例：再起一个只会撞单实例锁，直接把界面打开。
        print(f"[start] 服务已在运行：{running}", flush=True)
        if not args.no_browser:
            _open_browser(running)
        return 0

    try:
        handle = start(rest)
    except PortInUseError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr, flush=True)
        return 2
    except AlreadyRunningError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr, flush=True)
        return 3
    except (StartupError, bootstrap.ConfigBootstrapError) as exc:
        print(f"[启动失败] {exc}", file=sys.stderr, flush=True)
        return 1

    url = handle.base_url.rstrip("/")
    print(f"[start] 已就绪：{url}（Ctrl-C 退出）", flush=True)
    if not args.no_browser:
        _open_browser(url)
    try:
        handle.wait()
    except KeyboardInterrupt:
        handle.stop()
    finally:
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
