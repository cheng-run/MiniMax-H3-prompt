"""最终 H3 提示词的中文摘要：让用户快速核对视频走向。

阶段 2 产出完整提示词后，追加一次轻量 LLM 调用，提炼为：
整体剧情走向（3-5 句）+ 每个 Shot 一句画面、一句声音。摘要是纯展示层，
不回写提示词；LLM 调用/解析失败一律返回 None，由调用方降级提示，不阻塞流程。
（提示词正文也是中文，摘要扮演的是"提炼概览"而非"翻译"的角色。）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

_SUMMARY_INSTRUCTION = """你是中文视频内容解说员。下面是一条 H3 视频生成提示词（中文字段、英文字段名）。
请用**简体中文**提炼内容，输出**且仅输出**一个 JSON 对象（不含 markdown 代码围栏、不含任何其他文字）：

{
  "overall": "整体剧情走向，3-5 句中文",
  "shots": [
    {"shot": 1, "visual": "本镜头画面一句话（人物/场景/动作/运镜）", "audio": "本镜头声音一句话（环境声/动作声/配乐）"}
  ]
}

要求：
- shots 必须覆盖提示词中的每一个 [Shot N]，顺序一致；
- 若提示词无 [Shot N] 结构，shots 输出空数组；
- 只忠实翻译提炼，不要补充提示词中不存在的内容。"""


@dataclass
class PromptSummary:
    """一条提示词的中文摘要。"""

    overall: str
    shots: list[dict] = field(default_factory=list)  # [{"shot": int, "visual": str, "audio": str}]

    def shot_view(self, shot_number: int) -> dict | None:
        """按 Shot 号取本段摘要；未找到返回 None。"""
        for item in self.shots:
            if item.get("shot") == shot_number:
                return item
        return None


def summarize_prompt_zh(prompt: str, llm) -> PromptSummary | None:
    """用 LLM 生成中文摘要；任何失败（调用异常/非 JSON）都返回 None。

    llm 由调用方注入（测试可传 stub）；应为带 ``invoke(str)`` 接口的对象。
    """
    try:
        response = llm.invoke(f"{_SUMMARY_INSTRUCTION}\n\n提示词：\n{prompt}")
    except Exception:  # noqa: BLE001 - 摘要是增强体验，失败绝不阻塞流程
        return None
    text = str(getattr(response, "content", response)).strip()
    # 容忍模型把 JSON 包在 ```json ... ``` 围栏里
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("overall"):
        return None
    shots = payload.get("shots")
    if not isinstance(shots, list):
        shots = []
    return PromptSummary(overall=str(payload["overall"]).strip(), shots=shots)


def render_summary_zh(summary: PromptSummary, shot_number: int | None = None) -> str:
    """把摘要渲染成终端中文卡片；传 shot_number 只渲染该镜头段。"""
    lines: list[str] = []
    if shot_number is None:
        lines.append("【整条视频走向】")
        lines.append(summary.overall)
        for item in summary.shots:
            lines.append(_render_shot(item))
    else:
        item = summary.shot_view(shot_number)
        if item is None:
            lines.append(f"（摘要中未找到 Shot {shot_number}）")
        else:
            lines.append(_render_shot(item))
    return "\n".join(lines)


def _render_shot(item: dict) -> str:
    shot = item.get("shot", "?")
    visual = str(item.get("visual", "")).strip() or "（无画面描述）"
    audio = str(item.get("audio", "")).strip() or "（无声音描述）"
    return f"· 镜头{shot}｜画面：{visual}｜声音：{audio}"


__all__ = ["PromptSummary", "summarize_prompt_zh", "render_summary_zh"]
