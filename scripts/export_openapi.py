"""把 FastAPI 的 OpenAPI 契约导出到 ``contracts/openapi.json``。

Python 是契约唯一真源；这个脚本把真源落成可提交、可 diff 的文件，CI 会重新导出并
``git diff --exit-code`` 做 drift check。输出必须确定性：``sort_keys`` 固定键序、
``ensure_ascii=False`` 保留中文、2 空格缩进、LF 换行——否则同一份契约在不同平台/机器
上会来回改，drift check 变成噪声。
"""
from __future__ import annotations

import json
from pathlib import Path

from minimax_h3_prompt.api.app import create_app

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "openapi.json"


def main() -> None:
    schema = create_app().openapi()
    text = json.dumps(schema, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 是关键：Windows 文本模式默认把 \n 翻成 \r\n，会与 CI（Linux）漂移。
    CONTRACT_PATH.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
