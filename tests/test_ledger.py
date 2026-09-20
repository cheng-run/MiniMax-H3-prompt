"""ledger 生成台账测试。"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.tools.ledger import (
    SCHEMA,
    Discarded,
    InputFrame,
    Interruption,
    Ledger,
    Shot,
)


def _sample_ledger() -> Ledger:
    return Ledger(
        schema=SCHEMA,
        session={"topic_slug": "示例", "generation": "GEN001"},
        scan={"scanned_at": "2026-09-20T14:32:11+08:00", "rule_version": "1",
              "output_dirs": ["D:/out"], "threshold_mad": 5.0},
        shots=[
            Shot(
                shot=1, video="a.mp4", video_dir="2026-09-18",
                frames=158, duration_s=6.58, generated_at="2026-09-18T11:26:05",
                input_frame=InputFrame(file="frames/first.png", kind="generated",
                                       source=None, match_mad=None, confidence="high"),
                status="final", evidence=["起点"],
            )
        ],
        discarded=[Discarded(video="c.mp4", video_dir="2026-09-18",
                             reason="未被引用", replaced_by="a.mp4",
                             confidence="high", evidence=["首帧 mad=0.88"])],
        interruptions=[Interruption(at_shot=6, type="unfinished",
                                    evidence=["plan 期望 6 段"],
                                    confidence="medium", hint="原因未知")],
        review_needed=[],
        warnings=["1 个文件读不了"],
    )


def test_ledger_roundtrip_preserves_everything():
    original = _sample_ledger()
    restored = Ledger.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_to_dict_uses_schema_string():
    assert _sample_ledger().to_dict()["schema"] == "h3-ledger/1"


def test_from_dict_rejects_unknown_schema():
    with pytest.raises(ValueError, match="schema"):
        Ledger.from_dict({"schema": "h3-ledger/99"})


def test_from_dict_rejects_missing_schema():
    with pytest.raises(ValueError, match="schema"):
        Ledger.from_dict({})


def test_empty_ledger_roundtrips():
    empty = Ledger(schema=SCHEMA, session={}, scan={}, shots=[], discarded=[],
                   interruptions=[], review_needed=[], warnings=[])
    assert Ledger.from_dict(empty.to_dict()).to_dict() == empty.to_dict()
