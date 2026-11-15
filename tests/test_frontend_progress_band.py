"""步骤带与「等待你操作」必须常驻在所有分区之上（spec #274 Implementation B / #276）。

为什么需要：D6 验收时用户在「分段」页交视频，界面完全看不到当前进度——步骤带当时
只长在「概览」panel 内部，切走就没了（#251 真实验收）。#276 把它上移到分区 tab
之外，但没有任何判据拦着下一个人把它再塞回某个 panel。这条守卫把「步骤带与
「等待你操作」提示渲染在第一个 tabpanel 之前（＝所有分区之上）」变成能红的判据：
把它们挪回概览 panel 内部，源码顺序反转，用例即红。

配套变异：``scripts/mutation_check.py::frontend_progress_band_inside_overview``。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DETAIL_VIEW = ROOT / "frontend" / "src" / "features" / "sessions" / "SessionDetailView.vue"


def test_step_band_and_pending_hint_render_above_all_panels() -> None:
    text = DETAIL_VIEW.read_text(encoding="utf-8")
    band = text.find("<ProgressBand")
    hint = text.find('class="pending-hint"')
    # 每个分区都是一块 role="tabpanel"；它在源码里的首次出现就是第一个分区的开始。
    first_panel = text.find('role="tabpanel"')

    assert band != -1, "SessionDetailView 里找不到 <ProgressBand>"
    assert hint != -1, "SessionDetailView 里找不到待办提示行"
    assert first_panel != -1, "SessionDetailView 里找不到任何 tabpanel"

    assert band < first_panel, (
        "步骤带被放进某个分区 panel 里了：切到非概览分区会看不到当前进度"
        "（spec #274 / #276）"
    )
    assert hint < first_panel, (
        "「等待你操作」提示被放进某个分区 panel 里了：切分区就看不到该自己干活"
        "（spec #274 / #276）"
    )
