"""LangGraph 主管线：线性 DAG（含 3 个有界圆桌）+ Python 层有界质检精修。

v0.1 采用线性执行：并行 fan-in 深度不对齐时 LangGraph 会重复触发 fan-in 节点，
图内循环 + 并行会放大执行次数（已实测确认）。质检精修移到 Python 层：
确定性校验驱动，qa 角色给建议、prompt_engineer 修正，最多 max_qa_iterations 轮。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..agents import build_role_agents, run_agent
from ..brief_parser import FRAME_VARIANTS, Brief
from ..config import Config
from ..observability import stage_saver, token_meter
from ..output.assembler import assemble_and_repair
from ..tools.h3_validator import format_issues, validate_prompt
from ..tools.ref_metadata import to_tuple
from ..tools.theme_guard import theme_fidelity_issues, theme_requirements_text
from .nodes import (
    make_creative_rt_node,
    make_identity_rt_node,
    make_nodes,
    make_parallel,
    make_shot_rt_node,
)
from .roundtable import build_roundtable
from .state import PipelineState


def _theme_repair_injection(state: dict, brief: Brief) -> str:
    """把主题约束与关键帧锚定渲染成 QA/修复消息里的回锚指令；无约束时返回空。"""
    bundle = state.get("fl2va_prompt_bundle")
    extra = tuple(
        tuple(str(term) for term in group)
        for group in (bundle or {}).get("required_scene_terms", [])
        if group
    )
    req = theme_requirements_text(brief.plot, extra)
    anchor = str((bundle or {}).get("scene_anchor", "")).strip()
    variant = str(brief.variant).upper()
    lines = ["【主题忠实度要求】正文必须完全忠于用户原始主题，人物/地点/动作不得偏离。"]
    if req:
        lines.append(req)
    if anchor and variant in FRAME_VARIANTS:
        lines.append(f"{variant} 场景锚点（正文全程不得离开）：{anchor}")
    lines.append("【禁止无中生有】正文与画面中的每个细节必须可溯源到用户原始主题/分场剧本/镜头表/关键帧；"
                 "不得新增主题中不存在的主体、地点或道具。")
    return "\n".join(lines)

# 线性执行顺序（独立子任务在组合节点内并发，整图保持线性、每个节点恰好一次）
# 阶段 1：创意 → 设计 → 分镜 → 首尾帧生图提示词 + 声音；阶段 2：组装视频正文 + 终检
_STAGE1_CHAIN = [
    "producer",
    "director",
    "creative_rt",
    "screenwriter",
    "parallel_designers",  # character ‖ background ‖ prop designers
    "art_director",
    "storyboard",
    "parallel_decisions",  # shot_rt ‖ identity_rt
    "parallel_visual",     # cinematographer ‖ reference_consistency
    "fl2va_frame_prompts",  # FL2VA 首帧/尾帧融合生图提示词
    "parallel_sound",      # sound_designer ‖ composer
]
_STAGE2_CHAIN = [
    "prompt_engineer",
    "finalize",
]
_LINEAR_CHAIN = _STAGE1_CHAIN + _STAGE2_CHAIN

# 已折叠进并行组合节点、不再作为独立图节点的角色节点
_FOLDED_NODES = {
    "cinematographer", "reference_consistency", "sound_designer", "composer",
    "character_designer", "background_designer", "prop_designer",
    "image_prompt_character", "image_prompt_prop", "image_prompt_scene",
}

def build_pipeline_graph(model, brief: Brief, config: Config, *, stage: int = 0):
    """构建并编译主管线。stage=0 全图；1 仅阶段 1（生图提示词）；2 仅阶段 2（视频正文）。返回 (编译图, agents)。"""
    agents = build_role_agents(model, brief.refs, brief.mode, brief.duration, brief.variant)
    nodes = make_nodes(agents, model, brief, config)

    creative_rt = build_roundtable(model, ["producer", "director", "screenwriter"], config.roundtable_max_rounds)
    shot_rt = build_roundtable(model, ["storyboard", "cinematographer", "feasibility_reviewer"], config.roundtable_max_rounds)
    identity_rt = build_roundtable(model, ["art_director", "reference_consistency"], config.roundtable_max_rounds)

    if stage == 1:
        chain = _STAGE1_CHAIN
    elif stage == 2:
        chain = _STAGE2_CHAIN
    else:
        chain = _LINEAR_CHAIN

    g = StateGraph(PipelineState)
    for name in chain:
        fn = _combined_node(name, nodes, shot_rt, identity_rt, creative_rt, model, brief)
        g.add_node(name, fn)
    g.add_edge(START, chain[0])
    for a, b in zip(chain, chain[1:]):
        g.add_edge(a, b)
    g.add_edge(chain[-1], END)
    return g.compile(), agents


def _combined_node(name, nodes, shot_rt, identity_rt, creative_rt, model, brief):
    """按链位名字返回对应节点实现；组合节点与全图构建共用。"""
    if name == "creative_rt":
        return make_creative_rt_node(creative_rt, model, brief)
    if name == "parallel_designers":
        return make_parallel(
            nodes["character_designer"], nodes["background_designer"], nodes["prop_designer"],
        )
    if name == "parallel_decisions":
        return make_parallel(
            make_shot_rt_node(shot_rt, model, brief),
            make_identity_rt_node(identity_rt, model, brief),
        )
    if name == "parallel_visual":
        return make_parallel(nodes["cinematographer"], nodes["reference_consistency"])
    if name == "parallel_sound":
        return make_parallel(nodes["sound_designer"], nodes["composer"])
    return nodes[name]


def _build_stage_graph(model, brief: Brief, config: Config, chain: list[str]):
    """按显式链构建编译图；chain 必须是 _STAGE1_CHAIN/_STAGE2_CHAIN/_LINEAR_CHAIN 之一。"""
    if chain is _STAGE1_CHAIN:
        stage = 1
    elif chain is _STAGE2_CHAIN:
        stage = 2
    else:
        stage = 0
    return build_pipeline_graph(model, brief, config, stage=stage)[0]


def run_stage1(brief: Brief, config: Config, *, on_node=None) -> tuple[dict, object, object]:
    """阶段 1：创意/设计/分镜/首尾帧生图提示词/声音。返回 (state, model, agents)。

    on_node：可选回调，每个图节点跑完（有产出）时以节点名调用，供 UI 报进度。
    不组装视频正文；state 可经 session_store 落盘后中断，由 run_stage2 续跑。
    """
    from ..model_factory import build_chat_model

    model = build_chat_model()
    ref_meta = to_tuple(brief.refs)
    initial: PipelineState = {
        "brief": brief,
        "mode": brief.mode,
        "variant": brief.variant,
        "duration": brief.duration,
        "ref_meta": ref_meta,
        "iterations": 0,
        "qa_issues": [],
    }
    token_meter.reset()
    stage_saver.base.mkdir(parents=True, exist_ok=True)
    graph, agents = build_pipeline_graph(model, brief, config, stage=1)
    state: dict = dict(initial)
    for chunk in graph.stream(initial, stream_mode="updates"):
        for node_name, update in chunk.items():
            if update:
                state.update(update)
                stage_saver.save(node_name, update)
                if on_node is not None:
                    on_node(node_name)
    if config.common_sense_qa:
        state = _stage1_frame_qa_loop(state, brief, agents, model, config)
    return state, model, agents


def _stage1_frame_qa_loop(state: dict, brief: Brief, agents, model, config: Config) -> dict:
    """生图提示词有界常识 QA：发现 error 级问题注入 frame_prompt_issues 重生成，最多 max_qa_iterations 轮。"""
    from ..tools.frame_sanity import frame_common_sense_issues
    from ..tools.h3_validator import format_issues
    from .nodes import make_nodes

    for i in range(config.max_qa_iterations):
        bundle_dict = state.get("fl2va_prompt_bundle")
        if not isinstance(bundle_dict, dict):
            break
        issues = frame_common_sense_issues(bundle_dict, brief, agents)
        errors = [it for it in issues if it.severity == "error"]
        if not errors:
            break
        state["frame_prompt_issues"] = [it.message for it in errors]
        nodes = make_nodes(agents, model, brief, config)
        update = nodes["fl2va_frame_prompts"](state)
        state.pop("frame_prompt_issues", None)
        if isinstance(update, dict):
            state.update(update)
        stage_saver.save(f"frame_qa_{i}", {
            "常识问题": format_issues(errors),
            "重出提示词": state.get("fl2va_prompt_bundle"),
        })
    return state


def run_stage2(state: dict, brief: Brief, config: Config, *, model=None, on_node=None, checkpoint_dir=None) -> tuple[dict, str]:
    """阶段 2：注入真实帧描述后组装视频正文 + 有界 QA/theme_guard 精修。返回 (state, 最终提示词)。

    on_node：可选回调，每个图节点跑完（有产出）时以节点名调用，供 UI 报进度。
    checkpoint_dir：可选断点目录。若已有装组初稿断点，跳过组装直接进 QA 精修；
    组装完成后立刻落盘，质检每轮也更新，保证中途崩溃只重跑当前质检轮。
    """
    from ..model_factory import build_chat_model
    from ..stage2_checkpoint import clear_checkpoint, load_checkpoint, save_checkpoint

    model = model or build_chat_model()
    ref_meta = to_tuple(brief.refs)
    # QA 精修循环需要 qa / prompt_engineer agent，即便是断点续跑也要先构建
    agents = build_role_agents(model, brief.refs, brief.mode, brief.duration, brief.variant)

    checkpoint = load_checkpoint(checkpoint_dir) if checkpoint_dir else None
    if checkpoint is not None:
        merged = {k: v for k, v in state.items() if k not in ("final_prompt", "final_report")}
        prompt = str(checkpoint["prompt_draft"])
        print("[续接] 检测到阶段 2 断点（组装已于此前完成），直接进入质检精修。", flush=True)
    else:
        graph, _ = build_pipeline_graph(model, brief, config, stage=2)
        initial: PipelineState = {
            "brief": brief,
            "mode": brief.mode,
            "variant": brief.variant,
            "duration": brief.duration,
            "ref_meta": ref_meta,
            "iterations": 0,
            **{k: v for k, v in state.items() if k not in ("final_prompt", "final_report", "brief")},
        }
        merged: dict = dict(initial)
        for chunk in graph.stream(initial, stream_mode="updates"):
            for node_name, update in chunk.items():
                if update:
                    merged.update(update)
                    stage_saver.save(node_name, update)
                    if on_node is not None:
                        on_node(node_name)

        prompt = merged.get("final_prompt", "")
        prompt, _ = assemble_and_repair(prompt, brief.mode, brief.variant)
        if checkpoint_dir:
            save_checkpoint(checkpoint_dir, prompt_draft=prompt)

    # 有界质检精修：确定性校验 → 有 error 则 qa 给建议 + prompt_engineer 修正
    for i in range(config.max_qa_iterations):
        issues = validate_prompt(prompt, brief.mode, duration=brief.duration,
                                 variant=brief.variant, ref_meta=ref_meta)
        theme_issues = theme_fidelity_issues(
            prompt,
            plot=brief.plot,
            mode=brief.mode,
            extra_groups=(merged.get("fl2va_prompt_bundle") or {}).get("required_scene_terms", []),
        )
        issues = issues + theme_issues
        if config.common_sense_qa:
            from ..tools.frame_sanity import prompt_common_sense_issues

            issues = issues + prompt_common_sense_issues(prompt, brief, agents)
        errors = [i for i in issues if i.severity == "error"]
        if not errors:
            break
        rewire = _theme_repair_injection(merged, brief)
        review = run_agent(
            agents["qa"],
            f"质检以下 H3 提示词并给出修正建议：\n{prompt}\n\n{rewire}\n校验问题：\n{format_issues(issues)}",
        )
        prompt = run_agent(
            agents["prompt_engineer"],
            f"以下 H3 提示词有格式或主题忠实度问题，请修正后重新输出完整提示词。\n"
            f"【模式】{brief.mode} / 时长 {brief.duration}s / 变体 {brief.variant}\n"
            f"{rewire}\n"
            f"【当前提示词】\n{prompt}\n"
            f"【质检建议】\n{review}\n"
            f"【问题清单】\n{format_issues(issues)}",
        )
        prompt, _ = assemble_and_repair(prompt, brief.mode, brief.variant)
        stage_saver.save(f"qa_refine_{i}", {"质检建议": review, "重出提示词": prompt, "主题忠实度校验": format_issues(theme_issues)})
        if checkpoint_dir:
            save_checkpoint(checkpoint_dir, prompt_draft=prompt, status=f"qa_round_{i + 1}")

    prompt, _ = assemble_and_repair(prompt, brief.mode, brief.variant)
    merged["final_prompt"] = prompt
    if checkpoint_dir:
        clear_checkpoint(checkpoint_dir)
    return merged, prompt


def _execute_pipeline(brief: Brief, config: Config, *, frame_descriptions: list[dict] | None = None) -> tuple[dict, object]:
    """执行完整管线（阶段 1+2 连续拼接）并返回最终 state 与 brief。"""
    state, model, _agents = run_stage1(brief, config)
    if frame_descriptions:
        state["fl2va_frame_descriptions"] = list(frame_descriptions)
    state, prompt = run_stage2(state, brief, config, model=model)
    state["final_prompt"] = prompt
    return state, brief


def run_pipeline_structured(
    brief: Brief,
    config: Config,
    *,
    generation_id: str = "GEN001",
    topic_id: str = "",
    project_id: str = "",
    frame_descriptions: list[dict] | None = None,
):
    """运行管线并返回剧本、三类生图提示词和视频 H3 提示词。

    frame_descriptions：真实关键帧图片的读图描述（可选；由调用方先 audit_frame_images 得到）。
    """
    from ..generation import result_from_state

    state, brief = _execute_pipeline(brief, config, frame_descriptions=frame_descriptions)
    return result_from_state(
        state, brief, generation_id=generation_id,
        topic_id=topic_id, project_id=project_id,
    )


def run_pipeline(brief: Brief, config: Config) -> str:
    """端到端跑一遍，保持旧接口并只返回最终 H3 提示词。"""
    state, _ = _execute_pipeline(brief, config)
    return str(state.get("final_prompt", ""))
