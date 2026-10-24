"""LangGraph 主管线：线性 DAG（含 3 个有界圆桌）+ Python 层有界质检精修。

v0.1 采用线性执行：并行 fan-in 深度不对齐时 LangGraph 会重复触发 fan-in 节点，
图内循环 + 并行会放大执行次数（已实测确认）。质检精修移到 Python 层：
确定性校验驱动，qa 角色给建议、prompt_engineer 修正，最多 max_qa_iterations 轮。
"""
from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from ..agents import build_role_agents, run_agent
from ..brief_parser import FRAME_VARIANTS, Brief, is_ref_mode
from ..clarification import as_block
from ..config import Config
from ..observability import format_step_banner, stage_saver, token_meter
from ..output.assembler import assemble_and_repair
from ..tools.h3_validator import format_issues, validate_prompt
from ..tools.ref_metadata import to_tuple
from ..tools.theme_guard import theme_fidelity_issues, theme_requirements_text
from ..user_revisions import FRAME_BASELINE_KEY, LAYER_SPECS, LayerSpec
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
    # 起步澄清与主题并列，是**合法来源**：少了这条，质检精修会把澄清产出的细节当成
    # 「无中生有」删掉——用户说出口的要求被机器的修复轮静默撤回（issue #29）。
    clarify = as_block(brief.clarifications)
    if clarify:
        lines.append(clarify)
    lines.append("【禁止无中生有】正文与画面中的每个细节必须可溯源到用户原始主题/起步澄清/分场剧本/镜头表/关键帧；"
                 "不得新增这些来源里不存在的主体、地点或道具。")
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
    "parallel_image_prompts",  # 人物 ‖ 道具 ‖ 场景 资产图生图提示词（issue #36）
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

# 组合节点 → 它折叠的子节点名（``make_nodes`` 里的键）。**折叠这件事只声明在这里一处**：
# ``_combined_node`` 据此组装并行组合、``_FOLDED_NODES`` 由它现推。理由是这张清单曾经**说谎**：
# 三个资产图节点写在 ``_FOLDED_NODES`` 里、注释声称「已折叠进并行组合节点」，而没有任何组合
# 节点返回它们——清单与注释都是手写的第二份真相，谁也没跟着谁改（issue #36，本仓「规则在、
# 接线不在」的第 8 次）。现推之后，「清单说折叠了」与「真的有组合节点跑它」不可能再分家。
#
# ``parallel_decisions`` 不在表里：它折的是**两场圆桌**（``make_shot_rt_node`` /
# ``make_identity_rt_node``），不是 ``nodes`` 表里的角色节点，故它在 ``_combined_node`` 里单列。
_PARALLEL_GROUPS: dict[str, tuple[str, ...]] = {
    "parallel_designers": ("character_designer", "background_designer", "prop_designer"),
    "parallel_image_prompts": ("image_prompt_character", "image_prompt_prop", "image_prompt_scene"),
    "parallel_visual": ("cinematographer", "reference_consistency"),
    "parallel_sound": ("sound_designer", "composer"),
}

# 已折叠进并行组合节点、不再作为独立图节点的角色节点（由上面那张表现推）。
_FOLDED_NODES = frozenset(
    name for group in _PARALLEL_GROUPS.values() for name in group
)

def build_pipeline_graph(model, brief: Brief, config: Config, *, stage: int = 0,
                         chain: list[str] | None = None,
                         refs: list | None = None):
    """构建并编译主管线。stage=0 全图；1 仅阶段 1；2 仅阶段 2。
    chain：可选显式指定节点列表（用于 resume_from_node 截断链）；不传则按 stage 推导。
    refs：可选参考素材登记（issue #41 复审 F2）——图内 agent 的 ``validate_h3_prompt``
    工具在装配期把 ref_meta 封进闭包，向导多参路线 brief.refs 为空时必须由调用方把
    N 资产登记传进来，否则工具层的 N 张覆盖式校验在装配期全盲（外层 QA 轮才查得出）。
    不传则沿用 brief.refs（帧路线与 brief 文件流程的既有行为）。
    返回 (编译图, agents)。"""
    agents = build_role_agents(
        model, brief.refs if refs is None else refs,
        brief.mode, brief.duration, brief.variant,
    )
    nodes = make_nodes(agents, model, brief, config)

    creative_rt = build_roundtable(model, ["producer", "director", "screenwriter"], config.roundtable_max_rounds)
    shot_rt = build_roundtable(model, ["storyboard", "cinematographer", "feasibility_reviewer"], config.roundtable_max_rounds)
    identity_rt = build_roundtable(model, ["art_director", "reference_consistency"], config.roundtable_max_rounds)

    if chain is None:
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
    """按链位名字返回对应节点实现；组合节点与全图构建共用。

    并行组合按 ``_PARALLEL_GROUPS`` 现推（折叠声明只有那一处）；只有圆桌组合与普通节点
    在这里单列。链位名写错（比如漏了某组的键）会在构建时就取不到实现，不会静默少跑一个
    节点——这正是本仓「规则在、接线不在」要防的形态。
    """
    if name == "creative_rt":
        return make_creative_rt_node(creative_rt, model, brief)
    if name == "parallel_decisions":
        return make_parallel(
            make_shot_rt_node(shot_rt, model, brief),
            make_identity_rt_node(identity_rt, model, brief),
        )
    group = _PARALLEL_GROUPS.get(name)
    if group is not None:
        return make_parallel(*(nodes[sub] for sub in group))
    return nodes[name]


