"""本地服务启动入口：``python -m minimax_h3_prompt.api``。

只绑定 ``127.0.0.1:8000``——本地鉴权另有 Host/Origin 白名单与启动令牌（``api/auth.py``，
D3-1），但「只绑回环」是第一层：这一行改错，后面两层全白搭。
"""
from __future__ import annotations

import sys

import uvicorn

from .app import create_app

HOST = "127.0.0.1"
PORT = 8000


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


def main() -> None:
    _force_utf8_stdio()
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
