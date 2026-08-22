"""LangGraph 管线状态定义。

total=False：所有字段可缺省，节点按需返回部分字段。
"""
from __future__ import annotations

from typing import TypedDict

from ..brief_parser import Brief


class PipelineState(TypedDict, total=False):
    # 输入
    brief: Brief
    mode: str  # ref | base
    variant: str  # T2VA/I2VA/FL2VA/L2VA
    duration: float
    ref_meta: list[tuple[int, str, str]]

    # 前期（串行）
    production_plan: str  # 制片
    director_brief: str  # 导演
    creative_lock: str  # 创意会（圆桌 1）锁定
    script: str  # 编剧
    character_design: str  # 人物形象设计师
    background_design: str  # 背景设计师
    prop_design: str  # 道具设计师
    art_design: str  # 美术指导统筹结果
    identity_lock: str  # 一致性对齐会（圆桌 3）锁定

    # 视觉（分镜 → 并行决策 → 并行制作）
    shot_table: str  # 分镜
    shot_review_lock: str  # 镜头评审会（圆桌 2）锁定
    visual_design: str  # 摄影指导
    subject_defs: str  # 参考资产与一致性（subject_definitions 素材）

    # 声音（并行）
    sound_design: str  # 对白表 + 环境声
    music: str  # 配乐

    # 组装 / 质检
    final_prompt: str
    qa_report: str
    qa_issues: list[str]
    iterations: int
    final_report: str
