"""用 Playwright 经 CDP 连 spike 的真实窗口（#236 结论 3：验收能否自动化）。

先起一个带调试端口的 spike，再跑本脚本：

    # 终端 1（或后台）
    python scripts/spike_exe.py --debug-port 9333 --auto-close-seconds 30
    # 终端 2
    python scripts/spike_cdp_check.py --port 9333

退出码 0 ＝ 连上并读到本地页面标题；非 0 ＝ 连不上（结论记「CDP 不可用」，验收退到
「外部浏览器 + 人工点窗口」的兜底）。窗口是 spike 自己关的，本脚本只读不管生命周期。
"""
from __future__ import annotations

import argparse
import sys
import time


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="spike_cdp_check",
        description="Playwright connect_over_cdp 连 spike 窗口，读页面标题/URL。",
    )
    parser.add_argument("--port", type=int, required=True, help="spike 的调试端口")
    parser.add_argument("--expect-title", default="H3 EXE Spike", help="期望的页面标题")
    parser.add_argument("--timeout", type=float, default=30.0, help="等窗口出现的总秒数")
    return parser.parse_args(argv)


def check(port: int, expect_title: str, timeout: float) -> int:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    with sync_playwright() as p:
        while time.monotonic() < deadline:
            try:
                browser = p.chromium.connect_over_cdp(endpoint)
            except PlaywrightError as exc:  # 窗口还没起好：轮询到超时为止
                last_error = exc
                time.sleep(1.0)
                continue
            try:
                for context in browser.contexts:
                    for page in context.pages:
                        title, url = page.title(), page.url
                        print(f"[cdp] page title={title!r} url={url!r}")
                        if title == expect_title:
                            probe = page.locator("#probe").text_content()
                            print(f"[cdp] probe={probe!r}")
                            print("[cdp] PASS：CDP 连上了真实窗口")
                            return 0
                last_error = RuntimeError(f"连上了 {endpoint}，但没有名为 {expect_title!r} 的页面")
            finally:
                browser.close()
            time.sleep(1.0)
    print(f"[cdp] FAIL：{last_error}")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return check(args.port, args.expect_title, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
