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
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = ROOT / "frontend"
DEV_PORT = 8000
HEALTH_PATH = "/api/v1/health"
PROBE_TIMEOUT_S = 1.5

# 「前端源码树」的范围。列 glob 而不是写死一长串文件：`src/**/*` 递归收全部页面与组件，
# 新增文件不必回来改这里；少列一种反而会让改动静默不触发重建。
# 依赖清单（spec #274 §A）也算源码树：只跑 `pnpm install` 换了依赖树、dist 仍是按旧依赖
# 构建的产物，脚本却判「新鲜」——浏览器里就跑在与磁盘代码不匹配的运行时上。
# `eslint` / `prettier` / `vitest` / `playwright` 的配置不参与 vite 构建，故意不收。
SOURCE_TREE_PATTERNS: tuple[str, ...] = (
    "index.html",
    "vite.config.ts",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig*.json",
    "src/**/*",
)

BUILD_REASON_FORCED = "显式要求强制重建"
BUILD_REASON_DIST_MISSING = "前端产物缺失"
BUILD_REASON_SOURCE_NEWER = "检测到前端源码较新"


def _probe(url: str) -> bool:
    """服务在不在：只认 /api/v1/health 有响应。"""
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_S):
            return True
    except urllib.error.URLError, OSError:
        return False


def _newest_source_mtime(frontend_root: Path) -> float | None:
    """前端源码树里最新的文件 mtime；一个文件都没找到时返回 None。"""
    newest: float | None = None
    for pattern in SOURCE_TREE_PATTERNS:
        for path in frontend_root.glob(pattern):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def build_reason(
    frontend_root: Path, dist_index: Path, *, rebuild: bool
) -> str | None:
    """要不要重建前端产物、为什么；``None`` 表示产物新鲜、不必构建。

    判据用 mtime 而不是 git HEAD：用户常在脏工作区上跑，HEAD 没变而源码已经改过，
    按 HEAD 判会静默给出旧界面（#274 验收实际撞到的形态）。mtime 判错的唯一方向是
    「多重建一次」（约 1 秒），可接受；反过来「源码改了还说产物新」不可接受，所以
    找不到任何源码文件时也按需要构建处理。
    """
    if rebuild:
        return BUILD_REASON_FORCED
    if not dist_index.is_file():
        return BUILD_REASON_DIST_MISSING
    newest = _newest_source_mtime(frontend_root)
    if newest is None or newest > dist_index.stat().st_mtime:
        return BUILD_REASON_SOURCE_NEWER
    return None


def _vite_build(frontend_root: Path) -> Callable[[], int]:
    """默认构建器：找得到 npx 才构建，找不到明确报错；返回进程退出码。"""
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("[启动失败] 找不到 npx（Node 未安装或不在 PATH）")

    def run() -> int:
        return subprocess.run(
            [npx, "--no-install", "vite", "build"], cwd=str(frontend_root)
        ).returncode

    return run


def _build_frontend(
    rebuild: bool,
    *,
    frontend_root: Path = FRONTEND_ROOT,
    dist_index: Path | None = None,
    node_modules: Path | None = None,
    build: Callable[[], int] | None = None,
) -> None:
    """按新鲜度决定要不要重建前端；显式 ``--rebuild`` 强制重来。

    ``node_modules`` 缺失时**不自动安装**——依赖下载按用户口径必须由人执行，这里只给
    可操作提示并以非零码退出。源码与产物都新鲜时不构建、不打印，免得每次启动刷一行
    无用的话。``build`` 是执行构建的接缝，测试注入假构建器，不真跑 Vite。
    """
    if dist_index is None:
        dist_index = frontend_root / "dist" / "index.html"
    if node_modules is None:
        node_modules = frontend_root / "node_modules"

    reason = build_reason(frontend_root, dist_index, rebuild=rebuild)
    if reason is None:
        return
    if not node_modules.is_dir():
        raise SystemExit(
            f"[启动失败] 缺前端依赖：{node_modules}\n"
            "先在 frontend/ 目录执行 npx pnpm@11.7.0 install（下载需你本人执行）"
        )
    if build is None:
        build = _vite_build(frontend_root)
    print(f"[build] {reason}，重新构建前端产物 ...", flush=True)
    code = build()
    if code != 0:
        raise SystemExit(f"[启动失败] 前端构建退出码 {code}")
    print("[build] 已重新构建前端产物", flush=True)


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
