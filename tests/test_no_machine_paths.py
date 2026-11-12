"""本机绝对路径守卫（retro 2026-11-12）。

为什么：`scripts/accept_d5.py` 曾把 `D:/项目/python/MiniMax-H3-prompt/config/agent.yaml`
写成默认值，复审才抓出来；`scripts/ab_*.py` 的 usage 也带着同款路径。换机器就错，
而且把本机目录结构固化成仓库契约。

判据：`src/` 与 `scripts/` 里的 `.py`（随包/随仓分发的代码）不得出现「盘符 +
本机用户目录或项目目录」的路径。**外部依赖的默认值是合法白名单**（`D:/Comfyui/...`、
`D:\\素材库`、`C:\\Windows` 之类），判据只咬 `项目/` 与 `Users/` 两段。

`tests/` 不扫：那里的机器专属默认值（真实会话样本目录）本来就靠环境变量覆盖。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 盘符 + 本机目录：D:/项目、D:\项目、C:/Users、C:\Users
_LOCAL_PATH = re.compile(r"[A-Za-z]:[\\/](?:项目|Users)[\\/]")


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "src", "scripts"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line.endswith(".py")]


def test_no_machine_specific_paths_in_shipped_code():
    offenders: list[str] = []
    for path in _tracked_python_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if _LOCAL_PATH.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, "随仓代码里不许写本机绝对路径：\n" + "\n".join(offenders)
