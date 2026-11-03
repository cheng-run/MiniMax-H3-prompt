"""#145：给图契约的**首帧槽谓词**——判据必须与 ``give_images_for`` 同源。

为什么单独钉这一条：r2v 没有首/尾帧槽（首帧是 fl2v 的槽位），但它的
``camera_cut`` 以前会误触首帧词生产与「挂首帧」简报。凡是回答「这一段有没有首帧槽」
的地方都必须问同一个函数；各写一份 ``segment.type == "camera_cut"`` 就会在 r2v 下
静默指向一个不存在的槽位（本仓「同一契约不许两处口径」）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.director.frames import (
    IMAGE_FIRST,
    PLAN_MODES,
    SEGMENT_TYPES,
    give_images_for,
)


def test_needs_first_frame_image_is_derived_from_the_image_contract():
    """五型 × 四个规划模式：谓词恒等于「给图清单里有没有 first」。"""
    from minimax_h3_prompt.director.frames import needs_first_frame_image

    for mode in PLAN_MODES:
        for segment_type in SEGMENT_TYPES:
            assert needs_first_frame_image(segment_type, mode) == (
                IMAGE_FIRST in give_images_for(segment_type, mode)), (
                f"{mode}/{segment_type} 的首帧槽判据与给图契约不一致")


def test_fl2v_camera_cut_has_a_first_frame_slot():
    from minimax_h3_prompt.director.frames import needs_first_frame_image

    assert needs_first_frame_image("camera_cut", "fl2v") is True


def test_r2v_camera_cut_has_no_first_frame_slot():
    """r2v 每段只挂参考素材；切镜段也没有首帧槽（2026-11-01 用户裁定）。"""
    from minimax_h3_prompt.director.frames import needs_first_frame_image

    assert needs_first_frame_image("camera_cut", "r2v") is False


def test_continue_never_has_a_first_frame_slot():
    from minimax_h3_prompt.director.frames import needs_first_frame_image

    for mode in PLAN_MODES:
        assert needs_first_frame_image("continue", mode) is False


def test_unknown_segment_type_still_raises():
    from minimax_h3_prompt.director.frames import needs_first_frame_image

    with pytest.raises(ValueError):
        needs_first_frame_image("unknown_type", "fl2v")
