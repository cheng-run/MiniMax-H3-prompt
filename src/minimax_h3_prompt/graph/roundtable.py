"""有界圆桌讨论子图：共享 discussion 通道，≤max_rounds 轮，主持人收束成锁定决策。

参与者以「讨论 persona」轻量 LLM 节点发言（避免背全部生产 prompt 的成本）。
子图输出 `lock` 字段；外层用 make_roundtable_node 把它映射到主状态对应字段。
"""
from __future__ import annotations

import operator
import time
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from ..agents import _content_to_text
from ..observability import record_agent_call, reporter

DISCUSSION_PERSONAS = {
    "producer": "你是制片人：关注任务类型、时长、风格约束、成本与可交付性。",
    "director": "你是导演：关注情绪基调、视觉风格、叙事节奏。",
    "screenwriter": "你是编剧：关注剧情逻辑、对白、角色动机。",
    "art_director": "你是美术指导：关注角色造型、场景、色彩、美术风格。",
    "storyboard": "你是分镜师：关注镜头拆分、景别、构图、节奏。",
    "cinematographer": "你是摄影指导：关注构图、机位运动、光线、景深。",
    "sound_designer": "你是声音设计师：关注对白标注、环境声。",
    "composer": "你是配乐师：关注非剧情音乐的配器与动态。",
    "reference_consistency": "你是参考资产与一致性经理：关注 <Picture N> 标签映射与跨镜头身份一致性。",
    "feasibility_reviewer": "你是可生成性审查员：关注每个镜头能否被 H3 生成、风险与修改建议。",
    "prompt_engineer": "你是提示词工程师：关注最终输出是否符合官方格式。",
    "qa": "你是质检员：关注格式与内容是否合格。",
}

_CONTINUE = "继续"


def _invoke(role: str, model: BaseChatModel, msgs: list) -> str:
    """带观测地调用一次模型（圆桌发言/主持），返回纯文本。"""
    reporter.emit({"type": "agent_start", "role": role})
    t0 = time.time()
    res = model.invoke(msgs)
    out = _content_to_text(res.content)
    record_agent_call(role, [res], out, time.time() - t0)
    return out


class RoundTableState(TypedDict):
    discussion: Annotated[list[str], operator.add]
    round: int
    topic: str
    context: str
    lock: str


def build_roundtable(model: BaseChatModel, participants: list[str], max_rounds: int):
    """构造有界圆桌子图。participants 为角色 key 列表（按发言顺序）。"""

    def make_speaker(role: str):
        persona = DISCUSSION_PERSONAS.get(role, f"你是{role}。")

        def speak(state: RoundTableState) -> dict:
            past = "\n".join(state.get("discussion") or [])
            msgs = [
                SystemMessage(
                    content=(
                        f"{persona}\n你正参与圆桌讨论「{state['topic']}」。"
                        "请简短直接表态，给可执行的结论、质疑或修改建议，不要复述背景，100 字以内。"
                    )
                ),
                HumanMessage(
                    content=(
                        f"背景：{state['context']}\n\n已发言：\n{past or '（尚未有人发言）'}\n\n"
                        f"现在轮到你（{role}）发言："
                    )
                ),
            ]
            reply = _invoke(f"圆桌·{role}", model, msgs)
            return {"discussion": [f"{role}：{reply}"]}

        return speak

    def moderate(state: RoundTableState) -> dict:
        past = "\n".join(state.get("discussion") or [])
        current_round = state.get("round", 1)
        forced = current_round >= max_rounds
        sys = (
            "你是圆桌主持人。判断讨论是否已达共识：\n"
            "· 若已共识 → 直接输出最终「锁定决策」（一段结构化结论，含任务类型/方向/关键决定）。\n"
            "· 若未共识 → 只输出一行「继续」，并指出下一轮要聚焦的分歧点。"
            if not forced else
            "你是圆桌主持人。这是最后一轮，请忽略分歧，直接把讨论收束成最终「锁定决策」（一段结构化结论）。"
        )
        msgs = [
            SystemMessage(content=sys),
            HumanMessage(content=f"议题：{state['topic']}\n讨论记录：\n{past}\n\n你的判断："),
        ]
        reply = _invoke("圆桌·主持人", model, msgs).strip()

        if reply.startswith(_CONTINUE):
            if forced:
                # 最后一轮仍"继续"→ 强制再收束一次
                msgs2 = [
                    SystemMessage(content="请直接把上面的讨论收束成最终「锁定决策」，不要再说继续。"),
                    HumanMessage(content=f"议题：{state['topic']}\n讨论记录：\n{past}"),
                ]
                reply = _invoke("圆桌·主持人", model, msgs2).strip()
            else:
                return {"lock": "", "round": current_round + 1}

        if not reply:
            # 兜底：模型返回空 → 用最后一条讨论发言作决策，避免空 lock 流入下游
            reply = past.strip().splitlines()[-1] if past.strip() else "（讨论未产出结论）"
        return {"lock": reply, "round": current_round + 1}

    g = StateGraph(RoundTableState)
    first = participants[0]
    prev: str | None = None
    for role in participants:
        g.add_node(f"speak_{role}", make_speaker(role))
        if prev is None:
            g.add_edge(START, f"speak_{role}")
        else:
            g.add_edge(prev, f"speak_{role}")
        prev = f"speak_{role}"

    g.add_node("moderator", moderate)
    g.add_edge(prev, "moderator")

    def route(state: RoundTableState) -> str:
        if not state.get("lock") and state.get("round", 1) <= max_rounds:
            return f"speak_{first}"
        return END

    g.add_conditional_edges("moderator", route, {f"speak_{first}": f"speak_{first}", END: END})
    return g.compile()
