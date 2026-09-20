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


# --- Task 5: 废片 / 中断判定 + override 合并 ---------------------------------

import json

from minimax_h3_prompt.tools.ledger import (
    apply_override,
    classify_leftovers,
    detect_interruptions,
    load_override,
    read_plan_segment_count,
)


def test_classify_leftovers_marks_similar_unreferenced_as_discarded(tmp_path, tmp_video):
    """首帧几乎相同的两个视频，只有一个进了链 → 另一个是废弃初版，高置信度。"""
    from minimax_h3_prompt.tools.frame_match import read_window

    first = tmp_video("00001.mp4")
    kept = tmp_video("00004.mp4")
    twin = tmp_video("00003.mp4")   # 与 kept 用同一张输入图，首帧几乎相同
    head_gray = {v: read_window(v, window="head", k=1)[0] for v in (first, kept, twin)}

    discarded, review = classify_leftovers([first, kept, twin], {first, kept}, head_gray)
    assert [d.video for d in discarded] == ["00003.mp4"]
    assert discarded[0].confidence == "high"
    assert discarded[0].replaced_by == "00004.mp4"
    assert review == []


def test_classify_leftovers_flags_unknown_as_review(tmp_path, tmp_video):
    """找不到相似视频 → 中等置信度 + 进 review_needed，不猜。"""
    from minimax_h3_prompt.tools.frame_match import read_window

    only = tmp_video("solo.mp4")
    head_gray = {only: read_window(only, window="head", k=1)[0]}
    discarded, review = classify_leftovers([only], set(), head_gray)
    assert [d.confidence for d in discarded] == ["medium"]
    assert any("solo.mp4" in r for r in review)


def test_classify_leftovers_returns_empty_when_nothing_left_over(tmp_path, tmp_video):
    from minimax_h3_prompt.tools.frame_match import read_window

    only = tmp_video("only.mp4")
    head_gray = {only: read_window(only, window="head", k=1)[0]}
    assert classify_leftovers([only], {only}, head_gray) == ([], [])


def test_detect_interruptions_reports_missing_segment():
    shots = [Shot(shot=i, video=f"{i}.mp4", video_dir="d", frames=10,
                  duration_s=1.0, generated_at="", input_frame=InputFrame(
                      file="", kind="unknown", source=None, match_mad=None,
                      confidence="low"), status="final") for i in range(1, 6)]
    interruptions = detect_interruptions(shots, plan_segments=6)
    assert len(interruptions) == 1
    assert interruptions[0].at_shot == 6
    assert interruptions[0].type == "unfinished"


def test_detect_interruptions_none_when_counts_match():
    shots = [Shot(shot=1, video="1.mp4", video_dir="d", frames=10, duration_s=1.0,
                  generated_at="", input_frame=InputFrame(
                      file="", kind="unknown", source=None, match_mad=None,
                      confidence="low"), status="final")]
    assert detect_interruptions(shots, plan_segments=1) == []


def test_apply_override_replaces_shot_video():
    ledger = _sample_ledger()
    patched = apply_override(ledger, {"shots": {"1": {"video": "新.mp4"}}})
    assert patched.shots[0].video == "新.mp4"
    assert ledger.shots[0].video == "a.mp4", "原对象不应被就地修改"


def test_load_override_missing_file_returns_empty(tmp_path):
    assert load_override(tmp_path) == {}


def test_load_override_reads_json(tmp_path):
    (tmp_path / "ledger.override.json").write_text(
        json.dumps({"shots": {"1": {"video": "x.mp4"}}}), encoding="utf-8")
    assert load_override(tmp_path)["shots"]["1"]["video"] == "x.mp4"


def test_read_plan_segment_count(tmp_path):
    seg = tmp_path / "segments"
    seg.mkdir()
    (seg / "plan.json").write_text(json.dumps([{"index": 0}, {"index": 1}]),
                                   encoding="utf-8")
    assert read_plan_segment_count(tmp_path) == 2


def test_read_plan_segment_count_missing_returns_none(tmp_path):
    assert read_plan_segment_count(tmp_path) is None


# --- Task 6: rebuild 整合与落盘 ---------------------------------------------

from minimax_h3_prompt.tools.ledger import build_ledger, rebuild


def _make_session(tmp_path, tmp_video, *, n_shots: int,
                  plan_segments: int | None = None):
    """造一个最小会话：n_shots 个镜头，**尾帧接力**，可选一个废弃初版。"""
    session = tmp_path / "sessions" / "主题-abc" / "GEN001"
    (session / "bridge_frames").mkdir(parents=True)
    (session / "frames").mkdir(parents=True)
    out = tmp_path / "comfy" / "2026-09-18"
    out.mkdir(parents=True)

    videos = []
    for i in range(1, n_shots + 1):
        # 见裁决 2：offset 让视频 i 的首帧 = 视频 i-1 的尾帧
        v = tmp_video(f"MiniMax-H3视频_{i:05d}.mp4", frames=24, step=1,
                      offset=(i - 1) * 23)
        v.rename(out / v.name)
        videos.append(out / f"MiniMax-H3视频_{i:05d}.mp4")

    # frames/first.png：镜头1 的生图输入（非视频尺寸，用 4 倍放大模拟）
    first = read_window(videos[0], window="head", k=1)[0]
    big = cv2.resize(first, (512, 288), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(big, cv2.COLOR_GRAY2BGR))
    assert ok
    (session / "frames" / "first.png").write_bytes(buf.tobytes())

    # 桥接帧：镜头 N（N>=2）的首帧 = 镜头 N-1 的尾帧
    for i in range(2, n_shots + 1):
        tail = read_window(videos[i - 2], window="tail", k=1)[0]
        img = cv2.cvtColor(cv2.resize(tail, (128, 72), interpolation=cv2.INTER_NEAREST),
                           cv2.COLOR_GRAY2BGR)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        (session / "bridge_frames" / f"shot-{i:02d}-start.png").write_bytes(buf.tobytes())

    if plan_segments is not None:
        (session / "segments").mkdir()
        (session / "segments" / "plan.json").write_text(
            json.dumps([{"index": i} for i in range(plan_segments)]), encoding="utf-8")

    return session, out


def test_build_ledger_recovers_shot_order(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    ledger = build_ledger(session, [out])
    assert [s.shot for s in ledger.shots] == [1, 2, 3]


def test_build_ledger_first_shot_input_is_generated(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    ledger = build_ledger(session, [out])
    assert ledger.shots[0].input_frame.kind == "generated"
    assert ledger.shots[1].input_frame.kind == "extracted"


def test_build_ledger_extracted_frames_are_high_confidence(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    ledger = build_ledger(session, [out])
    assert all(s.input_frame.confidence == "high" for s in ledger.shots[1:])


def test_build_ledger_reports_interruption_when_plan_longer(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=2, plan_segments=4)
    ledger = build_ledger(session, [out])
    assert [i.at_shot for i in ledger.interruptions] == [3]
    assert ledger.interruptions[0].type == "unfinished"


def test_rebuild_writes_and_reloads(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    written = rebuild(session, [out])
    assert (session / "ledger.json").is_file()
    reloaded = Ledger.from_dict(
        json.loads((session / "ledger.json").read_text(encoding="utf-8")))
    assert reloaded.to_dict() == written.to_dict()


def test_rebuild_does_not_touch_override_file(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    override = session / "ledger.override.json"
    override.write_text(json.dumps({"shots": {"1": {"video": "手改.mp4"}}}),
                        encoding="utf-8")
    rebuild(session, [out])
    assert json.loads(override.read_text(encoding="utf-8"))["shots"]["1"]["video"] == "手改.mp4"
