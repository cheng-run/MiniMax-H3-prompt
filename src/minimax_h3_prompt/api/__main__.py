"""本地服务启动入口：``python -m minimax_h3_prompt.api``。

只绑定 ``127.0.0.1:8000``——D1 不做 Origin/Host 校验与启动令牌，因此绝不能监听
外网地址。Origin/Host 校验归 D3。
"""
from __future__ import annotations

import uvicorn

from .app import create_app

HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
