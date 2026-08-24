"""LangGraph 主管线：线性 DAG（含 3 个有界圆桌）+ Python 层有界质检精修。

v0.1 采用线性执行：并行 fan-in 深度不对齐时 LangGraph 会重复触发 fan-in 节点，
图内循环 + 并行会放大执行次数（已实测确认）。质检精修移到 Python 层：
确定性校验驱动，qa 角色给建议、prompt_engineer 修正，最多 max_qa_iterations 轮。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..agents import build_role_agents, run_agent
from ..brief_parser import Brief
from ..config import Config
from ..observability import stage_saver, token_meter
from ..output.assembler import assemble_and_repair
from ..tools.h3_validator import format_issues, validate_prompt
from ..tools.ref_metadata import to_tuple
from .nodes import (
    make_creative_rt_node,
    make_identity_rt_node,
    make_nodes,
    make_parallel,
    make_shot_rt_node,
)
from .roundtable import build_roundtable
from .state import PipelineState

# 线性执行顺序（独立子任务在组合节点内并发，整图保持线性、每个节点恰好一次）
_LINEAR_CHAIN = [
    "producer",
    "director",
    "creative_rt",
    "screenwriter",
    "parallel_designers",  # character ‖ background ‖ prop designers
    "art_director",
    "parallel_image_prompts",  # character/prop/scene → Z-Image + Flux.2
    "storyboard",
    "parallel_decisions",  # shot_rt ‖ identity_rt
    "parallel_visual",     # cinematographer ‖ reference_consistency
    "fl2va_frame_prompts",  # FL2VA 首帧/尾帧融合生图提示词
    "parallel_sound",      # sound_designer ‖ composer
    "prompt_engineer",
    "finalize",
]

# 已折叠进并行组合节点、不再作为独立图节点的角色节点
_FOLDED_NODES = {
    "cinematographer", "reference_consistency", "sound_designer", "composer",
    "character_designer", "background_designer", "prop_designer",
    "image_prompt_character", "image_prompt_prop", "image_prompt_scene",
}


def build_pipeline_graph(model, brief: Brief, config: Config):
    """构建并编译主管线。返回 (编译图, agents)。"""
    agents = build_role_agents(model, brief.refs, brief.mode, brief.duration, brief.variant)
    nodes = make_nodes(agents, model, brief, config)

    creative_rt = build_roundtable(model, ["producer", "director", "screenwriter"], config.roundtable_max_rounds)
    shot_rt = build_roundtable(model, ["storyboard", "cinematographer", "feasibility_reviewer"], config.roundtable_max_rounds)
    identity_rt = build_roundtable(model, ["art_director", "reference_consistency"], config.roundtable_max_rounds)

    g = StateGraph(PipelineState)
    for name, fn in nodes.items():
        if name not in _FOLDED_NODES:
            g.add_node(name, fn)
    g.add_node("creative_rt", make_creative_rt_node(creative_rt, model, brief))
    g.add_node("parallel_designers", make_parallel(
        nodes["character_designer"], nodes["background_designer"], nodes["prop_designer"],
    ))
    g.add_node("parallel_image_prompts", make_parallel(
        nodes["image_prompt_character"], nodes["image_prompt_prop"], nodes["image_prompt_scene"],
    ))
    g.add_node("parallel_decisions", make_parallel(
        make_shot_rt_node(shot_rt, model, brief),
        make_identity_rt_node(identity_rt, model, brief),
    ))
    g.add_node("parallel_visual", make_parallel(
        nodes["cinematographer"], nodes["reference_consistency"],
    ))
    g.add_node("parallel_sound", make_parallel(
        nodes["sound_designer"], nodes["composer"],
    ))

    g.add_edge(START, _LINEAR_CHAIN[0])
    for a, b in zip(_LINEAR_CHAIN, _LINEAR_CHAIN[1:]):
        g.add_edge(a, b)
    g.add_edge(_LINEAR_CHAIN[-1], END)
    return g.compile(), agents


def _execute_pipeline(brief: Brief, config: Config) -> tuple[dict, object]:
    """执行完整管线并返回最终 state 与 brief；项目层不应直接进入图内部。"""
    from ..model_factory import build_chat_model

    model = build_chat_model()
    graph, agents = build_pipeline_graph(model, brief, config)
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

    # 流式驱动：逐节点落盘产物（图内节点恰好执行一次）
    state: dict = dict(initial)
    for chunk in graph.stream(initial, stream_mode="updates"):
        for node_name, update in chunk.items():
            if update:
                state.update(update)
                stage_saver.save(node_name, update)

    prompt = state.get("final_prompt", "")

    # 有界质检精修：确定性校验 → 有 error 则 qa 给建议 + prompt_engineer 修正
    for i in range(config.max_qa_iterations):
        issues = validate_prompt(prompt, brief.mode, duration=brief.duration,
                                 variant=brief.variant, ref_meta=ref_meta)
        errors = [i for i in issues if i.severity == "error"]
        if not errors:
            break
        review = run_agent(
            agents["qa"],
            f"质检以下 H3 提示词并给出修正建议：\n{prompt}\n\n校验问题：\n{format_issues(issues)}",
        )
        prompt = run_agent(
            agents["prompt_engineer"],
            f"以下 H3 提示词有格式问题，请修正后重新输出完整提示词。\n"
            f"【模式】{brief.mode} / 时长 {brief.duration}s / 变体 {brief.variant}\n"
            f"【当前提示词】\n{prompt}\n"
            f"【质检建议】\n{review}\n"
            f"【问题清单】\n{format_issues(issues)}",
        )
        prompt, _ = assemble_and_repair(prompt, brief.mode, brief.variant)
        stage_saver.save(f"qa_refine_{i}", {"质检建议": review, "重出提示词": prompt})

    prompt, _ = assemble_and_repair(prompt, brief.mode, brief.variant)
    state["final_prompt"] = prompt
    return state, brief


def run_pipeline_structured(
    brief: Brief,
    config: Config,
    *,
    generation_id: str = "GEN001",
    topic_id: str = "",
    project_id: str = "",
):
    """运行管线并返回剧本、三类生图提示词和视频 H3 提示词。"""
    from ..generation import result_from_state

    state, brief = _execute_pipeline(brief, config)
    return result_from_state(
        state, brief, generation_id=generation_id,
        topic_id=topic_id, project_id=project_id,
    )


def run_pipeline(brief: Brief, config: Config) -> str:
    """端到端跑一遍，保持旧接口并只返回最终 H3 提示词。"""
    state, _ = _execute_pipeline(brief, config)
    return str(state.get("final_prompt", ""))
