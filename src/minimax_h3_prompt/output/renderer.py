"""输出渲染：写最终提示词文件 + 终端打印。"""
from __future__ import annotations

from pathlib import Path


def write_prompt(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
