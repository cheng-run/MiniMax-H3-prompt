"""LangGraph 节点工厂：把角色 agent 包装成节点，构造各节点所需的用户消息。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from langgraph.graph.state import CompiledStateGraph

from .. import image_config, image_routing
from ..agents import image_prompt_agent_key, run_agent
from ..brief_parser import (
    Brief,
    brief_uses_refs,
    director_frame_variant,
    route_emits_asset_prompts,
    route_emits_frame_prompts,
)
from ..config import Config
from ..generation import (
    IMAGE_FAMILY_LABELS,
    IMAGE_PROFILE_DEFAULTS,
    KIND_LABELS,
    canvas_display,
    composition_hint,
    fl2va_bundle_from_dict,
    image_canvas_size,
    ref_asset_registry,
    strip_foreign_frames,
    variant_frame_names,
)
from ..tools.h3_validator import validate_prompt
from ..tools.image_prompt_discipline import check_image_prompt
from ..tools.ref_metadata import format_ref_meta
from ..tools.theme_guard import theme_requirements_text
from ..user_revisions import (
    FRAME_BASELINE_KEY,
    FRAME_REVOKED_KEY,
    LAYER_ART,
    LAYER_CHARACTER,
    LAYER_STORY,
    pending_revisions,
    render_revision_block,
    setting_revision_block,
)
from .state import ASSET_KIND_FIELDS, PipelineState


# 各节点的上下文标签一律写字面量（与既有的「原始剧情」「分镜表」同形，读起来就是请求本身）。
# 其中「起步澄清」这个标签**必须逐字等于** clarification.BLOCK_LABEL（CONTEXT.md 的术语）：
# 它与「用户修订」会在同一个请求里并排出现，两个名字指同一件事就会有后人把它们读成一件事。
# 两者同形由 test_wizard_clarification 的标题一致性用例钉住。
def _ctx(**kw: str) -> str:
    """把带标签的上下文拼成一段。空值跳过。"""
    parts = [f"【{k}】\n{v}" for k, v in kw.items() if v]
    return "\n\n".join(parts)


def _brief_block(brief: Brief) -> str:
    refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
    return _ctx(
        模式=brief.mode,
        变体=brief.variant if brief.mode == "base" else "",
        时长=f"{brief.duration}s",
        风格=brief.style,
        语言=brief.language,
        剧情=brief.plot,
        起步澄清=brief.clarifications,
        # 导演台路线的生成模式（issue #103）：方言分派与段规划都按它走，请求里不给就等于
        # 所有模式拿到同一份口径。base/ref 的 director_mode 恒为空，_ctx 跳过、请求逐字不变。
        导演台模式=brief.director_mode if brief.is_director else "",
        参考资产=refs if brief_uses_refs(brief) else "",
        草稿=brief.draft or "",
    )


def _ref_assets_block(state: PipelineState, brief: Brief) -> str:
    """渲染【参考资产】块：N 张素材的编号登记 + 指称纪律（issue #41）。

    登记由 ``ref_asset_registry`` **现算**（编号按剧本出场顺序）——它依赖资产图提示词，
    而那是链上 ``parallel_image_prompts`` 的产物，构造节点时还不存在；这正是三个消费者
    （reference_consistency／身份圆桌／提示词工程师）都从这里取、不再各自抄一份的原因。

    多参纪律只对**产资产图的路线**说（``route_emits_asset_prompts``：ref 与导演台
    r2v/rv2v/mixed，复审 H6）：帧变体的 refs 是首/尾帧登记，套「素材语义名指称」
    会把两套指称体系搅在一起。判据原先读 ``brief.mode == "ref"``：导演台 r2v 的资产图
    提示词照常产出、素材登记却恒为空串，reference_consistency 与写段拿到的参考资产
    是「（无）」——指称与挂图对不上。多参纪律与角色提示词（prompt_engineer.md）互为
    表里：这里钉**数据侧**（哪些编号、哪些名字、描述取自哪），角色提示词钉写法。
    """
    if route_emits_asset_prompts(brief):
        refs = ref_asset_registry(state, brief)
        if not refs:
            return ""
        return (
            "编号按剧本出场顺序分配（编号即叙事顺序，不得改号）。"
            "破折号后的描述就是该素材的**生图提示词**（素材画面事实：画面描述以它为描述主体）。"
            "素材指称以**语义名为主**，<Picture N> 官方标签在格式要求处兜底。\n"
            + format_ref_meta(refs)
        )
    # 帧路线的 refs 是首/尾帧登记（base 的 FL2VA/I2VA 等）：判据与改动前逐字相同，
    # ``route_emits_asset_prompts`` 对 base 恒为假、对导演台 i2v/fl2v 也为假。
    if brief_uses_refs(brief) and brief.refs:
        return format_ref_meta(brief.refs)
    return ""


def _make_producer_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        return {"production_plan": run_agent(agents["producer"], f"请给出制作计划：\n{_brief_block(brief)}")}
    return node


def _make_director_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = f"请给出导演阐述：\n{_ctx(原始剧情=brief.plot, 起步澄清=brief.clarifications, 制作计划=state.get('production_plan', ''))}"
        return {"director_brief": run_agent(agents["director"], msg)}
    return node


def _make_screenwriter_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = (
            "请写出分场剧本：\n"
            + _ctx(
                原始剧情=brief.plot,
                起步澄清=brief.clarifications,
                # 剧情层的设定级修订（issue #28）：层次不匹配时渲染成空串、被 _ctx 跳过
                设定级修订=setting_revision_block(state, LAYER_STORY),
                导演阐述=state.get("director_brief", ""),
                创意锁定=state.get("creative_lock", ""),
            )
        )
        return {"script": run_agent(agents["screenwriter"], msg)}
    return node


def _design_context(state: PipelineState, brief: Brief, layer: str) -> str:
    """为三类生图设计师提供同一份剧情约束，避免脱离故事自由发挥。

    ``layer`` 是**本节点负责的那一层**：设定级重跑时只有该层的节点能拿到本轮那条修订
    （``setting_revision_block`` 按层次过滤）。重跑起点是**节点组**——``parallel_designers``
    一次跑三个设计师——不过滤的话一条「汉服改成现代审美」会把背景与道具设计一起改写，
    用户没要求的产物被替换掉。
    """
    refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
    return _ctx(
        原始剧情=brief.plot,
        起步澄清=brief.clarifications,
        设定级修订=setting_revision_block(state, layer),
        导演阐述=state.get("director_brief", ""),
        创意锁定=state.get("creative_lock", ""),
        分场剧本=state.get("script", ""),
        风格=brief.style,
        参考资产=refs if brief_uses_refs(brief) else "",
    )


def _make_design_node(agents: dict, role: str, output_field: str, label: str,
                      layer: str) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = f"请完成{label}，必须严格契合视频剧情：\n{_design_context(state, brief, layer)}"
        return {output_field: run_agent(agents[role], msg)}
    return node


# 中文资产类（节点请求里说的话）→ 配置里的键（``image_config.IMAGE_KINDS``）。
# **逆映射自 ``generation.KIND_LABELS``**：那份是「键 → 中文」的唯一定义处，这里再抄一份
# 「人物=character」就是第二份会各自漂移的映射（改键名时漏一处，节点会去取不存在的档）。
_KIND_KEY_BY_LABEL = {label: key for key, label in KIND_LABELS.items()}


def _image_prompt_agent(agents: dict, model_key: str):
    """取该档模型的资产图提示词 agent；缺键就**点名缺哪个模型**，不回落。

    静默回落是这里最坏的失败形态：出的词按另一家的写法写（Z-Image 要中文短句、Qwen 要
    英文长文），产物照样生成、不报错，只是质量掉一档。所以键不在就直接响。
    """
    key = image_prompt_agent_key(model_key)
    if key not in agents:
        raise ValueError(
            f"没有为生图模型 {model_key!r} 装配资产图提示词 agent（缺键 {key!r}）——"
            "装配处应按 image_config.IMAGE_MODEL_KEYS 为每档各建一个")
    return agents[key]


# 两张「类别 → state 键」的视图，都从 ``graph/state.ASSET_KIND_FIELDS`` 的**唯一声明**
# 派生（issue #88 复审：nodes 里再立一张表就是同一契约的第二处口径，改一处漏一处）。
ASSET_DESIGN_FIELDS: dict[str, str] = {
    kind: design_field for kind, (_prompt_field, design_field) in ASSET_KIND_FIELDS.items()
}
# 路由读「上一次的条目」时按它取（人手动改过的模型记在那一条上）。
_IMAGE_PROMPTS_FIELD_BY_KIND: dict[str, str] = {
    kind: prompt_field for kind, (prompt_field, _design_field) in ASSET_KIND_FIELDS.items()
}


def _asset_prompt_request(kind: str, design_field: str, state: dict, brief: Brief,
                          *, only: str = "") -> str:
    """资产图出词请求正文；``only`` 非空时只出**那一张**（issue #88 的单条重出）。

    点名资产用「资产「名字」」这个形式：它是模型手上唯一「只回这一条」的锚（请求里同时
    给了整类的设计正文，不点名就无从知道要回哪一条）。

    请求不点名**生图模型**：模型由 agent 的 system prompt（纪律块）决定，请求里再说一遍
    就成了两处口径，改配置不改请求时两者会打架（issue #87 的裁定）。
    """
    scope = (
        f"请为{kind}设计中的资产「{only}」生成一条可直接复制的生图提示词。"
        f"设计里有几个{kind}资产都不用管：只输出这 1 条。只输出 JSON 数组。\n"
        if only else
        f"请为{kind}设计中的每个资产各生成一条可直接复制的生图提示词。"
        f"设计里有几个{kind}资产就输出几条。只输出 JSON 数组。\n"
    )
    return scope + _ctx(
        原始剧情=brief.plot,
        起步澄清=brief.clarifications,
        分场剧本=state.get("script", ""),
        对应设计=state.get(design_field, ""),
        美术统筹=state.get("art_design", ""),
        视觉风格=brief.style,
        语言=brief.language,
        # 画布是**本次请求的数据**（不住在系统提示词里）：构图那套词按画幅族分
        # 两套，而用户选定的画布形状各案不同。词表里写着「画布与尺寸一律以请求里的
        # 【画布】块为准」——不带这块，那条指针就没有落点（issue #33 在帧节点踩过
        # 同一个坑）。
        画布=_canvas_block(brief.canvases),
    )


def _generate_asset_entries(agent, kind: str, design_field: str, state: dict, brief: Brief,
                            *, only: str = "") -> list[dict]:
    """跑一次资产图出词并归一成条目（多资产与单条重出共用这一条路）。

    ``only`` 非空时要求回复里**恰好是那一张**：模型回了别的名字就响——写进这张条目的
    词会属于另一张资产，而下游按语义名对号，串了也没人看得见（静默失败）。
    """
    raw = run_agent(agent, _asset_prompt_request(kind, design_field, state, brief, only=only))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 纯格式转换，但它同样用这份系统提示词——词表里的【画布】指针照样得在场。
        repair = run_agent(
            agent,
            f"把以下内容转换为严格 JSON 数组，不要 markdown：\n{_ctx(画布=_canvas_block(brief.canvases))}\n{raw}",
        )
        try:
            parsed = json.loads(repair)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{kind} 生图提示词不是有效 JSON") from exc
    # 清单层违约先修一次（issue #64）：条目是转述时下游的偏差判定会静默失效，
    # 但一次违约就停住整条阶段 1 的代价与这个模型的失败模式不匹配。
    violations = _locked_violations(parsed, kind)
    if violations:
        patch = run_agent(
            agent,
            _build_locked_repair_request(kind, violations, _canvas_block(brief.canvases)),
        )
        merged = _apply_locked_repair(parsed, kind, patch, violations)
        try:
            entries = _normalize_asset_entries(merged, kind)
        except ValueError as exc:
            # 修复后仍不合规才停：给重跑的人留下「已试过一次修复」与逐条明细，
            # 一眼分清是模型固执还是可修（节点原始报错里没有这层信息）。
            raise ValueError(f"{kind} 锁死清单修复 1 次后仍不合规：{exc}") from exc
    else:
        entries = _normalize_asset_entries(parsed, kind)
    if only:
        names = [str(entry.get("name", "")) for entry in entries]
        if names != [only]:
            raise ValueError(
                f"单条重出返回的不是被点名的资产：期望 ['{only}']，实际 {names}")
    return entries


def regenerate_asset_prompt(agents: dict, kind_key: str, name: str, state: dict, brief: Brief,
                            *, model: str) -> dict:
    """按**指定模型**只重出一条资产图提示词（issue #88）。

    两个调用方：自动路由命中时给那一张换 MOSS 重出，以及素材循环里人手动改模型后的
    重出。走同一个函数的意义是「换模型＝换纪律块」这条#87 的裁量只有一处接线——
    另写一条路径，早晚会有一条漏掉纪律块，出的词按别家的写法写而不报错。

    ``kind_key`` 是英文档名（``character``/``prop``/``scene``）——本文件里中文的
    ``kind``（人物/道具/场景）只用于请求措辞，两者不混用一个名字。

    返回的是**单条条目**（四键），模型归属由调用方写进条目（``image_routing``）。
    """
    if kind_key not in ASSET_KIND_FIELDS:
        raise ValueError(f"未知资产类别：{kind_key!r}（只认 {', '.join(ASSET_KIND_FIELDS)}）")
    entries = _generate_asset_entries(
        _image_prompt_agent(agents, model), KIND_LABELS[kind_key],
        ASSET_DESIGN_FIELDS[kind_key], state, brief, only=name)
    return entries[0]


def _make_image_prompt_node(agents: dict, kind: str, design_field: str, output_field: str) -> Callable:
    """把模型无关设计资料翻译为 N 条生图提示词（每资产一条，issue #40）。

    这是**资产图**那条路（人物／道具／场景参考图，按剧本设计动态枚举 N 个资产），
    与关键帧那条路（``_make_fl2va_frame_prompt_node``）是两件事，但同属生图侧、
    用同一份词表切片。Flux.2 版已全链取消：单资产只出一版。

    出图模型按档取（``config/image.yaml``，issue #87）：人物／道具／场景可各用不同模型，
    对应 agent 的 system prompt 里带的是**该模型**的纪律块。请求正文只说「生图提示词」，
    不点名模型——点名会与另一档的纪律块打架（改配置不改请求就成了两处口径）。

    **issue #88 起按张路由**：先用该档配置的模型出词，再逐条判「文字类关键词命中／
    人手动指定」；与刚用过的模型不同的那几张，用**目标模型的 agent 重出这一条**。
    判定写进条目（``image_routing.ENTRY_KEY``），下游才可能知道「这条词是按哪一档的
    纪律写的」——改写（改词再掷／AI 改词）要按同一档的纪律改它。
    """
    kind_key = _KIND_KEY_BY_LABEL.get(kind)
    if kind_key is None:
        raise ValueError(
            f"未知资产类别：{kind!r}（只认 {', '.join(KIND_LABELS.values())}）")

    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        # 路线守卫（issue #78 / 父 spec #77，issue #103 换判据）：资产图提示词只属于
        # 「会用参考素材」的路线——ref，以及导演台的 r2v/rv2v/mixed。首尾帧把人物、
        # 道具、场景**融合**进一张关键帧，没有「每件资产单独出图」这一步；导演台的
        # t2v/i2v/fl2v/v2v 同样没有参考素材。无条件跑就是白付 3 次调用，
        # 锁死清单不合规还会让整条阶段 1 失败。只早退、不改管线拓扑。
        if not route_emits_asset_prompts(brief):
            return {}
        # 模型在**调用时**取：配置是环境事实（换机器换模型只改 yaml），每次出词都现读。
        model_key = image_config.image_model_for(kind_key)
        entries = _generate_asset_entries(
            _image_prompt_agent(agents, model_key), kind, design_field, state, brief)
        entries = _route_asset_entries(
            agents, kind_key, entries, state, brief, default_model=model_key)
        # 纪律检查**排在归一与路由之后**：只有形状合规的条目才谈得上「正文写法」，
        # 也保证每条按**它自己那一档**的判据检查（四类只在 Z-Image 档成立，issue #87）。
        _warn_image_prompt_discipline(entries, kind, model_key)
        return {output_field: entries}
    return node


def _route_asset_entries(agents: dict, kind_key: str, entries: list[dict], state: dict,
                         brief: Brief, *, default_model: str) -> list[dict]:
    """给每条资产定模型并按需重出（issue #88 / 父 spec #84 D8）。

    判据是「该类的设计正文 ＋ 这一条自己的提示词」；人手动改过的（``source=manual``，
    在条目上，经得住阶段 1 重跑）压过自动规则。重出只针对**模型与刚用过的那个不同**
    的那几张：未命中的照旧用第一次的产物，不白花调用。

    设计正文是**整类共用**的一段文字，故它命中的关键词会让这一类的每一张都切过去
    （提示词里命中的才只切那一张）。这是这条规则的粒度（拿不到「设计里的哪一句属于
    哪一张」），不是缺陷：人可以在素材循环里逐张改回来，而漏判（该切没切）在这个规则
    下不会发生。
    """
    design = str(state.get(ASSET_DESIGN_FIELDS[kind_key], ""))
    # 上一次的条目按类别键**直接取**（不用 .get 兜底）：未知类别在这里就该响——静默
    # 取空表会让人的手动选择在重跑后无声消失，而「按配置默认」看起来一切正常。
    previous = {
        str(entry.get("name", "")): entry
        for entry in (state.get(_IMAGE_PROMPTS_FIELD_BY_KIND[kind_key]) or [])
        if isinstance(entry, dict)
    }
    for index, entry in enumerate(entries):
        name = str(entry.get("name", ""))
        manual = image_routing.manual_model_of(previous[name]) if name in previous else None
        value = image_routing.assignment(
            kind_key,
            texts=(design, str(entry.get("positive_prompt", ""))),
            manual=manual,
        )
        if value["model"] != default_model:
            entry = regenerate_asset_prompt(agents, kind_key, name, state, brief,
                                            model=value["model"])
        image_routing.put_assignment(entry, value)
        entries[index] = entry
        # 只报「不是按配置默认」的那些：每张都打一行会把真正需要复核的判断淹在噪声里。
        if value["source"] != image_routing.SOURCE_CONFIG:
            print(f"[生图模型] {KIND_LABELS[kind_key]}「{name}」{image_routing.described(value)}")
    return entries


def print_image_prompt_warnings(prompt: str, model: str, prefix: str) -> None:
    """打印一条提示词的警告级纪律检查（节点与素材循环**共用这一处循环**）。

    两处各写一遍 ``for warning in check_image_prompt(...)`` 的代价是：将来加一档警告
    文案或改判据时只改到一处，另一条出词路径就静默不再报（本仓「规则在、接线不在」的
    典型形态）。前缀由调用方给——两边面向的位置不同（阶段 1 出词 / 菜单里的重出），
    消息体同源。
    """
    for warning in check_image_prompt(prompt, model=model):
        print(f"{prefix}{warning.message}")


def _warn_image_prompt_discipline(entries: list[dict], kind: str, model: str) -> None:
    """出词后打印警告级纪律检查结果（issue #68 / ADR 0018 / issue #87）。

    **只打印、不改产物、不阻断**：Turbo 的失败模式是「细节没画出来」，不是
    「输出不合规」；做成 error 闸门会被合法写法误伤，把一份本来能出图的提示词停掉。
    警告打在节点里而不是展示口：真实运行都经过节点，且本文件已有同样的先例
    （关键帧节点剥帧的告警）。

    ``model`` 是**回落档**：issue #88 起同一类里可能逐张分属不同模型（命中文字类关键词
    的那张切到 MOSS），每条的判据按它自己记录的那一档跑；没有记录（旧会话）才用 ``model``。
    判据本身的依据（五类里四类只在 Z-Image 档成立）住在 ``check_image_prompt`` 一处。
    """
    for entry in entries:
        print_image_prompt_warnings(
            str(entry.get("positive_prompt", "")),
            image_routing.model_of(entry) or model,
            f"[警告] {kind}「{entry.get('name', '')}」生图提示词：",
        )


def _normalize_asset_entries(parsed: object, kind: str) -> list[dict]:
    """把资产图节点的 JSON 回复归一成四键资产条目列表（issue #40；锁死清单 issue #53）。

    只保留 ``name / positive_prompt / negative_prompt / locked_features``
    四键：多余的键（模型键 ``zimage``/``flux2``、已删的 ``instructions`` 等）不进 state——
    那是被取消的维度，
    放它进来会让下游再长出「按模型分叉」的读法。语义名必填：素材台账／注入卡靠它对号。

    **锁死清单**（issue #53）在这里连两道闸门一起把住：

    - 缺席／空表 → 响。清单是对照器判偏差的**唯一判据**，空表＝不收窄＝「8 张图 39 条
      偏差逐条三选」那场人裁灾难原样回来，而且没人看得出为什么（静默失败）；
    - 条目不是 ``positive_prompt`` 的**原文片段** → 响。读图逐字摘录的那句要落得到条目上，
      条目是转述的话命中判据恒不成立、偏差悄悄清空——同样是静默失败。这条不变式
      由角色提示词要求模型「逐字摘录正文」，这里把它钉成可证的检查。
    """
    if not isinstance(parsed, list):
        raise ValueError(f"{kind} 生图提示词 JSON 顶层必须是数组（每资产一项）")
    entries: list[dict] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"{kind} 生图提示词第 {index + 1} 项不是对象")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"{kind} 生图提示词第 {index + 1} 项缺少资产语义名 name")
        positive = str(item.get("positive_prompt", "")).strip()
        if not positive:
            raise ValueError(f"{kind} 资产「{name}」的 positive_prompt 为空")
        locked = _normalize_locked_features(item.get("locked_features"), name, positive, kind)
        entries.append({
            "name": name,
            "positive_prompt": positive,
            "negative_prompt": str(item.get("negative_prompt", "")),
            # 模型若仍按旧模板回了 instructions，这里直接丢掉（2026-10-24 删除该键）：
            # 产物没有「操作说明」出口，留着只会在 state 里多一份没人读的正文复制品。
            "locked_features": locked,
        })
    return entries


def _locked_problem(raw: object, positive: str) -> str | None:
    """返回锁死清单的违约描述；合规时返回 ``None``（issue #53 的不变式判据）。

    这是**唯一**一份清单判据：节点校验（``_normalize_locked_features``）与一次修复
    重试（``_locked_violations``）都从这里取口径。两处各写一份子串判断的话，改一处
    另一处就静默失效——本仓「同一契约两处口径」已经咬过两次（issue #56／#57）。

    返回值接在「{kind} 资产「{name}」：」之后读，所以每句都以自己的主语起头，不带前缀。
    """
    if raw is None:
        return ("缺 locked_features（锁死清单）——"
                "它是读图校对判偏差的唯一判据，缺席等于把整篇提示词当判据（人裁被淹）")
    if not isinstance(raw, list):
        return f"locked_features 不是数组：{raw!r}"
    locked = [str(x).strip() for x in raw]
    locked = [x for x in locked if x]
    if not locked:
        return "locked_features 是空表——至少要一条身份特征"
    bad = [x for x in locked if x not in positive]
    if bad:
        return (f"锁死清单条目 {bad[0]!r} 不是 positive_prompt 的原文片段"
                "——条目必须逐字摘自正文，否则读图摘录的引文落不到条目上、偏差判不出来")
    return None


def _normalize_locked_features(raw: object, name: str, positive: str, kind: str) -> list[str]:
    """归一一条资产的锁死清单（issue #53）；坏形状与不合不变式的条目都响。

    形状坏（不是数组）与条目坏（空、或不是正文原文片段）分开报错，是为了让重跑的人
    一眼看出是模型没交清单、还是交了一份转述——两者的修法不同（改角色提示词措辞 vs
    让人工看一眼那条到底想锁什么）。两类现在都先走一次补丁式修复（issue #64）；
    修复后仍不合规才到这里抛错。
    """
    problem = _locked_problem(raw, positive)
    if problem is not None:
        raise ValueError(f"{kind} 资产「{name}」：{problem}")
    return [str(x).strip() for x in raw if str(x).strip()]


def _locked_violations(parsed: object, kind: str) -> list[dict]:
    """收集**清单层**违约，供节点做一次补丁式修复（issue #64）。

    为什么需要修复而不是一次违约就停：条目转述时下游会**静默**把整条偏差降级成
    「理想描述出入」（``asset_proofread._locked_hit`` 双向子串必不命中），所以闸门
    必须响；但场景／道具的身份特征在设计资料里就是抽象描述，模型从资料概括而不是从
    正文摘的概率不低，一次违约就让整条阶段 1 停住、重跑还要重付已成功的那几路调用，
    代价与触发点不匹配。一次重交的成本是 1 次调用，且本仓已有同形先例（关键帧 bundle
    外层回灌错误重写一次）。

    结构性坏法（顶层不是数组、项不是对象、name／positive_prompt 缺）**不收**：修复
    请求的格式是 ``name → locked_features``，结构坏了补丁无处落，重交只会再撞同一道闸；
    它们仍由 ``_normalize_asset_entries`` 原样报错。
    """
    if not isinstance(parsed, list):
        return []
    violations = []
    for item in parsed:
        if not isinstance(item, dict):
            return []
        name = str(item.get("name", "")).strip()
        positive = str(item.get("positive_prompt", "")).strip()
        if not name or not positive:
            return []
        problem = _locked_problem(item.get("locked_features"), positive)
        if problem is not None:
            violations.append({
                "name": name,
                "positive_prompt": positive,
                "locked_features": item.get("locked_features"),
                "problem": problem,
            })
    return violations


def _build_locked_repair_request(kind: str, violations: list[dict], canvas_block: str) -> str:
    """拼一次补丁式修复请求：只收 ``name → locked_features``，正文冻结。

    为什么把 ``positive_prompt`` 全文交回去：模型要「逐字摘」，手里就得有那篇正文；
    只给条目名的话它只能再转述一次。为什么不许它改正文：图词已经设计定稿，允许改
    正文等于让修复顺手改产物，而且「条目在正文里」可以被改正文满足——唯一变量必须是
    清单，diff 才审得动。
    """
    lines = [
        f"以下{kind}资产的锁死清单不合规。请**只修正 locked_features**：",
        "每一条必须是该资产 positive_prompt 里**原样存在**的连续片段（逐字复制，不改写、"
        "不概括、不加「发色：」这类前缀），每条资产 2–5 条。",
        "不要改动 positive_prompt，也不要输出它或其它字段。",
        "只输出 JSON 数组，每项只含 name 与 locked_features 两个键。",
        canvas_block,
    ]
    for v in violations:
        lines.append(
            f"- name：{v['name']}\n"
            f"  positive_prompt：{v['positive_prompt']}\n"
            f"  不合格的清单：{v['locked_features']!r}\n"
            f"  问题：{v['problem']}")
    return "\n".join(lines)


def _apply_locked_repair(parsed: list, kind: str, raw: str, violations: list[dict]) -> list:
    """把修复补丁按 ``name`` 合并回原条目（正文与其余字段一字不动）。

    补丁缺资产就整次判失败：静默留下某个资产的坏清单，等于让下游的偏差判定继续悄悄
    失效——正是这道闸门要防的那件事。
    """
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{kind} 锁死清单的修复结果不是有效 JSON（已修复尝试 1 次）：{raw[:200]!r}") from exc
    if not isinstance(patch, list):
        raise ValueError(f"{kind} 锁死清单的修复结果顶层必须是数组（已修复尝试 1 次）")
    replacements: dict[str, object] = {}
    for item in patch:
        if not isinstance(item, dict) or "locked_features" not in item:
            raise ValueError(
                f"{kind} 锁死清单的修复结果每项只许有 name 与 locked_features（已修复尝试 1 次）：{item!r}")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"{kind} 锁死清单的修复结果有一项缺 name（已修复尝试 1 次）")
        replacements[name] = item.get("locked_features")
    missing = [v["name"] for v in violations if v["name"] not in replacements]
    if missing:
        raise ValueError(f"{kind} 锁死清单的修复结果缺了这些资产：{missing}（已修复尝试 1 次）")
    merged = []
    for item in parsed:
        name = str(item.get("name", "")).strip()
        if name in replacements:
            # 只替换清单键；其余键（尤其 positive_prompt）原样保留——这是补丁式的全部意义。
            item = {**item, "locked_features": replacements[name]}
        merged.append(item)
    return merged


def _canvas_block(canvases: dict[str, tuple[int, int]] | None = None) -> str:
    """生图侧请求里的【画布】块（issue #33 接线，issue #42 改为用户选定）。

    模型不知道自己在给哪个画幅写构图，就会按一种画幅的直觉安排主体位置。每行带三样：
    **用户选定的画布**（显示名+尺寸，与渲染层的画布行同取 ``canvas_display``）、
    **画幅形状**与**构图分套指示**（``composition_hint``）——分套绑画幅族不绑模型
    （Q21(c)），族外（横幅、极端比例）的指示明说「不用竖幅/正方任一套」，把
    「横幅画面配竖幅构图词」的说反话堵在请求这一层。

    行的来源就是**生图 profile 默认表本身**（家族清单从 `generation` 取）：本模块里
    再抄一份家族元组的话，将来加第三个生图家族时【画布】块会**静默少一行**。尺寸经
    ``image_canvas_size(family, canvases)`` 解析——用户选定优先、未选回落默认表。
    """
    lines = []
    for family in IMAGE_PROFILE_DEFAULTS:
        width, height = image_canvas_size(family, canvases)
        lines.append(
            f"- {IMAGE_FAMILY_LABELS[family]}（{family}）："
            f"{canvas_display(width, height)}；{composition_hint(width, height)}"
        )
    return "\n".join(lines)


def _scene_terms_block(brief: Brief) -> str:
    """渲染【场景词】块：主题要求的地点词组 ＋ 它们该落在哪儿。**恒为非空**。

    这是帧节点的三条请求（主 / 转 JSON / 改写）在讲地点约束时的唯一一处真相，别处只指回它。

    为什么必须明写：校验器（`generation.validate_fl2va_bundle`）做的是**字面子串**判定，
    而请求里从前只有一句「主题地点是硬约束」——模型无从知道要哪几个字面。实测该次运行
    两轮都没交出「室内」，改写重试那句「使其通过主题地点校验」在信息上**不可赢**
    （issue #37，物证见 output/issue37-evidence/）。词表与整片路径同源，不另抄一份。

    为什么落在「锚点或**确实处在该地点的那一帧**」而不是「每个正向提示词」：词表从**整条**
    主题抽取，而一张关键帧只画主题里的一个时刻——主题前半在天宫、后半在高楼室内时，
    硬要天宫那一帧含「室内」就是逼模型写假话。

    为什么无约束时也要有一段：角色提示词无条件写着「主题要求的地点词见输入里的【场景词】
    块」，空串会被 `_ctx` 跳过、指针就悬空了（复审实测 plot＝「深夜便利店一只橘猫…」）。
    同【画布】块——那块也是恒在的。
    """
    text = theme_requirements_text(
        brief.plot, target="scene_anchor 或画面确实在该地点的那一帧")
    if text:
        return text
    return ("主题未命中本仓的地点词表，本片无地点硬约束；仍不得写入主题里没有的地点，"
            "也不得改写成泛化的户外环境。")


def _make_fl2va_frame_prompt_node(agents: dict) -> Callable:
    """把完整镜头上下文转换为对应变体的关键帧生图提示词（FL2VA 双帧 / I2VA 仅首帧 / L2VA 仅尾帧）。"""
    def parse_bundle(raw: str, brief: Brief) -> dict:
        # 有效变体从路线能力判据取（issue #103）：导演台的 brief.variant 是 T2VA 占位，
        # 照读它会按 T2VA 拼「必须保留哪些帧」，i2v/fl2v 的首帧词就被静默丢掉。
        variant = director_frame_variant(brief)
        # 「必须保留哪些帧」现拼（复审指出）：变体→帧那份知识住在 `variant_frame_names`，
        # 这里再抄一张表就是同一件事的两处真相——本仓「规则在、接线不在」的温床。
        required = "、".join(variant_frame_names(variant))
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # 这条**不是**纯格式转换：它给出了一个要保留的**内容清单**，所以清单里的东西
            # 一样都不能少——`composition` / `lighting` 是角色提示词明文要求的字段
            # （issue #33），漏在清单外就等于这条路上没人要它们。
            # 【画布】块也必须带：角色提示词现在写着「画幅见输入里的【画布】块」，
            # 那次调用同样用这份系统提示词，不带就成了**悬空指针**。
            repair = run_agent(
                agents["frame_prompt_engineer"],
                f"把以下内容转换为严格 JSON，不要 markdown，必须保留 {required}、scene_anchor、"
                f"continuity_constraints，以及 composition 与 lighting 字段：\n"
                f"{_ctx(画布=_canvas_block(brief.canvases), 场景词=_scene_terms_block(brief))}\n原始内容：\n{raw}",
            )
            try:
                parsed = json.loads(repair)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{variant} 关键帧提示词不是有效 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{variant} 关键帧提示词 JSON 顶层必须是对象")
        # 模型多返回的关键帧在这里**看得见地**剥掉（issue #38）：剥离本身是确定性的
        # （变体是用户的选择，见 `strip_foreign_frames`），但「模型产物被丢掉」这件事
        # 不能静默——本仓的「绝不静默失败」。告警打在这个节点就够：真实运行都经过它。
        # 随后的 `fl2va_bundle_from_dict` 里还会再剥一次，那一次是**不变量**（任何调用方
        # 都绕不过），此处这一剥只为拿到帧名好说话。
        parsed, dropped = strip_foreign_frames(parsed, variant)
        if dropped:
            print(f"[警告] {variant} 变体不含 {'、'.join(dropped)} 帧，"
                  f"模型多返回的它已剥离，不会进入产物。")
        bundle = fl2va_bundle_from_dict(parsed, brief)
        return bundle.to_dict()

    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        # 路线能力判据（issue #103）：base 按变体、导演台按生成模式（i2v/fl2v），
        # ref 恒不产帧图。判据住 brief_parser 一处，这里不写 mode/variant 的裸比较。
        if not route_emits_frame_prompts(brief):
            return {}
        variant = director_frame_variant(brief)
        requirement = {
            "FL2VA": "请为这一段 FL2VA 视频生成融合的首帧（first）和尾帧（last）静态生图提示词；"
                     "两帧必须是同一空间、同一组人物、同一套服装和同一组关键道具的连续状态。",
            "I2VA": "请只生成首帧（first）静态生图提示词：视频 0.00s 的开场完整静态画面；"
                    "人物、道具和场景必须融合在同一画面中。",
            "L2VA": "请只生成尾帧（last）静态生图提示词：视频结束时的完整静态画面；"
                    "人物、道具和场景必须融合在同一画面中。",
        }[variant]
        repair_hint = {"FL2VA": "使其通过主题地点和首尾帧连续性校验",
                       "I2VA": "使其通过主题地点校验",
                       "L2VA": "使其通过主题地点校验"}[variant]
        scene_terms = _scene_terms_block(brief)
        message = (
            requirement
            + "人物、道具和场景必须同时出现在每张关键帧中（不要拆成资产卡片）；"
              "主题要求的地点词以输入里的【场景词】块为准。"
            # 构图/光线两个字段（issue #33）：解析器一直支持读写，却无人写——实测仓库里
            # 9 份真实产物（5 个会话）的 18 行全是空串。字段名进请求是「有生产者」的第一步。
            + "必须给出 composition 与 lighting 两个字段"
              "（按画布写构图、按光线的方向/强度/质感写光）。"
            + "只输出 JSON。\n"
            + _ctx(
                变体=brief.variant,
                原始主题=brief.plot,
                # 词表是**本次请求的数据**、不在系统提示词里，故三条请求各自带一份。
                # 为什么、以及词该落在哪儿，见 `_scene_terms_block`。
                场景词=scene_terms,
                起步澄清=brief.clarifications,
                时长=f"{brief.duration}s",
                视觉风格=brief.style,
                语言=brief.language,
                # 画布是**本次请求的数据**（不住在系统提示词里），所以主请求与下面的
                # 改写重试都要带：少了它，模型只能按自己的直觉重新安排构图。
                画布=_canvas_block(brief.canvases),
                # 累积的用户修订：人机修改循环每轮重出都把它带进上下文（issue #23）。
                # 自动质检循环不设该键 → 空值被 _ctx 跳过，它的替换语义逐字不变。
                用户修订=render_revision_block(pending_revisions(state)),
                # 修订基线（issue #25）：上一轮**完整产物**渲染成的块，由人机修改循环经独立
                # 入参键传入——没有它，用户没提过的细节每轮都会跟着重掷一起漂走。
                修订基线=state.get(FRAME_BASELINE_KEY),
                # 本轮撤销（issue #24）：被撤的修订不再生效，其造成的画面改动要跟着撤回。
                # 不说给模型听的话，基线那句「未提到的部分原样保留」会把它们原样保住。
                本轮撤销=state.get(FRAME_REVOKED_KEY),
                分场剧本=state.get("script", ""),
                人物设计=state.get("character_design", ""),
                道具设计=state.get("prop_design", ""),
                背景设计=state.get("background_design", ""),
                美术统筹=state.get("art_design", ""),
                分镜表=state.get("shot_table", ""),
                镜头评审锁定=state.get("shot_review_lock", ""),
                画面细化=state.get("visual_design", ""),
                身份一致性锁定=state.get("identity_lock", ""),
                质检问题="\n".join(state.get("frame_prompt_issues") or []),
            )
        )
        raw = run_agent(agents["frame_prompt_engineer"], message)
        try:
            bundle = parse_bundle(raw, brief)
        except ValueError as first_error:
            # 这条改写请求**重写整份 JSON**，所以它必须带上主请求的全部要求：少了修订、
            # 基线或本轮撤销，这一轮就退回纯重掷（用户没提过的细节漂走）、把用户说过的
            # 意见丢掉，或把已经撤销的改动又写回来。
            # 它只在产物过不了校验时走，正常路径走不到——最容易只接主请求那一处（澄清
            # 当年就漏在这条路上，见 tests/test_wizard_clarification.py 的重试用例）。
            # 内层「转成 JSON」那条重试不管内容（原文随请求一并发给模型），故不在此列。
            rewrite = _ctx(
                # 画布是数据、不是系统提示词里的常量，所以这条重写请求必须自带一份。
                # 相对地，composition / lighting 的字段要求住在角色系统提示词里（每次调用
                # 都在场），不必在这里重复——同「起步澄清」与「内容规则」的分工。
                画布=_canvas_block(brief.canvases),
                # 场景词同画布：数据、不在系统提示词里。少了它这条重写在信息上不可赢。
                场景词=scene_terms,
                起步澄清=brief.clarifications,
                用户修订=render_revision_block(pending_revisions(state)),
                修订基线=state.get(FRAME_BASELINE_KEY),
                本轮撤销=state.get(FRAME_REVOKED_KEY),
            )
            repair = run_agent(
                agents["frame_prompt_engineer"],
                f"重写以下 {variant} JSON，{repair_hint}。不要 markdown。\n"
                f"主题：{brief.plot}\n{rewrite}"
                f"错误：{first_error}\n原始结果：{raw}",
            )
            try:
                bundle = parse_bundle(repair, brief)
            except ValueError as exc:
                raise ValueError(f"{variant} 关键帧提示词生成失败：{exc}") from exc
        return {"fl2va_prompt_bundle": bundle}

    return node


def _make_art_director_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
        msg = "请统筹并裁决最终美术设计：\n" + _ctx(
            原始剧情=brief.plot,
            起步澄清=brief.clarifications,
            导演阐述=state.get("director_brief", ""),
            创意锁定=state.get("creative_lock", ""),
            分场剧本=state.get("script", ""),
            人物设计=state.get("character_design", ""),
            背景设计=state.get("background_design", ""),
            道具设计=state.get("prop_design", ""),
            参考资产=refs if brief_uses_refs(brief) else "",
        )
        return {"art_design": run_agent(agents["art_director"], msg)}
    return node


@dataclass
class ShotScale:
    """根据时长与内容复杂度推导的分镜规模建议。"""

    min_shots: int
    max_shots: int
    target_segment_seconds: float
    shot_seconds_ceiling: float = 10.0

    def describe(self) -> str:
        return (f"视频总时长按情节节拍分配，不按固定比例平分；预期 {self.min_shots}-{self.max_shots} 个镜头，"
                f"建议平均镜头时长 ~{self.target_segment_seconds:.0f}s（单镜头上限 {self.shot_seconds_ceiling:.0f}s，"
                f"超过的部分会由生产管线自动拆成连续执行段，无需在分镜中缩短镜头）。")


def derive_shot_scale(duration: float, plot_richness_hint: str = "") -> ShotScale:
    """根据时长和粗略内容复杂度推导镜头数与镜头时长区间。

    设定依据（H3 官方 base-en 案例 + ComfyUI H3 时长选项 4-10s 整数档）：
    - ≤10s：短影片，1-3 镜，单镜即执行窗口（4-10s 整数）。
    - 11-30s：中片，4-6 镜，镜头按情节节拍 5-7s，不硬限。
    - >30s：长片（本项目产能目标），8-12 镜，单镜需求被 H3 执行窗口约束，
      超过 shot_seconds_ceiling 的镜头由管线自动拆分。
    """
    if duration <= 10:
        return ShotScale(min_shots=1, max_shots=3, target_segment_seconds=7.0)
    if duration <= 30:
        return ShotScale(min_shots=4, max_shots=6, target_segment_seconds=6.0)
    return ShotScale(min_shots=8, max_shots=12, target_segment_seconds=min(6.0, 60.0 / 12))


def _shot_durations_from_table(shot_table: str, total_duration: float) -> list[float]:
    """从分镜表的 [Shot N] 块与时间戳推各镜头时长（用于均分检测）。"""
    from ..segment_prompts import _shot_blocks

    blocks = _shot_blocks(shot_table)
    if len(blocks) < 2:
        return []
    durations = [
        round(blocks[index + 1][1] - blocks[index][1], 3)
        for index in range(len(blocks) - 1)
    ]
    durations.append(round(max(total_duration - blocks[-1][1], 0.0), 3))
    return durations


def _make_storyboard_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        from ..segment_prompts import is_degenerate_durations

        brief: Brief = state["brief"]
        issues = "\n".join(state.get("qa_issues") or []) or ""
        scale = derive_shot_scale(brief.duration)
        msg = (
            "请给出分镜镜头表（视频总时长 "
            + f"{brief.duration}s，{scale.describe()}）：\n"
            + _ctx(原始剧情=brief.plot, 起步澄清=brief.clarifications,
                   # 剧情/分镜层的设定级修订（issue #28）：分镜表是这一层的产物之一，
                   # 修订（如「镜头再碎一些」）必须到得了它，只喂剧本等于让它自生自灭
                   设定级修订=setting_revision_block(state, LAYER_STORY),
                   分场剧本=state.get("script", ""), 美术设计=state.get("art_design", ""),
                   人物设计=state.get("character_design", ""),
                   背景设计=state.get("background_design", ""),
                   道具设计=state.get("prop_design", ""),
                   上一轮质检问题=issues)
        )
        shot_table = run_agent(agents["storyboard"], msg)
        durations = _shot_durations_from_table(shot_table, brief.duration)
        warning = ""
        if is_degenerate_durations(durations):
            # 均分 = 无视情节节拍的偷懒输出：自动重试一次（带明确指出问题）
            retry_msg = msg + (
                "\n\n[质检退回] 上轮所有镜头时长完全相同（"
                + f"{durations[0]:.1f}s × {len(durations)}），属于均分偷懒。"
                "请按情节节拍重新分配镜头时长（可以长短不一），并以相同格式重出分镜表。"
            )
            shot_table = run_agent(agents["storyboard"], retry_msg)
            durations = _shot_durations_from_table(shot_table, brief.duration)
            if is_degenerate_durations(durations):
                warning = ("SHOT_DURATIONS_UNIFORM: 重试后镜头仍为均分（"
                           f"{durations[0]:.1f}s × {len(durations)}），请人工检查节奏")
                print(f"[警告] 分镜时长仍是均分：{durations[0]:.1f}s × {len(durations)}")
        return {"shot_table": shot_table, **({"shot_duration_warning": warning} if warning else {})}
    return node


def _make_roundtable_node(rt: CompiledStateGraph, lock_field: str, topic: str,
                          context_getter: Callable[[PipelineState], str]) -> Callable:
    def node(state: PipelineState) -> dict:
        result = rt.invoke({
            "discussion": [],
            "round": 1,
            "topic": topic,
            "context": context_getter(state),
            "lock": "",
        })
        return {lock_field: result.get("lock", "")}
    return node


def _make_cinematographer_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = "请细化每个镜头的画面描述：\n" + _ctx(
            原始剧情=brief.plot,
            起步澄清=brief.clarifications,
            镜头表=state.get("shot_table", ""),
            镜头评审锁定=state.get("shot_review_lock", ""),
        )
        return {"visual_design": run_agent(agents["cinematographer"], msg)}
    return node


def _make_reference_consistency_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        # 路线能力判据（issue #103）：subject_definitions/retention_analysis 是参考素材的
        # 定义块，住在「会用参考素材」的路线（ref、导演台 r2v/rv2v/mixed）。base 与导演台的
        # t2v/i2v/fl2v 没有它，跳过而不是产一份没人用的定义。
        if not route_emits_asset_prompts(brief):
            return {"subject_defs": ""}
        # 登记现算（issue #41）：本节点跑在 parallel_image_prompts 之后，拿得到 N 资产；
        # brief.refs 为空的向导多参路线正是靠它才有 <Picture N> 映射可锁。
        refs = _ref_assets_block(state, brief) or "（无）"
        msg = "请给出 subject_definitions 与 retention_analysis 素材：\n" + _ctx(
            参考资产=refs, 美术设计=state.get("art_design", ""),
            人物设计=state.get("character_design", ""),
            背景设计=state.get("background_design", ""),
            道具设计=state.get("prop_design", ""), 一致性锁定=state.get("identity_lock", ""),
        )
        return {"subject_defs": run_agent(agents["reference_consistency"], msg)}
    return node


def _make_sound_designer_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = "请给出对白表与环境声：\n" + _ctx(
            画面细化=state.get("visual_design", ""),
            分场剧本=state.get("script", ""),
            对白语言=f"{brief.language}（对白原文必须保留）" if brief.language else "",
        )
        return {"sound_design": run_agent(agents["sound_designer"], msg)}
    return node


def _make_composer_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        msg = "请给出 non_diegetic_music 素材：\n" + _ctx(画面细化=state.get("visual_design", ""))
        return {"music": run_agent(agents["composer"], msg)}
    return node


def _fl2va_frame_context(state: PipelineState) -> str:
    """渲染首尾帧锚定上下文：真实图片描述优先，生图提示词降级为补充。

    - state["fl2va_frame_descriptions"] 非空 → 用 qwen3.7-plus 读出的真实画面描述
      作为主锚定（「视频第一帧实际画面是……」），并附生图提示词作对照；
    - 否则回退到 fl2va_prompt_bundle 里的首尾帧生图提示词（阶段 1 计划画面）。
    """
    lines: list[str] = []
    raw = state.get("fl2va_prompt_bundle")
    bundle = raw if isinstance(raw, dict) else {}
    if str(bundle.get("scene_anchor", "")).strip():
        lines.append(f"场景锚点（视频全程不得离开）：{str(bundle['scene_anchor']).strip()}")

    def _planned_prompt(frame_key: str) -> str:
        rows = bundle.get(frame_key)
        if not isinstance(rows, list):
            return ""
        row = next((r for r in rows if isinstance(r, dict) and r.get("model_family") == "zimage"), None)
        if row is None:
            row = next((r for r in rows if isinstance(r, dict)), None)
        return str(row.get("positive_prompt", "")).strip() if row else ""

    audits = state.get("fl2va_frame_descriptions") or []
    role_labels = {"first": "视频第一帧实际画面", "last": "视频最后一帧实际画面"}
    real_by_role = {a.get("role"): a for a in audits if isinstance(a, dict)}
    has_real = bool(real_by_role)

    if has_real:
        for role in ("first", "last"):
            audit = real_by_role.get(role)
            planned = _planned_prompt(role)
            if audit and str(audit.get("description", "")).strip():
                lines.append(f"{role_labels[role]}：{str(audit['description']).strip()}")
            elif planned:
                lines.append(f"{'视频第一帧' if role == 'first' else '视频最后一帧'}计划画面（未提供实际图片，以此为准）：{planned}")
        if lines:
            lines.insert(1, "【注意】以下为关键帧图片的实际画面描述，正文锚定以它为准；生图提示词仅作对照参考。"
                         if any(_planned_prompt(r) for r in ("first", "last")) else
                         "【注意】以下为关键帧图片的实际画面描述，正文锚定以它为准。")
        return "\n".join(lines)

    # 回退：无真实图片描述，用阶段 1 生图提示词作为计划锚定
    for frame_key, label in (("first", "首帧（视频 0.00s 的画面，正文必须以此为开场状态）"),
                             ("last", "尾帧（视频结束时的画面，正文必须落到此状态）")):
        planned = _planned_prompt(frame_key)
        if planned:
            lines.append(f"{label}：{planned}")
    return "\n".join(lines)


def _make_prompt_engineer_node(agents: dict) -> Callable:
    def _msg(state: PipelineState, brief: Brief, minimal: bool = False) -> str:
        style_note = f"风格 {brief.style}" if brief.style else "风格由 AI 根据主题确定"
        head = (
            f"请按官方规范组装最终 H3 提示词（模式 {brief.mode}"
            + (f"，变体 {brief.variant}" if brief.mode == "base" else "")
            + f"，时长 {brief.duration}s，{style_note} / 语言 {brief.language}）。"
            "【最高优先级】用户原始主题如下，人物、地点、动作必须完全忠于它，"
            "禁止引入主题中没有的新地点、新事件或新人物。\n"
            + _ctx(用户原始主题=brief.plot, 起步澄清=brief.clarifications)
        )
        variant = str(brief.variant).upper()
        fl2va_ctx = _fl2va_frame_context(state) if variant in ("FL2VA", "I2VA", "L2VA") else ""
        anchor_rule = {
            "FL2VA": "FL2VA 正文必须以首帧画面开场、经过连续变化、最终落到尾帧画面；保持同一地点/人物/服装/道具，单连续镜头。",
            "I2VA": "I2VA 正文必须从首帧画面出发向前发展，保持人物身份/服装/构图一致。",
            "L2VA": "L2VA 正文先推断合理的开场状态，逐步收敛，最终精确落到尾帧画面。",
        }.get(variant, "")
        if minimal:
            return head + "\n" + _ctx(
                关键帧锚定=fl2va_ctx,
                锚定规则=anchor_rule,
                镜头表=state.get("shot_table", ""),
                统筹美术设计=state.get("art_design", ""),
                人物设计=state.get("character_design", ""),
                背景设计=state.get("background_design", ""),
                道具设计=state.get("prop_design", ""),
                画面细化=state.get("visual_design", ""),
                对白与环境声=state.get("sound_design", ""),
                配乐=state.get("music", ""),
            )
        return head + "\n" + _ctx(
            关键帧锚定=fl2va_ctx,
            锚定规则=anchor_rule,
            镜头表=state.get("shot_table", ""),
            镜头评审锁定=state.get("shot_review_lock", ""),
            统筹美术设计=state.get("art_design", ""),
            人物设计=state.get("character_design", ""),
            背景设计=state.get("background_design", ""),
            道具设计=state.get("prop_design", ""),
            画面细化=state.get("visual_design", ""),
            对白与环境声=state.get("sound_design", ""),
            配乐=state.get("music", ""),
            subject_definitions_素材=state.get("subject_defs", "") if brief.mode == "ref" else "",
            # 编号登记 + 指称纪律（issue #41）：语义名为主、<Picture N> 格式处兜底、
            # 画面描述以生图提示词为描述主体（素材画面事实）。
            参考资产=_ref_assets_block(state, brief),
            质检问题=("\n".join(state.get("qa_issues") or []) or ""),
        )

    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        out = run_agent(agents["prompt_engineer"], _msg(state, brief))
        if not out.strip():
            # 兜底：大上下文偶发空产出，用精简上下文重试一次
            out = run_agent(agents["prompt_engineer"], _msg(state, brief, minimal=True))
        return {"final_prompt": out}
    return node


def _make_finalize_node(config: Config) -> Callable:
    def node(state: PipelineState) -> dict:
        from ..output.assembler import assemble_and_repair
        from ..tools.theme_guard import theme_fidelity_issues

        brief: Brief = state["brief"]
        repaired, report = assemble_and_repair(state.get("final_prompt", ""), brief.mode, brief.variant)
        issues = validate_prompt(
            repaired, brief.mode, duration=brief.duration, variant=brief.variant, ref_meta=state.get("ref_meta")
        )
        extra = state.get("fl2va_prompt_bundle")
        groups = ()
        if isinstance(extra, dict):
            groups = tuple(
                tuple(str(term) for term in group)
                for group in extra.get("required_scene_terms", [])
                if group
            )
        issues += theme_fidelity_issues(repaired, plot=brief.plot, mode=brief.mode, extra_groups=groups)
        return {"final_prompt": repaired, "final_report": report + "\n" + format_issues_text(issues)}
    return node


def format_issues_text(issues) -> str:
    from ..tools.h3_validator import format_issues
    return format_issues(issues)


def make_nodes(agents: dict, model, brief: Brief, config: Config) -> dict[str, Callable]:
    """构造全部节点。roundtable 节点由调用方（pipeline）注入。"""
    nodes = {
        "producer": _make_producer_node(agents),
        "director": _make_director_node(agents),
        "screenwriter": _make_screenwriter_node(agents),
        # 每个设计师只认自己那一层的设定级修订（层次见 user_revisions.SETTING_LAYERS）
        "character_designer": _make_design_node(
            agents, "character_designer", "character_design", "人物形象设计", LAYER_CHARACTER),
        "background_designer": _make_design_node(
            agents, "background_designer", "background_design", "背景设计", LAYER_ART),
        "prop_designer": _make_design_node(
            agents, "prop_designer", "prop_design", "道具设计", LAYER_ART),
        # 设计正文键走 ``ASSET_DESIGN_FIELDS``（同一份映射也被「单条重出」用）：
        # 这里手写一遍字符串，加第四类资产时路由就会去读错的设计正文。
        "image_prompt_character": _make_image_prompt_node(
            agents, "人物", ASSET_DESIGN_FIELDS["character"], "character_image_prompts"),
        "image_prompt_prop": _make_image_prompt_node(
            agents, "道具", ASSET_DESIGN_FIELDS["prop"], "prop_image_prompts"),
        "image_prompt_scene": _make_image_prompt_node(
            agents, "场景", ASSET_DESIGN_FIELDS["scene"], "scene_image_prompts"),
        "fl2va_frame_prompts": _make_fl2va_frame_prompt_node(agents),
        "art_director": _make_art_director_node(agents),
        "storyboard": _make_storyboard_node(agents),
        "cinematographer": _make_cinematographer_node(agents),
        "reference_consistency": _make_reference_consistency_node(agents),
        "sound_designer": _make_sound_designer_node(agents),
        "composer": _make_composer_node(agents),
        "prompt_engineer": _make_prompt_engineer_node(agents),
        # qa 不在图里跑：质检精修在 pipeline.run_pipeline 的 Python 循环里用 qa agent
        "finalize": _make_finalize_node(config),
    }
    return nodes


def make_creative_rt_node(rt, model, brief) -> Callable:
    return _make_roundtable_node(
        rt, "creative_lock", "锁定创意方向、情绪基调、任务类型与叙事节奏",
        lambda s: _ctx(原始剧情=brief.plot, 起步澄清=brief.clarifications,
                       制作计划=s.get("production_plan", ""), 导演阐述=s.get("director_brief", "")))


def make_shot_rt_node(rt, model, brief) -> Callable:
    return _make_roundtable_node(
        rt, "shot_review_lock", "评审镜头表的可生成性并锁定镜头方案",
        lambda s: _ctx(原始剧情=brief.plot, 起步澄清=brief.clarifications,
                       镜头表=s.get("shot_table", ""), 美术设计=s.get("art_design", ""),
                       时长=f"{s['duration']}s"))


def make_identity_rt_node(rt, model, brief) -> Callable:
    # 身份圆桌锁的是角色/场景与 `<Picture N>` 的映射，只在有参考素材的路线成立（#103 判据）。
    if not route_emits_asset_prompts(brief):
        return lambda s: {"identity_lock": ""}
    return _make_roundtable_node(
        rt, "identity_lock", "锁定角色/场景身份定义与 <Picture N> 映射",
        # 参考资产从 state 现算（issue #41）：<Picture N> 映射正是本圆桌的锁定对象，
        # 向导多参路线的登记要到资产图提示词产出后才存在，构造时抄一份就是空的。
        lambda s: _ctx(统筹美术设计=s.get("art_design", ""),
                       人物设计=s.get("character_design", ""),
                       背景设计=s.get("background_design", ""),
                       道具设计=s.get("prop_design", ""),
                       参考资产=_ref_assets_block(s, brief) or "（无）"))


def make_parallel(*nodes: Callable[[PipelineState], dict]) -> Callable[[PipelineState], dict]:
    """把多个独立节点并行执行，合并输出。

    各子节点只读 state（互不写对方需要的字段），无读后写冲突；LLM 调用是 I/O 密集，
    线程并发能接近线性提速。整图仍保持线性链（每个组合节点恰好执行一次），避免
    并行 fan-in 深度不对齐导致节点被重复触发的问题。
    """
    def node(state: PipelineState) -> dict:
        with ThreadPoolExecutor(max_workers=max(2, len(nodes))) as ex:
            futs = [ex.submit(fn, state) for fn in nodes]
            merged: dict = {}
            for f in futs:
                merged.update(f.result())
        return merged

    return node