@dataclass(frozen=True)
class Stage1RerunPlan:
    """设定级重跑的落点：哪一层的、从哪个节点截断、将重跑几步、哪些产物会被替换。

    确认门照它念（issue #28）：「将重跑 N 步 ＋ 下列产物会被替换」——被替换的是**用户
    已经看过**的产物，且 LLM 生成不可复现同一版本，所以这份清单是用户判断值不值得跑的
    唯一依据。

    带上 ``spec`` 而不是只带它的字符串值：屏幕上要念的层次名（``spec.label``）与「回灌什么」
    都在那份描述里，调用方手上就不必再并排多拿一个同源的参数。
    """

    spec: LayerSpec
    start: str
    nodes: tuple[str, ...]
    artifacts: tuple[str, ...]


# 链上节点产出的产物（人读标签）。**只列可能出现在重跑计划里的节点**：最早的设定级起点是
# 编剧，之前的制作人/导演/创意圆桌永远不在截断链里，给它们编标签就又是一条没人读的接线。
# 有测试核对「重跑计划能碰到的节点」与这张表逐一对应——链上加了节点而表没加，确认门会
# **少报**一件被替换的产物，而用户是凭这份清单决定要不要跑的。
_NODE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "screenwriter": ("分场剧本",),
    "parallel_designers": ("人物形象设计", "背景设计", "道具设计"),
    "art_director": ("美术统筹",),
    # 三类**资产图**的生图提示词（人物/道具/场景），与「关键帧生图提示词」是两件事：
    # 前者是拿去生成资产参考图的，后者是视频首尾帧（人物/道具/场景已融合在一张里）。
    "parallel_image_prompts": ("人物资产图提示词", "道具资产图提示词", "场景资产图提示词"),
    "storyboard": ("分镜表",),
    "parallel_decisions": ("镜头评审锁定", "身份一致性锁定"),
    "parallel_visual": ("画面细化", "参考一致性素材"),
    "fl2va_frame_prompts": ("关键帧生图提示词（首/尾帧）",),
    "parallel_sound": ("对白与环境声", "配乐"),
}

# base 模式下这些产物根本不产出：身份评审与参考一致性节点直接返回空串
# （nodes.make_identity_rt_node / _make_reference_consistency_node）；三件资产图提示词
# 是同一形态的路线专属产物——首尾帧把人物/道具/场景**融合**进一张关键帧，没有「每件资产
# 单独出图」这一步，资产图节点直接早退（issue #78 / 父 spec #77）。把它们列进
# 「会被替换」就是对用户说谎——那是不存在、也就无所谓被替换的产物。
_REF_ONLY_ARTIFACTS = frozenset({
    "身份一致性锁定", "参考一致性素材",
    "人物资产图提示词", "道具资产图提示词", "场景资产图提示词",
})


def rerun_silent_nodes(mode: str = "base") -> frozenset[str]:
    """该路线下不会产出任何产物的环节：产物标签**全都**落在「仅多参」集合里。

    确认门用它扣掉「将重跑 N 步」与环节名（issue #81）：资产图节点在首尾帧路线早退、
    返回空更新，``run_stage1`` 只在 update 非空时回调，所以进度行不出现；确认门若照
    ``plan.nodes`` 全切片念，就比用户实际看到的环节多一步，而这句话正是他判断代价的依据。

    从 ``_NODE_ARTIFACTS`` 现推、调用时现算：链上增删节点或调整「仅多参」集合时自动跟随。
    另手写一份节点名清单就会像本仓既往那样——链条变了它不报错，只是悄悄说错。
    """
    if is_ref_mode(mode):
        return frozenset()
    return frozenset(
        name
        for name, labels in _NODE_ARTIFACTS.items()
        if labels and all(label in _REF_ONLY_ARTIFACTS for label in labels)
    )


