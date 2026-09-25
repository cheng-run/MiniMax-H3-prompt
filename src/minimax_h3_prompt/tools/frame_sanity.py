"""生图提示词与视频提示词的日常常识 QA。

由 qa agent 按通用规则审核并产出 common_sense issue（error 级触发有界重生成）。
幻觉类问题不硬编码关键词表：通用规则写进审核指令，交给 LLM 判断；解析失败修复一次，
再失败按降级哲学返回空列表（不阻塞管线）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..agents import run_agent
from ..brief_parser import Brief
from ..clarification import as_block
from .h3_validator import ValidationIssue

# 审核通用规则：作为指令注入 qa agent，不硬编码关键词表。
_SANITY_RULES = (
    "拍摄者视角常识：vlog/自拍/纪实类题材中，被拍人物不得手持、穿戴或紧邻正在录制本画面的设备"
    "（云台、相机、手机支架、指向自己的麦克风等）；若画面中确实出现拍摄设备，必须有明确合理的"
    "自拍/镜像/他人代拍设定。",
    "物理与行为合理性：同一人物不得同时做互斥动作（如同时骑两种车、又手持又悬空同一道具）；"
    "物品数量、方位、与手部交互必须前后一致；倒影、影子与主体一致。",
    "状态一致性：跨帧/跨时间的人物服装、道具状态、场景陈设不得无理由跳变；人物位置变化要符合移动轨迹。",
    "禁止无中生有：正文与画面中的每个细节必须可溯源到用户原始主题、起步澄清、分场剧本、"
    "镜头表或关键帧描述；不得新增这些来源里不存在的主体、地点或道具。",
)


def _rules_block() -> str:
    return "\n".join(f"- {rule}" for rule in _SANITY_RULES)


def _clarify_line(brief: Brief) -> str:
    """起步澄清块 + 换行；没有澄清时是空串。

    **必须有这一块**：澄清按设计就是主题里没有、用户另外说出口的要求，而本判官的
    「禁止无中生有」只认主题/剧本/镜头表/关键帧。少了它，澄清产出的细节会被判成
    ``UNSOURCED_DETAIL`` error → 有界质检循环把它重出掉——用户的要求被机器静默撤回，
    正是本票要消灭的形态。
    """
    block = as_block(brief.clarifications)
    return f"{block}\n" if block else ""


def _audit(text: str, brief: Brief, agents: dict[str, Any]) -> list[ValidationIssue]:
    """让 qa agent 按严格 JSON 审核一段提示词；解析失败修复一次，再失败降级为空列表。"""
    instruction = (
        "你是质检员，审核下面这段视频/生图提示词是否违反日常常识。通用检查规则：\n"
        + _rules_block()
        + f"\n【用户主题】{brief.plot}\n"
        + _clarify_line(brief)
        + "只报告确定违反常识的项，不要吹毛求疵。必须只输出一个 JSON 对象（不要 markdown 围栏）：\n"
        '{"issues": [{"severity": "error" 或 "warning", "code": "如 SHOOTING_PERSPECTIVE / PHYSICAL_IMPOSSIBLE / UNSOURCED_DETAIL", "message": "一句话说明问题"}]}\n'
        "没有问题时 issues 为空数组。\n"
        f"【待审内容】\n{text}"
    )

    def _parse(raw: str) -> list[ValidationIssue]:
        cleaned = str(raw).strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start != -1 and end > start:
                data = json.loads(cleaned[start:end + 1])
            else:
                raise
        issues: list[ValidationIssue] = []
        for item in (data or {}).get("issues") or []:
            issues.append(ValidationIssue(
                severity=str(item.get("severity", "warning")),
                code=str(item.get("code", "COMMON_SENSE")),
                message=str(item.get("message", "")),
            ))
        return issues

    raw = run_agent(agents["qa"], instruction)
    try:
        return _parse(raw)
    except (json.JSONDecodeError, AttributeError, TypeError):
        repair = run_agent(
            agents["qa"],
            "上次审核输出不是合法 JSON。请重新只输出 JSON，不要 markdown。\n"
            f"{instruction}\n上次输出：\n{raw}",
        )
        try:
            return _parse(repair)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return []


def frame_common_sense_issues(bundle_dict: Any, brief: Brief, agents: dict[str, Any]) -> list[ValidationIssue]:
    """审核生图提示词 bundle 中的关键帧画面描述；无 bundle 或空帧时返回空。"""
    if not isinstance(bundle_dict, dict):
        return []
    texts: list[str] = []
    for frame_key in ("first", "last"):
        rows = bundle_dict.get(frame_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and str(row.get("positive_prompt", "")).strip():
                texts.append(f"[{frame_key} 帧] {str(row['positive_prompt']).strip()}")
    if not texts:
        return []
    return _audit("\n".join(texts), brief, agents)


def prompt_common_sense_issues(prompt: str, brief: Brief, agents: dict[str, Any]) -> list[ValidationIssue]:
    """审核最终 H3 视频提示词正文；空提示词返回空。"""
    if not str(prompt).strip():
        return []
    return _audit(str(prompt), brief, agents)


__all__ = [
    "frame_common_sense_issues",
    "prompt_common_sense_issues",
    "ValidationIssue",
]
