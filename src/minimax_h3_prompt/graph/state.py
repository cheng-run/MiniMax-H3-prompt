"""LangGraph 管线状态定义。

total=False：所有字段可缺省，节点按需返回部分字段。
"""
from __future__ import annotations

from typing import TypedDict

from ..brief_parser import Brief


# 资产类别 → (产物 state 键, 上游设计 state 键)。**唯一声明**（issue #88 收拢）：
# 产物出口（``generation.image_prompt_variants`` 的降级读法）、资产图节点的注册、
# 按张路由（读上一次的条目与设计正文）与单条重出都从它取。三处各写一份的代价是
# 加第四类资产时总有一处静默漏掉，而且没有用例会红（#56 复审：守卫形态要从常量派生）。
#
# 每条条目是**四键契约**（name / positive_prompt / negative_prompt / locked_features）
# 再加系统自己的一个字段 ``image_model``（``{model, source, keyword}``，issue #88）：
# 它记「这条词按哪一档的纪律写成、为什么是那一档」，改写与出词横幅都按它取模型。
# 它跟着条目走而不是另开一张 state 表——三个资产图节点在同一个组合节点里并发跑，
# 共享一张表会被 ``make_parallel`` 的 ``merged.update`` 覆盖掉前两类的记录。
ASSET_KIND_FIELDS: dict[str, tuple[str, str]] = {
    "character": ("character_image_prompts", "character_design"),
    "prop": ("prop_image_prompts", "prop_design"),
    "scene": ("scene_image_prompts", "background_design"),
}

# 资产图提示词三键（父 spec #77 / issue #80）：多参路线独占的产物键，从上面那张表
# 展开。会话持久化白名单、读回时的路线过滤、产物出口都从这个常量派生。
ASSET_PROMPT_KEYS = tuple(
    prompt_field for prompt_field, _design_field in ASSET_KIND_FIELDS.values())


class PipelineState(TypedDict, total=False):
    # 输入
    brief: Brief
    mode: str  # base | ref | director
    variant: str  # T2VA/I2VA/FL2VA/L2VA
    # 导演台路线选定的生成模式（issue #101）。**必须在这里声明**：LangGraph 只搬运
    # schema 里声明过的键，未声明的键传得进图、到节点手上却没了（静默，#28 实测）。
    # 七值与校验见 ``brief_parser.DIRECTOR_MODES`` / ``validate_director_mode``。
    director_mode: str
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
    # 三键必须逐个声明：LangGraph 只搬运 schema 里声明过的键；键名清单见
    # ``ASSET_PROMPT_KEYS``（上面），别在这里另立一份。
    character_image_prompts: list  # 人物资产生图提示词列表（每资产一条，issue #40 起）
    prop_image_prompts: list  # 道具资产生图提示词列表
    scene_image_prompts: list  # 场景资产生图提示词列表
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

    # 读图校对记录（issue #44）：语义名 → {sha256, status, description, …}。
    # `description` 是合并后的素材画面事实，下游提示词写作经 ref_asset_registry 消费。
    # **必须在这里声明**：提示词工程师/参考一致性节点读它（经 ref_asset_registry），
    # LangGraph 只搬运声明过的键——漏声明就是「裁决结果到不了下游」的静默断链。
    asset_proofreads: dict