def stage1_rerun_plan(layer: str, *, mode: str = "base") -> Stage1RerunPlan:
    """设定级层次 → 截断重跑计划（起点、步数、会被替换的产物）。

    起点取自 ``user_revisions.LAYER_SPECS``（层次＝重跑起点，只有一张表）；「跑几步」与
    「替换哪些产物」都从链上现推，不另手写——手写的清单在链变了之后不报错，只会让确认门
    少报一件产物。
    """
    spec = LAYER_SPECS.get(str(layer))
    if spec is None:
        raise ValueError(f"不是设定级层次：{layer!r}")
    if spec.resume_from not in _STAGE1_CHAIN:
        raise ValueError(f"层次的起点不在阶段 1 链上：{spec.resume_from}")
    nodes = tuple(_STAGE1_CHAIN[_STAGE1_CHAIN.index(spec.resume_from):])
    artifacts = tuple(
        label
        for name in nodes
        for label in _NODE_ARTIFACTS.get(name, ())
        if is_ref_mode(mode) or label not in _REF_ONLY_ARTIFACTS
    )
    return Stage1RerunPlan(spec=spec, start=spec.resume_from, nodes=nodes, artifacts=artifacts)


def _build_stage_graph(model, brief: Brief, config: Config, chain: list[str]):
    """按显式链构建编译图；chain 必须是 _STAGE1_CHAIN/_STAGE2_CHAIN/_LINEAR_CHAIN 之一。"""
    if chain is _STAGE1_CHAIN:
        stage = 1
    elif chain is _STAGE2_CHAIN:
        stage = 2
    else:
        stage = 0
    return build_pipeline_graph(model, brief, config, stage=stage)[0]


def run_stage1(brief: Brief, config: Config, *, on_node=None,
               resume_from_node: str | None = None,
               initial_state: dict | None = None,
               on_step_done=None) -> tuple[dict, object, object]:
    """阶段 1：创意/设计/分镜/首尾帧生图提示词/声音。返回 (state, model, agents)。

    on_node：可选回调，每个图节点跑完（有产出）时以节点名调用，供 UI 报进度。
    resume_from_node：从某个节点继续阶段 1（之前的产物由 initial_state 传入），用于中断续跑。
    initial_state：续跑时预填的 state（通常是上次中断时已落盘的 stage_state）。
    on_step_done：可选回调，每次节点完成后立刻以 (node_name, current_state) 调用，
    供调用方增量落盘（防中断丢进度）。
    不组装视频正文；state 可经 session_store 落盘后中断，由 run_stage2 续跑。
    """
    from ..model_factory import build_chat_model

    model = build_chat_model()
    ref_meta = to_tuple(brief.refs)

    # 确定从哪个节点开始跑；中断续跑时跳过已完成节点
    chain = list(_STAGE1_CHAIN)
    if resume_from_node:
        if resume_from_node not in chain:
            raise ValueError(f"resume_from_node 不在阶段 1 链中：{resume_from_node}")
        chain = chain[chain.index(resume_from_node):]

    base_state: dict = dict(initial_state or {})
    initial: PipelineState = {
        "brief": brief,
        "mode": brief.mode,
        "variant": brief.variant,
        "duration": brief.duration,
        "ref_meta": ref_meta,
        "iterations": 0,
        "qa_issues": [],
        **base_state,  # 续跑时的已有产物
    }
    # 这里**不清零**计量表（issue #52）：阶段入口清零＝进阶段 2 就把阶段 1 的成本抹掉，
    # 而「跑完的成本」正是指整条流程的累计。账的边界是**一份生成**（换新生成由向导开新账），
    # 不是阶段；续跑进程则先在续跑入口把上一次的成本表接回来（``TokenMeter.restore``）。
    stage_saver.base.mkdir(parents=True, exist_ok=True)
    graph, agents = build_pipeline_graph(model, brief, config, stage=1, chain=chain)
    state: dict = dict(initial)
    for chunk in graph.stream(initial, stream_mode="updates"):
        for node_name, update in chunk.items():
            if update:
                state.update(update)
                stage_saver.save(node_name, update)
                if on_node is not None:
                    on_node(node_name)
                if on_step_done is not None:
                    on_step_done(node_name, dict(state))
    if config.common_sense_qa:
        state = _stage1_frame_qa_loop(state, brief, agents, model, config)
    # 成本表**不写进 state**（issue #52 复审按 ADR 0005 接线纪律核过）：它没有产品级
    # 读者——落盘由 `session_store.save` 取活账（那份 state 副本会被覆盖），于是写进来
    # 只是多一份没人读的真相。它的产出物是会话文件与阶段 2 断点，两处都直接取活账。
    return state, model, agents


