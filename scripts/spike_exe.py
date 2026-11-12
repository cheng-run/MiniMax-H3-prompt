"""D5-1 打包 spike：#236「Python 3.14 + PyInstaller + pywebview」的最小窗口程序。

这个文件**不接业务**——它只回答一个问题：这条打包链路能不能产出一个
双击打开、且能被 Playwright 通过 CDP 连上的 Windows 窗口。真实结论与版本号
见 ``docs/research/2026-11-12-exe-packaging-spike.md``。

喂给 PyInstaller 的入口就是这个文件本身（``scripts/build_spike_exe.py`` 负责参数）。
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

# 本地页面文件名：构建脚本按同一个常量把它作为 ``--add-data`` 打进包，
# 冻结与源码运行都从 ``resolve_page_dir`` 找它（两处共用一个名字，不各写一份）。
PAGE_NAME = "spike_page.html"


def resolve_page_dir(meipass: str | None, file_dir: Path) -> Path:
    """本地页面所在目录：冻结后是 PyInstaller 的解包根，源码运行是脚本目录。"""
    return Path(meipass) if meipass else Path(file_dir)


def page_url(page_dir: Path) -> str:
    """把本地页面变成 WebView 能加载的 ``file:///`` URI。"""
    return (Path(page_dir) / PAGE_NAME).resolve().as_uri()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="spike_exe",
        description="D5-1 打包 spike：打开一个本地页面窗口，读秒自行关闭。",
    )
    parser.add_argument(
        "--auto-close-seconds",
        type=float,
        default=10.0,
        help="窗口存活秒数，到点自行关闭（无人值守环境不能留下要点掉的窗口）",
    )
    parser.add_argument(
        "--debug-port",
        type=int,
        default=None,
        help="给 WebView2 开远程调试端口（CDP），供 Playwright 连窗口",
    )
    return parser.parse_args(argv)


def report_versions() -> dict[str, str]:
    """把实证要记的版本号一次取齐（3.14 上这些包的兼容性就是本 spike 的结论）。"""
    from importlib.metadata import PackageNotFoundError, version

    versions = {"python": sys.version.split()[0]}
    for package in ("pywebview", "pythonnet", "clr-loader", "pyinstaller"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "未安装"
    return versions


def _after_start(window: "object", seconds: float, log: Callable[[str], None]) -> None:
    """GUI 循环起来之后跑：等页面加载 → 读标题/URL → 读秒 → 自己关窗。

    这必须是自动的：验收在无人值守环境里跑，留一个要点掉的窗口就会把流程卡死。
    """
    loaded = window.events.loaded.wait(30)  # noqa: SLF001  pywebview 的公开事件容器
    log(f"[spike] loaded={loaded}")
    log(f"[spike] title={window.title!r}")
    log(f"[spike] url={window.get_current_url()!r}")
    log(f"[spike] probe={window.evaluate_js('document.getElementById(\"probe\").textContent')!r}")
    # 日志走 ASCII：冻结后控制台按 cp936 输出，Git Bash 终端读中文会乱码
    log(f"[spike] auto-close in {seconds:g}s")
    time.sleep(seconds)
    window.destroy()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = _logger()
    for name, value in report_versions().items():
        log(f"[spike] {name}={value}")

    import webview

    page_dir = resolve_page_dir(getattr(sys, "_MEIPASS", None), Path(__file__).resolve().parent)
    url = page_url(page_dir)
    log(f"[spike] page={url}")

    if args.debug_port is not None:
        # pywebview 的 edgechromium 后端把它翻译成 WebView2 的
        # --remote-debugging-port（platforms/edgechromium.py 的
        # REMOTE_DEBUGGING_PORT 分支）。也可以走 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS。
        webview.settings["REMOTE_DEBUGGING_PORT"] = args.debug_port
        log(f"[spike] cdp=http://127.0.0.1:{args.debug_port}")

    window = webview.create_window("H3 EXE Spike", url=url, width=960, height=640)
    webview.start(_after_start, (window, args.auto_close_seconds, log), debug=False)
    log("[spike] window closed, exiting")
    return 0


def _logger() -> Callable[[str], None]:
    """冻结成窗口程序后 stdout 可能是 None，print 会炸——所以统一走这里兜底。"""

    def _log(message: str) -> None:
        if sys.stdout is not None:
            print(message, flush=True)

    return _log


if __name__ == "__main__":
    sys.exit(main())
