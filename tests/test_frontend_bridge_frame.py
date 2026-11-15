"""桥接帧的前后端对齐与两条「有意如此」的判据（#277 复审 F5–F7）。

为什么需要：`artifactKinds.ts` 的 `bridgeFrameName` 与 `SegmentsTab.vue` 的「差一位」
映射是前端 JS 侧的第二份契约。后端改名、段号位数改动或偏移写错时前端不报错，只是
**静默取不到图**：段卡片没有起点图、产物分区凭空少一件。这三条守卫都从后端真源
（`workbench_store` 的常量与命名函数）派生，锚点是前端源码里的字面结构；后端或前端
任一侧漂移都会变红。
"""
from __future__ import annotations

import re
from pathlib import Path

from minimax_h3_prompt.workbench_store import (
    _BRIDGE_FRAME_DIGITS,
    _BRIDGE_FRAME_PREFIX,
    _BRIDGE_FRAME_SUFFIX,
    _BRIDGE_FRAMES_PATH,
    bridge_frame_name,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src" / "features" / "sessions"
ARTIFACT_KINDS = FRONTEND / "artifactKinds.ts"
SEGMENTS_TAB = FRONTEND / "SegmentsTab.vue"


def _function_body(source: str, name: str) -> str:
    """取出函数体（含 `export` 与否都认）；找不到就让守卫直接红。"""
    match = re.search(
        rf"(?:export\s+)?function\s+{re.escape(name)}\s*\([^)]*\)[^{{]*\{{(.*?)\n\}}",
        source,
        re.S,
    )
    assert match, f"{name} 在前端源码里找不到（守卫锚点失效，需更新测试）"
    return match.group(1)


def test_frontend_bridge_frame_name_matches_the_backend():
    """`bridgeFrameName` 与 `bridge_frame_name` 同目录、同前后缀、同段号位宽。"""
    body = _function_body(ARTIFACT_KINDS.read_text(encoding="utf-8"), "bridgeFrameName")

    # 从后端常量派生期望片段：后端改名/改位宽时这里跟着变，前端不同步就红。
    expected = (
        f"{_BRIDGE_FRAMES_PATH}/{_BRIDGE_FRAME_PREFIX}"
        f"${{String(segmentNumber).padStart({_BRIDGE_FRAME_DIGITS}, '0')}}"
        f"{_BRIDGE_FRAME_SUFFIX}"
    )
    assert expected in body, (
        "artifactKinds.ts 的 bridgeFrameName 与后端 bridge_frame_name 不同形；"
        f"期望出现 {expected!r}")

    # 真实样本再核一遍目录与位宽（位数由常量决定，不抄字面）。
    sample = bridge_frame_name(7)
    assert sample == (
        f"{_BRIDGE_FRAMES_PATH}/{_BRIDGE_FRAME_PREFIX}"
        f"{str(7).zfill(_BRIDGE_FRAME_DIGITS)}{_BRIDGE_FRAME_SUFFIX}"
    )


def test_bridge_frame_is_registered_as_a_segment_local_kind():
    """桥接帧必须留在 `SEGMENT_LOCAL_KINDS` 里（可红判据）。

    `artifactGroup` 对未分组 kind 默认返回 null，所以把 `'bridge_frame'` 从集合删掉
    时现有组件用例仍绿——这条断言让「有意不进产物分区」变成会咬人的判据。
    """
    source = ARTIFACT_KINDS.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+SEGMENT_LOCAL_KINDS\s*=\s*new\s+Set\(\s*\[(.*?)\]\s*\)", source, re.S)
    assert match, "artifactKinds.ts 里找不到 SEGMENT_LOCAL_KINDS（守卫锚点失效）"

    members = re.findall(r"'([^']+)'", match.group(1))
    assert "bridge_frame" in members, (
        f"SEGMENT_LOCAL_KINDS 没把 bridge_frame 收进来：{members}")


def test_segments_tab_maps_the_start_frame_to_the_previous_segment():
    """第 N 段（N ≥ 2）取 N−1 那张桥接帧——「差一位」只在 SegmentsTab 折算一次。"""
    body = _function_body(SEGMENTS_TAB.read_text(encoding="utf-8"), "startFrameFor")

    assert "if (index < 2)" in body, (
        "第 1 段的起点是用户交的开场帧，没有桥接帧；缺少 index < 2 的分支会去取"
        "一张不存在的图")
    assert "bridgeFrameName(index - 1)" in body, (
        "第 N 段的起点图是第 N−1 段的剥帧；写成 index 会取到本段尾帧（差一位）")