def _stage1_frame_qa_loop(state: dict, brief: Brief, agents, model, config: Config) -> dict:
    """生图提示词有界常识 QA：发现 error 级问题注入 frame_prompt_issues 重生成，最多 max_qa_iterations 轮。"""
    from ..tools.frame_sanity import frame_common_sense_issues
    from ..tools.h3_validator import format_issues
    from .nodes import make_nodes

    # 修订基线是**人的累积**那条循环的入参（ADR 0005 / issue #25）。本循环对当前产物重算，
    # 语义是**替换**：万一上游把它递进来（旧 state、手工改过的会话、续接），当场摘掉——
    # 替换语义不该因为 state 里多了一个键就滑成「以用户上一版为基线」。
    state.pop(FRAME_BASELINE_KEY, None)
    for i in range(config.max_qa_iterations):
        # 横幅回答「在干什么、第几轮」（ADR 0019 决定 3 / 复审 S2）：这个循环原先
        # 只在事件总线上有动静而没人订阅，终端上就是一段没有任何输出的长等待。
        print(format_step_banner("正在质检精修", round_no=i + 1,
                                 total_rounds=config.max_qa_iterations), flush=True)
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
    from ..generation import ref_asset_registry
    from ..model_factory import build_chat_model
    from ..stage2_checkpoint import clear_checkpoint, load_checkpoint, save_checkpoint

    model = model or build_chat_model()
    # N 张素材登记（issue #41）：多参路线的 ref_meta 来自登记（编号按剧本出场顺序），
    # 不是 brief.refs——向导路线的 brief 没有参考图段，登记是链上资产图提示词的衍生物。
    # brief.refs 非空时登记以用户声明为准，行为与旧版逐字一致。
    registry = ref_asset_registry(state, brief)
    ref_meta = to_tuple(registry)
    # QA 精修循环需要 qa / prompt_engineer agent，即便是断点续跑也要先构建
    agents = build_role_agents(model, registry, brief.mode, brief.duration, brief.variant)

    checkpoint = load_checkpoint(checkpoint_dir) if checkpoint_dir else None
    if checkpoint is not None:
        merged = {k: v for k, v in state.items() if k not in ("final_prompt", "final_report")}
        merged["ref_meta"] = ref_meta
        prompt = str(checkpoint["prompt_draft"])
        # 断点里带着组装那一刻的成本表（issue #52）：组装的钱花在上一个进程里，而它
        # 跑完只落这个断点——不接回来，跳过组装的那次续跑就永远少记这一笔。
        token_meter.restore(checkpoint.get("cost_table"))
        print("[续接] 检测到阶段 2 断点（组装已于此前完成），直接进入质检精修。", flush=True)
    else:
        # 「正在组装」也是 ADR 0019 决定 3 点名的长步骤（复审 S2）。只在实际组装
        # 分支打：断点续跑跳过组装，打出来就是一句假话。
        print(format_step_banner("正在组装视频提示词"), flush=True)
        graph, _ = build_pipeline_graph(model, brief, config, stage=2, refs=registry)
        initial: PipelineState = {
            "brief": brief,
            "mode": brief.mode,
            "variant": brief.variant,
            "duration": brief.duration,
            "ref_meta": ref_meta,
            "iterations": 0,
            # ref_meta 不从旧 state 继承：阶段 1 落下的那份是 brief.refs 的旧口径，
            # 放它覆盖新登记，N 张覆盖式校验就静默退回空清单（issue #41 的接线点）。
            **{k: v for k, v in state.items()
               if k not in ("final_prompt", "final_report", "brief", "ref_meta")},
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
            save_checkpoint(checkpoint_dir, prompt_draft=prompt,
                            cost_table=token_meter.snapshot())

    # 有界质检精修：确定性校验 → 有 error 则 qa 给建议 + prompt_engineer 修正
    for i in range(config.max_qa_iterations):
        # 与阶段 1 的生图 qa 循环同一条横幅口径（ADR 0019 决定 3 / 复审 S2）。
        print(format_step_banner("正在质检精修", round_no=i + 1,
                                 total_rounds=config.max_qa_iterations), flush=True)
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
            save_checkpoint(checkpoint_dir, prompt_draft=prompt, status=f"qa_round_{i + 1}",
                            cost_table=token_meter.snapshot())

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
