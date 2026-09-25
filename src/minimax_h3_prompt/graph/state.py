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
    character_image_prompts: dict  # Z-Image/Flux.2 人物提示词
    prop_image_prompts: dict  # Z-Image/Flux.2 道具提示词
    scene_image_prompts: dict  # Z-Image/Flux.2 场景提示词
    identity_lock: str  # 一致性对齐会（圆桌 3）锁定

    # 视觉（分镜 → 并行决策 → 并行制作）
    shot_table: str  # 分镜
    shot_duration_warning: str  # 分镜镜头时长均分警告（重试仍未修正时记录）
    shot_review_lock: str  # 镜头评审会（圆桌 2）锁定
    visual_design: str  # 摄影指导
    fl2va_prompt_bundle: dict  # FL2VA 融合首帧/尾帧提示词
    fl2va_prompt_issues: list[str]  # FL2VA 确定性校验问题
    fl2va_frame_descriptions: list[dict]  # 真实首尾帧图片的 qwen3.7-plus 读图描述（阶段 2 注入）
    frame_images: list[dict]  # 已入库关键帧图片追溯记录（copy_frame_image 结果）
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

    # 人的累积修订与它的一轮入参。**必须在这里声明**：LangGraph 只搬运 schema 里声明过的
    # 键，未声明的键在图外传得进去、到节点手上却没了——静默、不报错。2026-09-25（issue #28）
    # 实测：把 setting_revision 放进 run_stage1 的 initial_state 就以为接线完成，三个设计师
    # 节点一个都没拿到它（打桩 run_stage1 的用例照样全绿，因为它们绕过了图）。
    # 画面级那条循环直接调节点，本来不受影响；声明它们是为了**设定级截断重跑**——那条路是
    # 走图跑的：重跑中的首帧节点要看得见累积修订，否则一次设定级重跑会把用户已经谈定的
    # 画面级要求悄悄丢干净（新解出的提示词里一条都不剩）。
    user_revisions: list[dict]  # 累积的用户修订（含已落地标记），真源见 user_revisions.py
    setting_revision: dict  # 本轮那条设定级修订 {layer, text}，只在一次重跑内有效
