"""执行段（Segment）测试：ID 格式 / 自动拆分 / 断链检测 / 桥接帧登记 / 链验证。"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from minimax_h3_prompt.execution import (
    extract_bridge_frame,
    plan_segments,
    verify_segment_chain,
)
from minimax_h3_prompt.project_models import (
    Segment,
    Shot,
    ShotPlan,
    split_shot_into_segments,
    verify_chain,
)
from minimax_h3_prompt.project_store import ProjectStore


def make_video(path: Path, *, frames: int = 8) -> Path:
    """写入一个帧色分明的测试视频（尾帧为纯白，便于肉眼/像素核对）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 8, (64, 64))
    for index in range(frames):
        color = 255 if index == frames - 1 else index * 20
        writer.write(np.full((64, 64, 3), color, np.uint8))
    writer.release()
    return path


def make_project_with_shot(store: ProjectStore, duration: float = 12.0) -> Shot:
    store.init_project("topic", "project", "分段测试", duration_seconds=duration)
    shot = Shot(
        shot_id="SH001", shot_number=1, duration_seconds=duration,
        start_state="start", action="action", end_state="end",
        generation_id="SH001-G001",
    )
    document = store.load_project("topic", "project")
    plan = ShotPlan(
        document.shot_plan.shot_plan_id, document.shot_plan.project_id,
        document.shot_plan.variant, document.shot_plan.duration_seconds, (shot,),
    )
    store.update_shot_plan("topic", "project", plan, overwrite=True)
    return shot


# 1. 段 ID 格式 -----------------------------------------------------------------

def test_segment_id_format():
    segment = Segment(segment_id="SEG01-SH001a", shot_ref="SH001", duration_seconds=7.0)
    assert segment.shot_number == 1
    assert segment.sequence_letter == "a"
    assert segment.is_first_in_shot

    with pytest.raises(ValueError, match="Segment ID 格式无效"):
        Segment(segment_id="SEG1-SH001a", shot_ref="SH001", duration_seconds=7.0)
    with pytest.raises(ValueError, match="Segment ID 格式无效"):
        Segment(segment_id="SEG01-SH001", shot_ref="SH001", duration_seconds=7.0)


# 2. 自动拆分 -----------------------------------------------------------------

def _bare_shot(duration: float) -> Shot:
    return Shot(shot_id="SH001", shot_number=1, duration_seconds=duration,
                start_state="s", action="a", end_state="e")


def test_shot_auto_split():
    segments = split_shot_into_segments(_bare_shot(14.0))
    assert len(segments) == 2
    assert [s.duration_seconds for s in segments] == pytest.approx([7.0, 7.0])
    assert segments[1].prev_segment_id == segments[0].segment_id
    assert segments[0].next_segment_id == segments[1].segment_id

    # 整数秒且末段吸收余数（9s → 4+5）
    assert [s.duration_seconds for s in split_shot_into_segments(_bare_shot(15.0))] == \
        pytest.approx([8.0, 7.0])
    assert [s.duration_seconds for s in split_shot_into_segments(_bare_shot(21.0))] == \
        pytest.approx([7.0, 7.0, 7.0])

    single = split_shot_into_segments(_bare_shot(9.0))
    assert len(single) == 1
    assert single[0].segment_id == "SEG01-SH001a"
    assert single[0].prev_segment_id == ""


# 3. 断链检测 -----------------------------------------------------------------

def test_chain_broken():
    first, second = split_shot_into_segments(_bare_shot(14.0))
    # 桥接帧资产 ID 不一致→ BRIDGE_FRAME_MISMATCH
    first = Segment.from_dict({**first.to_dict(), "end_frame_asset_id": "E02"})
    second = Segment.from_dict({**second.to_dict(), "start_frame_asset_id": "E01"})
    errors = verify_chain((first, second))
    assert any("BRIDGE_FRAME_MISMATCH" in error for error in errors)
    # prev 指针缺失 → MISSING_PREV_SEGMENT
    orphan = Segment.from_dict({**second.to_dict(), "prev_segment_id": ""})
    errors = verify_chain((first, orphan))
    assert any("MISSING_PREV_SEGMENT" in error for error in errors)


# 4. 桥接帧登记 ----------------------------------------------------------------

def test_bridge_frame_registered(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    make_project_with_shot(store, duration=12.0)
    plan_segments(store, "topic", "project")

    video = make_video(tmp_path / "output" / "SH001-G001.mp4")
    result = extract_bridge_frame(store, "topic", "project", "SEG01-SH001a", video)

    asset = result["asset"]
    assert asset["status"] == "planned"
    assert asset["asset_type"] == "bridge_frame"
    target = Path(asset["target_path"])
    assert target.is_file()
    assert target.parent.name == "bridge_frames"
    assert target.name == "SEG01-SH001a-end.png"
    # 段已接线：本段 end_frame，下一段 start_frame + 派生来源
    document = store.load_project("topic", "project")
    first, second = document.shot_plan.shots[0].segments
    assert first.end_frame_asset_id == asset["asset_id"]
    assert second.start_frame_asset_id == asset["asset_id"]
    assert second.start_state_derived_from == "SEG01-SH001a"
    assert document.registry.get(asset["asset_id"]) is not None
    # 幂等：重复剥取同一视频不新增资产
    again = extract_bridge_frame(store, "topic", "project", "SEG01-SH001a", video)
    assert again["asset"]["asset_id"] == asset["asset_id"]


# 5. 链验证通过 ----------------------------------------------------------------

def test_verify_chain_pass(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    make_project_with_shot(store, duration=12.0)
    plan_segments(store, "topic", "project")
    video = make_video(tmp_path / "output" / "SH001-G001.mp4")
    extract_bridge_frame(store, "topic", "project", "SEG01-SH001a", video)

    result = verify_segment_chain(store, "topic", "project", "SH001")
    assert result["chain_ok"] is True
    assert result["errors"] == []
    assert result["segment_count"] == 2

    # 未剥尾帧时链不完整
    store2 = ProjectStore(tmp_path / "Assets2")
    make_project_with_shot(store2, duration=12.0)
    plan_segments(store2, "topic", "project")
    broken = verify_segment_chain(store2, "topic", "project", "SH001")
    assert broken["chain_ok"] is False
    assert any("BRIDGE_FRAME_PENDING" in error for error in broken["errors"])
