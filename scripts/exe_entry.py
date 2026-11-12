"""PyInstaller 冻结入口（D5-6 / #241）。

正常启动原样转给桌面外壳（``desktop.main``，D5-5 / #240）；``--self-check`` 转给
``selfcheck.main``——只跑冻结环境、ffmpeg 与桩管线阶段 1/2，不开窗口。

**为什么单独一个入口脚本**：``--self-check`` 是打包与 CI 的关注点，不该长进业务外壳
的公开命令行；它写在这里，``desktop.py`` 一个字节都不改。
"""
from __future__ import annotations

import sys

from minimax_h3_prompt import desktop

SELF_CHECK_FLAG = "--self-check"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [SELF_CHECK_FLAG]:
        from minimax_h3_prompt import selfcheck

        return selfcheck.main(args[1:])
    return desktop.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
