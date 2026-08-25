"""LangGraph 节点工厂：把角色 agent 包装成节点，构造各节点所需的用户消息。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from langgraph.graph.state import CompiledStateGraph

from ..agents import run_agent
from ..brief_parser import Brief, brief_uses_refs
from ..config import Config
from ..generation import fl2va_bundle_from_dict
from ..tools.h3_validator import validate_prompt
from ..tools.ref_metadata import format_ref_meta
from .state import PipelineState


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
        参考资产=refs if brief_uses_refs(brief) else "",
        草稿=brief.draft or "",
    )


def _make_producer_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        return {"production_plan": run_agent(agents["producer"], f"请给出制作计划：\n{_brief_block(brief)}")}
    return node


def _make_director_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = f"请给出导演阐述：\n{_ctx(原始剧情=brief.plot, 制作计划=state.get('production_plan', ''))}"
        return {"director_brief": run_agent(agents["director"], msg)}
    return node


def _make_screenwriter_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = (
            "请写出分场剧本：\n"
            + _ctx(
                原始剧情=brief.plot,
                导演阐述=state.get("director_brief", ""),
                创意锁定=state.get("creative_lock", ""),
            )
        )
        return {"script": run_agent(agents["screenwriter"], msg)}
    return node


def _design_context(state: PipelineState, brief: Brief) -> str:
    """为三类生图设计师提供同一份剧情约束，避免脱离故事自由发挥。"""
    refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
    return _ctx(
        原始剧情=brief.plot,
        导演阐述=state.get("director_brief", ""),
        创意锁定=state.get("creative_lock", ""),
        分场剧本=state.get("script", ""),
        风格=brief.style,
        参考资产=refs if brief_uses_refs(brief) else "",
    )


def _make_design_node(agents: dict, role: str, output_field: str, label: str) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        msg = f"请完成{label}，必须严格契合视频剧情：\n{_design_context(state, brief)}"
        return {output_field: run_agent(agents[role], msg)}
    return node


def _make_image_prompt_node(agents: dict, kind: str, design_field: str, output_field: str) -> Callable:
    """把模型无关设计资料翻译为 Z-Image/Flux.2 双版本提示词。"""
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        message = (
            f"请为{kind}生成 Z-Image 和 Flux.2 两个可直接复制的生图提示词。只输出 JSON。\n"
            + _ctx(
                原始剧情=brief.plot,
                分场剧本=state.get("script", ""),
                对应设计=state.get(design_field, ""),
                美术统筹=state.get("art_design", ""),
                视觉风格=brief.style,
                语言=brief.language,
            )
        )
        raw = run_agent(agents["image_prompt_engineer"], message)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            repair = run_agent(agents["image_prompt_engineer"], f"把以下内容转换为严格 JSON，不要 markdown：\n{raw}")
            try:
                parsed = json.loads(repair)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{kind} 生图提示词不是有效 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{kind} 生图提示词 JSON 顶层必须是对象")
        return {output_field: parsed}
    return node


def _make_fl2va_frame_prompt_node(agents: dict) -> Callable:
    """把完整镜头上下文转换为 FL2VA 融合首帧/尾帧提示词。"""
    def parse_bundle(raw: str, brief: Brief) -> dict:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            repair = run_agent(
                agents["frame_prompt_engineer"],
                f"把以下内容转换为严格 JSON，不要 markdown，必须保留 first、last、scene_anchor 和 continuity_constraints：\n{raw}",
            )
            try:
                parsed = json.loads(repair)
            except json.JSONDecodeError as exc:
                raise ValueError("FL2VA 首尾帧提示词不是有效 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("FL2VA 首尾帧提示词 JSON 顶层必须是对象")
        bundle = fl2va_bundle_from_dict(parsed, brief)
        return bundle.to_dict()

    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        if brief.variant.upper() != "FL2VA":
            return {}
        message = (
            "请为这一段 FL2VA 视频生成融合的首帧和尾帧静态生图提示词。"
            "人物、道具和场景必须同时出现在每一张关键帧中；主题地点是硬约束。只输出 JSON。\n"
            + _ctx(
                原始主题=brief.plot,
                时长=f"{brief.duration}s",
                视觉风格=brief.style,
                语言=brief.language,
                分场剧本=state.get("script", ""),
                人物设计=state.get("character_design", ""),
                道具设计=state.get("prop_design", ""),
                背景设计=state.get("background_design", ""),
                美术统筹=state.get("art_design", ""),
                分镜表=state.get("shot_table", ""),
                镜头评审锁定=state.get("shot_review_lock", ""),
                画面细化=state.get("visual_design", ""),
                身份一致性锁定=state.get("identity_lock", ""),
            )
        )
        raw = run_agent(agents["frame_prompt_engineer"], message)
        try:
            bundle = parse_bundle(raw, brief)
        except ValueError as first_error:
            repair = run_agent(
                agents["frame_prompt_engineer"],
                f"重写以下 FL2VA JSON，使其通过主题地点和首尾帧连续性校验。不要 markdown。\n"
                f"主题：{brief.plot}\n错误：{first_error}\n原始结果：{raw}",
            )
            try:
                bundle = parse_bundle(repair, brief)
            except ValueError as exc:
                raise ValueError(f"FL2VA 首尾帧提示词生成失败：{exc}") from exc
        return {"fl2va_prompt_bundle": bundle}

    return node


def _make_art_director_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
        msg = "请统筹并裁决最终美术设计：\n" + _ctx(
            原始剧情=brief.plot,
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


def _make_storyboard_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        issues = "\n".join(state.get("qa_issues") or []) or ""
        msg = (
            "请给出分镜镜头表（时长 "
            + f"{brief.duration}s）：\n"
            + _ctx(原始剧情=brief.plot,
                   分场剧本=state.get("script", ""), 美术设计=state.get("art_design", ""),
                   人物设计=state.get("character_design", ""),
                   背景设计=state.get("background_design", ""),
                   道具设计=state.get("prop_design", ""),
                   上一轮质检问题=issues)
        )
        return {"shot_table": run_agent(agents["storyboard"], msg)}
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
            镜头表=state.get("shot_table", ""),
            镜头评审锁定=state.get("shot_review_lock", ""),
        )
        return {"visual_design": run_agent(agents["cinematographer"], msg)}
    return node


def _make_reference_consistency_node(agents: dict) -> Callable:
    def node(state: PipelineState) -> dict:
        brief: Brief = state["brief"]
        if brief.mode != "ref":
            return {"subject_defs": ""}  # base 模式不需要 subject 定义，跳过
        refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
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
            + _ctx(用户原始主题=brief.plot)
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
            参考资产=format_ref_meta(brief.refs) if (brief_uses_refs(brief) and brief.refs) else "",
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
        "character_designer": _make_design_node(agents, "character_designer", "character_design", "人物形象设计"),
        "background_designer": _make_design_node(agents, "background_designer", "background_design", "背景设计"),
        "prop_designer": _make_design_node(agents, "prop_designer", "prop_design", "道具设计"),
        "image_prompt_character": _make_image_prompt_node(agents, "人物", "character_design", "character_image_prompts"),
        "image_prompt_prop": _make_image_prompt_node(agents, "道具", "prop_design", "prop_image_prompts"),
        "image_prompt_scene": _make_image_prompt_node(agents, "场景", "background_design", "scene_image_prompts"),
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
        lambda s: _ctx(原始剧情=brief.plot,
                       制作计划=s.get("production_plan", ""), 导演阐述=s.get("director_brief", "")))


def make_shot_rt_node(rt, model, brief) -> Callable:
    return _make_roundtable_node(
        rt, "shot_review_lock", "评审镜头表的可生成性并锁定镜头方案",
        lambda s: _ctx(原始剧情=brief.plot,
                       镜头表=s.get("shot_table", ""), 美术设计=s.get("art_design", ""),
                       时长=f"{s['duration']}s"))


def make_identity_rt_node(rt, model, brief) -> Callable:
    if brief.mode != "ref":
        return lambda s: {"identity_lock": ""}  # base 模式不需要身份锁定
    refs = format_ref_meta(brief.refs) if brief.refs else "（无）"
    return _make_roundtable_node(
        rt, "identity_lock", "锁定角色/场景身份定义与 <Picture N> 映射",
        lambda s: _ctx(统筹美术设计=s.get("art_design", ""),
                       人物设计=s.get("character_design", ""),
                       背景设计=s.get("background_design", ""),
                       道具设计=s.get("prop_design", ""), 参考资产=refs))


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
