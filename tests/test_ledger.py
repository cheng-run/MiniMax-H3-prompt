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


# --- Task 4: 建边与链路重建 ------------------------------------------------

from pathlib import Path

import cv2

from minimax_h3_prompt.tools.frame_match import read_window
from minimax_h3_prompt.tools.ledger import (
    BridgeRef,
    build_edges,
    collect_videos,
    pick_start,
    resolve_chain,
)


def _probe_from_tail(video, size=(128, 72)):
    """取某视频尾帧，放大成 BGR 当作"桥接帧"。"""
    tail = read_window(video, window="tail", k=1)[0]
    return cv2.cvtColor(cv2.resize(tail, size, interpolation=cv2.INTER_NEAREST),
                        cv2.COLOR_GRAY2BGR)


def test_collect_videos_sorted_and_filters_non_mp4(tmp_path, tmp_video):
    tmp_video("MiniMax-H3视频_00002.mp4")
    tmp_video("MiniMax-H3视频_00001.mp4")
    (tmp_path / "notes.txt").write_text("x")
    videos = collect_videos([tmp_path])
    assert [v.name for v in videos] == ["MiniMax-H3视频_00001.mp4", "MiniMax-H3视频_00002.mp4"]


def test_collect_videos_empty_dir(tmp_path):
    assert collect_videos([tmp_path / "不存在"]) == []


def test_build_edges_identifies_src_and_keeps_all_dst_candidates(tmp_path, tmp_video):
    """桥接帧由 A 的尾帧复制而来 → src=A；dst 候选保留全部。"""
    a = tmp_video("a.mp4", frames=24)
    b = tmp_video("b.mp4", frames=24)
    bridge_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", _probe_from_tail(a))
    assert ok
    bridge_path.write_bytes(buf.tobytes())

    ref = BridgeRef(shot_no=2, path=bridge_path, src_video=None, src_mad=None,
                    dst_candidates=[])
    edges = build_edges([ref], [a, b])
    assert edges[0].src_video == a
    assert edges[0].src_mad < 2.0
    assert all(d < 5.0 for _, d in edges[0].dst_candidates)


def test_build_edges_leaves_unreadable_bridge_empty(tmp_path, tmp_video):
    a = tmp_video("a.mp4")
    ref = BridgeRef(shot_no=2, path=tmp_path / "无.png", src_video=None,
                    src_mad=None, dst_candidates=[])
    edges = build_edges([ref], [a])
    assert edges[0].src_video is None
    assert edges[0].dst_candidates == []


def test_pick_start_returns_video_that_is_never_a_dst(tmp_path, tmp_video):
    first = tmp_video("00001.mp4")
    second = tmp_video("00002.mp4")
    third = tmp_video("00003.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", first, 0.5, [(second, 0.5)]),
             BridgeRef(3, tmp_path / "b3.png", second, 0.5, [(third, 0.5)])]
    assert pick_start([first, second, third], edges) == first


def test_pick_start_returns_none_when_everything_is_a_dst(tmp_path, tmp_video):
    a = tmp_video("a.mp4")
    b = tmp_video("b.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", a, 0.5, [(b, 0.5)]),
             BridgeRef(3, tmp_path / "b3.png", b, 0.5, [(a, 0.5)])]
    assert pick_start([a, b], edges) is None


def test_resolve_chain_prefers_continuation_over_time(tmp_path, tmp_video):
    """两个候选都在阈值内时，选尾帧还被引用的那个（链延续性优先）。

    这正是 00003 / 00004 的真实情形：两者首帧都对得上同一张桥接帧。
    """
    first = tmp_video("00001.mp4", frames=24)
    dead_end = tmp_video("00003.mp4", frames=24)
    continuer = tmp_video("00004.mp4", frames=24)
    last = tmp_video("00005.mp4", frames=24)

    edges = [
        BridgeRef(2, tmp_path / "b2.png", first, 0.5,
                  [(dead_end, 0.88), (continuer, 0.88)]),   # 同分候选，一次给全
        BridgeRef(3, tmp_path / "b3.png", continuer, 0.5, [(last, 0.5)]),
    ]
    chain = resolve_chain(edges, [first, dead_end, continuer, last], first)
    assert [v.name for _, v in chain] == ["00001.mp4", "00004.mp4", "00005.mp4"]


def test_resolve_chain_stops_at_dead_end(tmp_path, tmp_video):
    a = tmp_video("a.mp4")
    b = tmp_video("b.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", a, 0.5, [(b, 0.5)])]
    chain = resolve_chain(edges, [a, b], a)
    assert [v.name for _, v in chain] == ["a.mp4", "b.mp4"]


def test_resolve_chain_ignores_bridges_above_threshold(tmp_path, tmp_video):
    """src 的 mad 超过阈值 → 这条边不可信，链在这里就断。"""
    a = tmp_video("a.mp4")
    b = tmp_video("b.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", a, 40.0, [(b, 0.5)])]
    assert [v.name for _, v in resolve_chain(edges, [a, b], a)] == ["a.mp4"]
