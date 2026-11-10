"""把 FastAPI 的 OpenAPI 契约导出到 ``contracts/openapi.json``。

Python 是契约唯一真源；这个脚本把真源落成可提交、可 diff 的文件，CI 会重新导出并
``git diff --exit-code`` 做 drift check。输出必须确定性：``sort_keys`` 固定键序、
``ensure_ascii=False`` 保留中文、2 空格缩进、LF 换行——否则同一份契约在不同平台/机器
上会来回改，drift check 变成噪声。

``--out`` 可把结果写到别处（``check.sh`` 用它导出到临时文件，再与**工作区**里的
契约逐字节比）：本地开发时契约改动还没提交，直接和 HEAD 比会把「未提交」误报成
「漂移」（2026-11-10 #223 实测白跑一轮全量）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from minimax_h3_prompt.api.app import create_app

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "openapi.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="导出 OpenAPI 契约")
    parser.add_argument("--out", default=str(CONTRACT_PATH),
                        help="输出路径（默认 contracts/openapi.json）")
    out = Path(parser.parse_args(argv).out)
    schema = create_app().openapi()
    text = json.dumps(schema, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 是关键：Windows 文本模式默认把 \n 翻成 \r\n，会与 CI（Linux）漂移。
    out.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
