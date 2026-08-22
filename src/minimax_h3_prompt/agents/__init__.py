"""角色 agent 装配：每个角色 = langchain.agents.create_agent（模型 + 角色 system prompt + 工具）。

**为什么用 create_agent 而不是 deepagents.create_deep_agent**：
deepagents 的 base stack 无条件注入文件系统/execute/task 等内建工具（`ls`、`write_file`、
`execute` 等）。对本项目"单次产出文本"的角色这是负资产——模型会去调 `write_file`/`ls`
而不是直接返回文本，导致空产出。create_agent 是同一 LangChain 栈、无默认工具、轻量；
deepagents 的"有界圆桌/讨论"思想已用 LangGraph 子图实现（graph/roundtable.py）。
"""
from __future__ import annotations

import time
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool

from ..brief_parser import FRAME_VARIANTS, RefItem
from ..observability import record_agent_call, reporter
from ..tools.h3_validator import format_issues, validate_prompt
from ..tools.ref_metadata import format_ref_meta, to_tuple

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 15 个角色，覆盖电影制作 + AIGC 双流程
ROLE_KEYS = [
    "producer",
    "director",
    "screenwriter",
    "art_director",
    "character_designer",
    "background_designer",
    "prop_designer",
    "storyboard",
    "cinematographer",
    "sound_designer",
    "composer",
    "reference_consistency",
    "feasibility_reviewer",
    "prompt_engineer",
    "qa",
]

# 需要 h3_validator 校验工具的角色（审查/组装/质检）
TOOL_ROLES = {"feasibility_reviewer", "prompt_engineer", "qa"}
# 需要参考资产工具的角色（ref 模式下）
REF_TOOL_ROLES = {
    "producer",
    "art_director",
    "character_designer",
    "background_designer",
    "prop_designer",
    "reference_consistency",
    "feasibility_reviewer",
    "prompt_engineer",
    "qa",
}


def load_role_prompt(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")


def _make_tools(refs: list[RefItem], mode: str, duration: float, variant: str, role: str) -> list:
    tools: list = []
    ref_meta = to_tuple(refs)

    if role in REF_TOOL_ROLES and (mode == "ref" or variant in FRAME_VARIANTS) and refs:

        @tool
        def list_reference_assets() -> str:
            """列出当前任务的参考资产（<Picture N> 映射）。ref/帧变体模式使用。"""
            return format_ref_meta(refs)

        tools.append(list_reference_assets)

    if role in TOOL_ROLES:
        run_mode = mode

        @tool
        def validate_h3_prompt(prompt: str, mode: str = "") -> str:
            """校验一段 H3 提示词是否符合官方格式，返回问题清单。mode 为 'ref' 或 'base'（留空取当前任务模式）。"""
            issues = validate_prompt(
                prompt, mode=mode or run_mode, duration=duration, variant=variant, ref_meta=ref_meta
            )
            return format_issues(issues)

        tools.append(validate_h3_prompt)

    return tools


def build_role_agents(
    model: BaseChatModel,
    refs: list[RefItem],
    mode: str,
    duration: float,
    variant: str,
) -> dict[str, object]:
    """构建 15 个角色 agent。返回 {role_key: agent}。"""
    agents: dict[str, object] = {}
    for role in ROLE_KEYS:
        tools = _make_tools(refs, mode, duration, variant, role)
        agents[role] = create_agent(
            model=model,
            system_prompt=load_role_prompt(role),
            tools=tools or None,
            name=f"role_{role}",
        )
    return agents


def _content_to_text(content: object) -> str:
    """把模型返回的 content（str 或 blocks 列表，如 DeepSeek 带 thinking）提取成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content)


def run_agent(agent: object, message: str, retry_empty: bool = True) -> str:
    """invoke 一个角色 agent，返回最终回答的纯文本。

    优先取「最后一个有非空文本的 AI 消息」（避免工具调用后 thinking-only 消息导致空产出）。
    若整体为空且 retry_empty=True，追加一句提示重试一次（缓解大上下文偶发空产出）。
    每次调用都会上报 token 计量与进度事件（观测）。
    """
    role = getattr(agent, "name", "agent")
    reporter.emit({"type": "agent_start", "role": role})
    t0 = time.time()
    result = agent.invoke({"messages": [{"role": "user", "content": message}]})
    msgs = result.get("messages", [])

    out = ""
    for m in reversed(msgs):
        if getattr(m, "type", "") == "ai":
            txt = _content_to_text(m.content)
            if txt.strip():
                out = txt
                break
    if not out:
        for m in reversed(msgs):  # 兜底：任意最后一条非空消息
            txt = _content_to_text(m.content)
            if txt.strip():
                out = txt
                break
    if not out and retry_empty:
        out = run_agent(
            agent,
            f"{message}\n\n（你上一条回复是空的，请直接给出内容，不要空谈。）",
            retry_empty=False,
        )
    record_agent_call(role, msgs, out, time.time() - t0)
    return out
