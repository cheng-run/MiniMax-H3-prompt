"""确定性模型桩（D2-AC15）：接在 ``model_factory.build_chat_model`` 缝上。

真 LangGraph、真节点、真状态机，只把模型回复换成按角色预置的内容。为什么按**角色**
辨认：每个 agent 的系统提示词由 ``load_role_prompt`` 现读 .md，首行就是角色标题；
圆桌直调走 ``roundtable._invoke``，系统提示词里写着「圆桌主持人」/「你正参与圆桌讨论」。
按请求正文反推调用方会在措辞一改时静默失效（本仓的老形态），角色标题则跟着提示词文件走。

``calls`` 记录每次模型调用的角色，用例用它证明节点真被执行过，而不是被跳过。
闸门（``gate_role`` + ``entered`` / ``release``）给取消用例用：在某个角色上停住，
让用例有时间发取消请求，再放行观察「当前节点跑完才停」。
"""
from __future__ import annotations

import threading
from typing import Any, List

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

# 角色提示词首行 → 角色键。加角色时这里与 ``prompts/*.md`` 的首行一起改。
_ROLE_HEADINGS: tuple[tuple[str, str], ...] = (
    ("# 制片人", "producer"),
    ("# 导演", "director"),
    ("# 编剧", "screenwriter"),
    ("# 人物形象设计师", "character_designer"),
    ("# 背景设计师", "background_designer"),
    ("# 道具设计师", "prop_designer"),
    ("# 美术指导", "art_director"),
    ("# 分镜师", "storyboard"),
    ("# 摄影指导", "cinematographer"),
    ("# 参考资产与一致性经理", "reference_consistency"),
    ("# 声音设计师", "sound_designer"),
    ("# 配乐师", "composer"),
    ("# FL2VA 关键帧提示词工程师", "frame_prompt_engineer"),
)

# 阶段 1 各角色的预置回复。全部是非空纯文本：base+T2VA 路线没有资产清单、
# 关键帧与生图节点的额外解析，节点拿到的正文直接进 state。
DEFAULT_REPLIES: dict[str, str] = {
    "producer": "制作计划：4 秒竖屏短视频，单场景呈现。",
    "director": "导演阐述：写实电影感，低饱和雨夜。",
    "screenwriter": "分场剧本：雨夜街口，人物撑伞走过积水。",
    "character_designer": "人物设计正文：一位撑黑伞的成年女性，米色风衣。",
    "background_designer": "背景设计正文：雨夜老城街口，霓虹反光。",
    "prop_designer": "道具设计正文：一把黑伞。",
    "art_director": "美术统筹正文：冷蓝主调，霓虹点缀。",
    "storyboard": (
        "分镜镜头表：\n"
        "[Shot 1] At 00:00.000, a medium shot of the woman under a black umbrella.\n"
        "[Shot 2] At 00:02.000, a closer shot as she walks through the puddle."
    ),
    "cinematographer": "画面细化：每镜中景，手持跟随。",
    "reference_consistency": "一致性核对：人物与场景保持一致。",
    "sound_designer": "对白表：无对白。环境声：雨声与脚步声。",
    "composer": "non_diegetic_music：低频弦乐铺底。",
    "roundtable_speaker": "同意当前方案，按情节节拍推进。",
    # 主持人回复不能以「继续」开头，否则圆桌会再开一轮——桩要让测试线性可数。
    "roundtable_moderator": "锁定决策：写实雨夜短视频，四秒，单场景。",
}


def role_of(messages: List[BaseMessage]) -> str:
    """从出站消息认出角色；认不出返回 ``unknown``（用例会看见它，不会静默当成别人）。"""
    system = next(
        (str(message.content) for message in messages
         if getattr(message, "type", "") == "system"),
        "",
    )
    if "你是圆桌主持人" in system:
        return "roundtable_moderator"
    if "你正参与圆桌讨论" in system:
        return "roundtable_speaker"
    first_line = system.splitlines()[0] if system else ""
    for heading, role in _ROLE_HEADINGS:
        if first_line.startswith(heading):
            return role
    return "unknown"


class DeterministicChatModel(BaseChatModel):
    """按角色返回预置文本的桩模型；``bind_tools`` 返回自身，供 ``create_agent`` 装配。"""

    replies: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_REPLIES))
    calls: List[str] = Field(default_factory=list)
    gate_role: str = ""

    _entered: threading.Event = PrivateAttr(default_factory=threading.Event)
    _release: threading.Event = PrivateAttr(default_factory=threading.Event)
    _gate_used: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self) -> str:
        return "workbench-deterministic-stub"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "DeterministicChatModel":
        return self

    @property
    def entered(self) -> threading.Event:
        return self._entered

    @property
    def release(self) -> threading.Event:
        return self._release

    def _generate(self, messages: List[BaseMessage], stop: Any = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        role = role_of(messages)
        self.calls.append(role)
        if role == self.gate_role and not self._gate_used:
            self._gate_used = True
            self._entered.set()
            # 超时只是防止取消用例写错时把整个测试挂死；正常路径由用例 release。
            if not self._release.wait(timeout=30):
                raise RuntimeError("确定性桩的闸门等待超时：用例没有放行")
        reply = self.replies.get(role, f"{role} 的预置回复")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=reply))])


__all__ = ["DEFAULT_REPLIES", "DeterministicChatModel", "role_of"]
