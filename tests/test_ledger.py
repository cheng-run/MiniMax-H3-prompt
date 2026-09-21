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
    # read_window 返回 float32；PNG 编码器只收 8-bit，显式转换避免 OpenCV fallback 警告
    return cv2.cvtColor(cv2.resize(tail.astype(np.uint8), size,
                                   interpolation=cv2.INTER_NEAREST),
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

import numpy as np

from minimax_h3_prompt.tools.ledger import build_ledger, rebuild


def _make_session(tmp_path, tmp_video, *, n_shots: int,
                  plan_segments: int | None = None, bridge_shift: float = 0.0):
    """造一个最小会话：n_shots 个镜头，**尾帧接力**，可选一个废弃初版。

    ``bridge_shift`` 把桥接帧整体抬高若干灰阶，从而把它与来源尾帧的 mad
    抬到指定水平（默认 0.0 = 完全同源，与历史行为一致）。用于测阈值边界。
    """
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
    # read_window 返回 float32；PNG 编码器只收 8-bit，显式转换避免 OpenCV fallback 警告
    first8 = cv2.cvtColor(first.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    big = cv2.resize(first8, (512, 288), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", big)
    assert ok
    (session / "frames" / "first.png").write_bytes(buf.tobytes())

    # 桥接帧：镜头 N（N>=2）的首帧 = 镜头 N-1 的尾帧
    for i in range(2, n_shots + 1):
        tail = read_window(videos[i - 2], window="tail", k=1)[0]
        if bridge_shift:
            tail = np.clip(tail + bridge_shift, 0, 255)
        img = cv2.cvtColor(cv2.resize(tail.astype(np.uint8), (128, 72),
                                      interpolation=cv2.INTER_NEAREST),
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


def test_build_ledger_forwards_threshold_to_edge_building(tmp_path, tmp_video):
    """threshold 必须贯通到 ``build_edges``，否则 CLI 的 --threshold 静默无效。

    桥接帧抬高 7 个灰阶后，它与来源尾帧的 mad 落在默认阈值 5.0 之上。
    默认阈值下这段接不上（只剩起点镜头）；放大到 20 就该接上。
    若 threshold 没传到建边（建边恒用 5.0），两次调用都会只剩 1 个镜头。
    """
    session, out = _make_session(tmp_path, tmp_video, n_shots=2, bridge_shift=7.0)

    assert len(build_ledger(session, [out]).shots) == 1
    assert len(build_ledger(session, [out], threshold=20.0).shots) == 2


def test_build_ledger_flags_weak_bridge_source_for_review(tmp_path, tmp_video):
    """桥接帧与来源尾帧的 mad 超过同源阈值时，来源存疑：置信度降为 low 并进 review。

    threshold 放大到 20 后这段链能接上，但 mad≈7 说明它与来源并非同源帧，
    不能当作可信来源静默放过（设计决定 ④：推断不出就进 review_needed，不猜）。
    已找到的 source / match_mad 仍照实保留，不隐藏"在用户阈值下确有匹配"。
    """
    session, out = _make_session(tmp_path, tmp_video, n_shots=2, bridge_shift=7.0)
    ledger = build_ledger(session, [out], threshold=20.0)

    frame = ledger.shots[1].input_frame
    assert frame.confidence == "low"
    assert frame.source is not None
    assert frame.match_mad is not None and frame.match_mad >= 5.0
    assert any("镜头 2" in item for item in ledger.review_needed)


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


# --- resolve_chain 消歧优先级：延续性 > 生成时间 ------------------------------
# 既有那个 `prefers_continuation_over_time` 没有区分力：它构造的 continuer
# 恰好 mtime 也更晚，删掉「延续性优先」那一行它照样过。下面两条把两个优先级
# 各自单独钉死。

import os
import time


def test_resolve_chain_beats_mtime_when_continuation_disagrees(tmp_path, tmp_video):
    """延续性优先于生成时间：不延续的候选 mtime 明显更晚，也不能选它。

    ``dead_end`` 的 mtime 被拨到 1 小时之后 —— 只按 mtime 排序一定会选错
    它；能选对 ``continuer`` 说明「候选的尾帧还被别的桥接帧引用」这一条
    优先级更高。这正是 00003 / 00004 的真实情形。
    """
    first = tmp_video("00001.mp4", frames=24)
    dead_end = tmp_video("00003.mp4", frames=24)
    continuer = tmp_video("00004.mp4", frames=24)
    last = tmp_video("00005.mp4", frames=24)
    later = time.time() + 3600
    os.utime(dead_end, (later, later))

    edges = [
        BridgeRef(2, tmp_path / "b2.png", first, 0.5,
                  [(dead_end, 0.88), (continuer, 0.88)]),   # 同分候选，一次给全
        BridgeRef(3, tmp_path / "b3.png", continuer, 0.5, [(last, 0.5)]),
    ]
    chain = resolve_chain(edges, [first, dead_end, continuer, last], first)
    assert [v.name for _, v in chain] == ["00001.mp4", "00004.mp4", "00005.mp4"]


def test_resolve_chain_falls_back_to_mtime_when_nothing_continues(tmp_path, tmp_video):
    """两个候选都不延续（都不是任何边的 src）时，退到第二优先级：取 mtime 更晚的。

    候选列表故意把 ``older`` 放前面，确保选对靠的是排序而不是遍历顺序。
    """
    first = tmp_video("00001.mp4", frames=24)
    older = tmp_video("00002.mp4", frames=24)
    newer = tmp_video("00003.mp4", frames=24)
    base = time.time()
    os.utime(older, (base - 600, base - 600))
    os.utime(newer, (base, base))

    edges = [BridgeRef(2, tmp_path / "b2.png", first, 0.5,
                       [(older, 0.88), (newer, 0.88)])]
    chain = resolve_chain(edges, [first, older, newer], first)
    assert [v.name for _, v in chain] == ["00001.mp4", "00003.mp4"]


# --- 桥接帧未匹配任何视频尾帧：报路径 + 按尺寸判 kind + 进 review ---------------

import numpy as np

from minimax_h3_prompt.tools.ledger import _shot_input_frame


def _write_png(path: Path, width: int, height: int,
               colour: tuple[int, int, int] = (255, 255, 255)) -> Path:
    """写一张纯色 png（BGR），返回路径。"""
    ok, buf = cv2.imencode(".png", np.full((height, width, 3), colour, dtype=np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    return path


def test_shot_input_frame_reports_bridge_without_source(tmp_path, tmp_video):
    """匹配不上任何视频尾帧时：路径要报出来，kind 退回尺寸判定（1280×736 → extracted），
    但不猜来源（source 为 None）、置信度 low。"""
    bridge_path = _write_png(tmp_path / "shot-02-start.png", 1280, 736)
    shot2 = tmp_video("00002.mp4", frames=24)
    shot1 = tmp_video("00001.mp4", frames=24)

    frame = _shot_input_frame(tmp_path, shot2, BridgeRef(2, bridge_path, None, None, []),
                              shot1)
    assert frame.file == "bridge_frames/shot-02-start.png"
    assert frame.kind == "extracted"
    assert frame.source is None
    assert frame.match_mad is None
    assert frame.confidence == "low"


def test_shot_input_frame_size_fallback_for_unmatched_bridge(tmp_path, tmp_video):
    """同一分支、尺寸换成生图尺寸 → kind 换成 generated，证明尺寸后备真的在区分。"""
    bridge_path = _write_png(tmp_path / "shot-02-start.png", 3840, 2160)
    shot2 = tmp_video("00002.mp4", frames=24)
    shot1 = tmp_video("00001.mp4", frames=24)

    frame = _shot_input_frame(tmp_path, shot2, BridgeRef(2, bridge_path, None, None, []),
                              shot1)
    assert frame.kind == "generated"
    assert frame.source is None
    assert frame.confidence == "low"


def test_build_ledger_flags_unmatched_bridge_for_review(tmp_path, tmp_video):
    """能读出来、但没匹配上任何视频尾帧的桥接帧 → 进 review_needed，不猜来源。"""
    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    # 覆盖成纯白 1280×736：与任何视频（深色背景 + 小白块）的尾帧 mad 都远大于阈值
    _write_png(session / "bridge_frames" / "shot-02-start.png", 1280, 736)

    ledger = build_ledger(session, [out])
    assert any("shot-02-start.png" in item for item in ledger.review_needed)
