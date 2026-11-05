"""资产类别与 state 键的**唯一声明**（2026-11-02 体检 P0-5）。

为什么单独成模块：``workflow_config``（配置层）此前 import ``asset_ledger``（领域层），
``generation`` 与 ``session_store`` 又 import ``graph.state``，几条边把 7 个模块绕成
一个环。类别标签与「类别 → state 键」都是纯数据，住在最底层即可让配置/会话层不再
依赖领域实现与图实现；``asset_ledger`` 仍按原名字再导出这些常量，调用点零改动。

这里只放常量，不 import 任何项目模块——它是断环的锚点，自己不能再拉起依赖。
"""
from __future__ import annotations

# 参考素材三型：对齐官方标签（Picture / Video / Audio），值是机器口径，渲染再转中文。
KIND_PICTURE = "picture"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KINDS = (KIND_PICTURE, KIND_VIDEO, KIND_AUDIO)
KIND_LABELS = {KIND_PICTURE: "图", KIND_VIDEO: "视频", KIND_AUDIO: "音频"}
OFFICIAL_LABEL = {KIND_PICTURE: "Picture", KIND_VIDEO: "Video", KIND_AUDIO: "Audio"}

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

# 设计端资产清单三键（issue #173 / spec #166 D）：三位设计师在回复末尾附机器可读的
# ```asset-manifest``` 块，出词节点**只按这份清单**产出并校验名字集合。粒度是「张」——
# 一个人两张板写两个名字，没有本类资产写空清单。
#
# 键名从 ``ASSET_KIND_FIELDS`` 现推而不是另抄一张表：加第四类资产时清单键、状态契约、
# 持久化白名单与路线过滤要一起动，抄一份就会出现「节点写了、读回丢了」的静默漏。
# **必须同时**登记进 ``graph/state.py`` 的状态契约与 ``session_store`` 的持久化白名单，
# 并在非产资产路线随资产键一起丢弃——LangGraph 只搬运声明过的键。
ASSET_MANIFEST_FIELDS: dict[str, str] = {
    kind: f"{kind}_asset_manifest" for kind in ASSET_KIND_FIELDS
}
ASSET_MANIFEST_KEYS = tuple(ASSET_MANIFEST_FIELDS.values())
