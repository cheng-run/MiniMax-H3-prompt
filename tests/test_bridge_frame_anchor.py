"""桥接帧锚定回路（issue #12）：中间段写段请求必须携带 Picture 1 实际画面与一致性纪律句。

用 GEN004 真实产物裁剪版 fixture 驱动 `build_segment_v2_request`（纯函数，不需要 LLM/GPU）。

当前缺陷（#12）：`_frame_anchor_note` 的纪律句只在第 1 段发出，中间段拿不到
桥接帧内容 → 段 2、3 红。以 strict xfail 钉住：#12 修复后 XPASS 会强制摘标。

RED = 请求里没有 Picture 1 的实际画面描述 / 没有「开场状态与图逐项一致」纪律句
GREEN = 有（修复后）
"""
import json
import types
from pathlib import Path

import pytest

from minimax_h3_prompt.segment_prompts import build_segment_v2_request

FIXTURES = Path(__file__).resolve().parent / "fixtures"

DISCIPLINE_MARKERS = ["逐项一致", "开场状态必须与这张图"]
FRAME_CONTENT_MARKER = "实际画面"


def _load_gen004():
    plans = []
    for r in json.loads((FIXTURES / "gen004_plan.json").read_text(encoding="utf-8")):
        p = types.SimpleNamespace(**r)
        p.duration_s = p.end_s - p.start_s
        plans.append(p)
    state = json.loads((FIXTURES / "gen004_stage_state_slim.json").read_text(encoding="utf-8"))
    return plans, state


def _anchor_block(request: str) -> str:
    """只取 Picture 1 锚定说明块。不能直接搜 "Picture 1"——模板顶部 I2VA 锚定行
    就含 "<Picture 1>"，会误命中（已踩过）；锚定说明块的固定开头是 "Picture 1 是"。"""
    head = request.split("【时间窗外剧情禁区】")[0]
    anchor_start = head.rfind("Picture 1 是")
    return head[anchor_start:anchor_start + 600] if anchor_start >= 0 else ""


def test_segment1_request_carries_frame_anchor():
    plans, state = _load_gen004()
    req = build_segment_v2_request(plans[0], plans, state, None)
    anchor = _anchor_block(req)
    assert FRAME_CONTENT_MARKER in anchor
    assert any(m in anchor for m in DISCIPLINE_MARKERS)


@pytest.mark.xfail(strict=True, reason="issue #12: 中间段拿不到桥接帧读图结果（修复后摘标）")
@pytest.mark.parametrize("seg_index", [1, 2])
def test_middle_segments_request_carries_frame_anchor(seg_index):
    plans, state = _load_gen004()
    req = build_segment_v2_request(plans[seg_index], plans, state, None)
    anchor = _anchor_block(req)
    assert FRAME_CONTENT_MARKER in anchor
    assert any(m in anchor for m in DISCIPLINE_MARKERS)


def test_fixture_matches_original_failure_shape():
    """物证保真：fixture 驱动出的段 2 请求确实缺锚定块（否则物证变形，回路失真）。"""
    plans, state = _load_gen004()
    req = build_segment_v2_request(plans[1], plans, state, None)
    anchor = _anchor_block(req)
    assert FRAME_CONTENT_MARKER not in anchor
    assert not any(m in anchor for m in DISCIPLINE_MARKERS)
