"""向导别再长大（体检 P1-5 / P5）。

``ui/wizard.py`` 是「读代码不全 → 功能残缺」的最大一处：拆之前 6408 行、149 个
顶层定义、fan-out 43。拆它是**增量**工作（一次抽一个流程、配金样），所以这里只钉
一条最简单也最有效的闸：**文件不许继续变长**。

要加流程就加进 ``ui/wizard_*.py``（第一刀是 ``wizard_display.py``）。真的非加在
向导里不可时，先把一段等价逻辑搬出去，让本用例保持绿——涨行数不是被禁止，而是
必须**用搬迁换**。
"""
from __future__ import annotations

from pathlib import Path

# 2026-11-02 抽出 wizard_display.py 之后的基线（原 6408 行）。
_BASELINE_LINES = 6267


def test_wizard_does_not_grow():
    path = Path(__file__).resolve().parents[1] / "src/minimax_h3_prompt/ui/wizard.py"
    lines = len(path.read_text(encoding="utf-8").splitlines())

    assert lines <= _BASELINE_LINES, (
        f"ui/wizard.py 从 {_BASELINE_LINES} 行长到了 {lines} 行。"
        "新流程请写进 ui/wizard_*.py；纯展示/纯判定搬进 wizard_display.py 一类模块，"
        "别继续往这个 6000 行的编排文件里加。")
