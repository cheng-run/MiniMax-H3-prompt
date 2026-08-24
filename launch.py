"""项目唯一启动入口。

在项目根目录执行：
    uv run launch.py

命令行参数会原样传给内部 CLI，例如：
    uv run launch.py --brief examples/brief_fl2va.md
"""
from __future__ import annotations

import sys

from minimax_h3_prompt.main import main


if __name__ == "__main__":
    raise SystemExit(main())
