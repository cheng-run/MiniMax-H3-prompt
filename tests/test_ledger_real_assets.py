"""真实素材回归：把 2026-09-20 的实测结论钉成断言。

素材在本地 ComfyUI 输出目录，不在仓库里，所以条件跳过。
本测试的价值：以后改推断算法，跑一下就知道有没有退化。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from minimax_h3_prompt.tools.ledger import build_ledger

COMFY_OUTPUT = Path(os.environ.get(
    "H3_COMFY_OUTPUT", r"D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"))
SESSION = Path(
    "output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001")

pytestmark = pytest.mark.skipif(
    not (COMFY_OUTPUT.is_dir() and SESSION.is_dir()),
    reason="需要本地 ComfyUI 素材与会话目录",
)


def test_recovers_five_shots_in_correct_order():
    """正确顺序是 1→2→4→5→6：00003 是镜头 3 的废弃初版。"""
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert [s.video for s in ledger.shots] == [
        "MiniMax-H3视频_00001-audio.mp4",
        "MiniMax-H3视频_00002-audio.mp4",
        "MiniMax-H3视频_00004-audio.mp4",
        "MiniMax-H3视频_00005-audio.mp4",
        "MiniMax-H3视频_00006-audio.mp4",
    ]


def test_shot_one_input_is_generated_frame():
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert ledger.shots[0].input_frame.kind == "generated"
    assert ledger.shots[0].input_frame.file == "frames/first.png"


def test_bridge_sourced_inputs_are_high_confidence():
    """4 张桥接帧在实测里全部对上（mad 0.00 / 0.37 / 0.49 / 1.97）。"""
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert all(s.input_frame.confidence == "high" for s in ledger.shots[1:])


def test_00003_is_marked_discarded():
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert [d.video for d in ledger.discarded] == ["MiniMax-H3视频_00003-audio.mp4"]
    assert ledger.discarded[0].confidence == "high"
    assert ledger.discarded[0].replaced_by == "MiniMax-H3视频_00004-audio.mp4"


def test_reports_plan_expects_six_but_five_produced():
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert [i.at_shot for i in ledger.interruptions] == [6]
    assert ledger.interruptions[0].type == "unfinished"
