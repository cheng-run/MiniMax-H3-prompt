"""导演台路线（issue #100）：与 ``base`` / ``ref`` 并列的第三条生成路线。

本包只装导演台自己的纯件与适配层；``base`` / ``ref`` 的模块不因新增路线而改动
（设计 §2）。对外出口集中在这里，模块内部不互相 re-export。
"""
from __future__ import annotations

from .frames import (
    FPS,
    SEGMENT_TYPE_LABELS,
    SEGMENT_TYPES,
    DirectorPlan,
    DirectorSegment,
    align_frames,
    declared_frames_for_export,
    duration_sec_for_declared,
    export_frames_for_declared,
    give_images_for,
    is_guided,
    is_on_grid,
    land_exports,
    max_segments_for_mode,
    max_total_seconds_for_mode,
    optional_images_for,
    segment_type_label,
    segment_types_for_mode,
    validate_segment_types,
)

__all__ = [
    "FPS",
    "SEGMENT_TYPES",
    "SEGMENT_TYPE_LABELS",
    "DirectorPlan",
    "DirectorSegment",
    "align_frames",
    "declared_frames_for_export",
    "duration_sec_for_declared",
    "export_frames_for_declared",
    "give_images_for",
    "is_guided",
    "is_on_grid",
    "land_exports",
    "max_segments_for_mode",
    "max_total_seconds_for_mode",
    "optional_images_for",
    "segment_type_label",
    "segment_types_for_mode",
    "validate_segment_types",
]
