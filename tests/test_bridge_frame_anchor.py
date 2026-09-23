"""桥接帧锚定回路（issue #12）：每个段的写段请求都必须携带 Picture 1 实际画面与双向一致性纪律句。

用 GEN004 真实产物裁剪版 fixture 驱动 `build_segment_v2_request`（纯函数，不需要 LLM/GPU）。

修复前缺陷：`_frame_anchor_note` 的纪律句只在第 1 段发出，中间段拿不到桥接帧读图结果
→ 写段 LLM 在信息上不可能写出与桥接帧一致的开场状态（GEN004 段 2 实证：正文写
「门被推开、猫已冲进店内」，而桥接帧是门关着、没有猫）。

运行时接线（wizard）：剥出桥接帧后立刻读图并写进 ``state["bridge_frame_descriptions"]``
（元素形如 ``{"segment": <0-based 段号>, "description": ...}``），写段请求按段号取用；
本文件的中间段测试模拟该 state（fixture 是历史产物，没有运行时读图记录）。
"""
import json
import types
from pathlib import Path

from minimax_h3_prompt.segment_prompts import build_segment_v2_request

FIXTURES = Path(__file__).resolve().parent / "fixtures"

DISCIPLINE_MARKERS = ["逐项一致", "开场状态必须与这张图"]
FRAME_CONTENT_MARKER = "实际画面"
# 反向那一半（issue 验收第三条）：GEN004 段 2 坏的正是「图中没有的写成已存在」
REVERSE_DISCIPLINE_MARKER = "图中没有的事物"

# 模拟向导运行时对段 2（0-based 1）桥接帧的读图结果；
# 内容对齐真实桥接帧（GEN004/bridge_frames/shot-02-start.png）：门关、无猫、店员右手在台面。
BRIDGE_SEG2_DESC = (
    "便利店收银台中景：玻璃店门完整关闭，门外是夜晚；一名穿深蓝色工装的男性店员站在"
    "收银台内侧，右手平放在台面上，画面中没有猫，台面上有收银机和一袋零食。"
)
BRIDGE_SEG3_DESC = (
    "近景：橘猫四爪落在收银台面上，尾巴压低；店员右手悬停在猫头侧上方，玻璃店门仍完整关闭。"
)


def _load_gen004(bridge: dict[int, str] | None = None):
    plans = []
    for r in json.loads((FIXTURES / "gen004_plan.json").read_text(encoding="utf-8")):
        p = types.SimpleNamespace(**r)
        p.duration_s = p.end_s - p.start_s
        plans.append(p)
    state = json.loads((FIXTURES / "gen004_stage_state_slim.json").read_text(encoding="utf-8"))
    if bridge:
        state["bridge_frame_descriptions"] = [
            {"segment": segment, "description": description}
            for segment, description in sorted(bridge.items())
        ]
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


def test_middle_segments_request_carries_frame_anchor():
    """中间段写段请求必须携带桥接帧读图结果（运行时由向导剥帧后读图写入 state）。"""
    plans, state = _load_gen004(bridge={1: BRIDGE_SEG2_DESC, 2: BRIDGE_SEG3_DESC})
    for seg_index in (1, 2):
        req = build_segment_v2_request(plans[seg_index], plans, state, None)
        anchor = _anchor_block(req)
        assert FRAME_CONTENT_MARKER in anchor, f"段 {seg_index + 1} 的请求没带桥接帧实际画面"
        assert any(m in anchor for m in DISCIPLINE_MARKERS), f"段 {seg_index + 1} 的请求没带一致性纪律句"


def test_bridge_description_is_scoped_to_its_segment():
    """段 3 的请求不得误用段 2 的桥接帧描述（每段桥接帧不同，按段号取用）。"""
    plans, state = _load_gen004(bridge={1: BRIDGE_SEG2_DESC})
    req = build_segment_v2_request(plans[2], plans, state, None)
    assert BRIDGE_SEG2_DESC not in req


def test_middle_segments_without_reading_still_carry_discipline():
    """剥帧被跳过/读图失败时诚实降级：不带「实际画面」，但纪律句仍必须对所有段发出。"""
    plans, state = _load_gen004()
    for seg_index in (1, 2):
        anchor = _anchor_block(build_segment_v2_request(plans[seg_index], plans, state, None))
        assert FRAME_CONTENT_MARKER not in anchor, "没有读图结果不得假装有（fixture 物证形态）"
        assert any(m in anchor for m in DISCIPLINE_MARKERS), f"段 {seg_index + 1} 的纪律句缺失"


def test_anchor_discipline_covers_reverse_half_for_all_segments():
    """双向纪律：不只禁止「图中已有写成不存在」，也要禁止「图中没有写成已存在」——对所有段。"""
    plans, state = _load_gen004(bridge={1: BRIDGE_SEG2_DESC, 2: BRIDGE_SEG3_DESC})
    for seg_index in (0, 1, 2):
        anchor = _anchor_block(build_segment_v2_request(plans[seg_index], plans, state, None))
        assert REVERSE_DISCIPLINE_MARKER in anchor, f"段 {seg_index + 1} 缺反向纪律（图中没有的不得写成已存在）"
