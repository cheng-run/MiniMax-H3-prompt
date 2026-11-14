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

import json
import re
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

# 导演台的写段回复（#262）：三字段外壳齐全、无图片指称、时间戳在段窗口内；时间轴
# 注记由 ``normalize_segment_output`` 注进正文，桩不自己抄一份。
_DIRECTOR_SEGMENT_REPLY = (
    "integrated_multimodal_description:\n"
    "[Shot 1] At 00:00.000, a woman under a black umbrella walks through the "
    "rainy street, neon reflecting in the puddles.\n\n"
    "overall_soundscape:\n"
    "Rain on the pavement and steady footsteps.\n\n"
    "non_diegetic_music:\n"
    "Low sustained strings."
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
    # 阶段 2 的中文摘要（summary.summarize_prompt_zh 要求 JSON）：合法摘要才会落
    # summary-zh.json/md，让「三件套」在真实路径用例里可断言。
    "summary": (
        '{"overall": "雨夜街口的短片：人物撑伞走过积水，停在旧楼门口收伞回望。",'
        ' "shots": [{"shot": 1, "visual": "雨夜街口撑伞行走", "audio": "雨声与脚步声"}]}'
    ),
    # 导演台写段（#262）：三字段外壳齐全、无图片指称、时间戳在段窗口内；时间轴注记
    # 由 ``normalize_segment_output`` 注进正文，桩不自己抄一份。
    "director_segment_writer": _DIRECTOR_SEGMENT_REPLY,
}


# 导演台的两条直调没有 system 消息（``run_direct_call(llm, <字符串>)``）：按出站请求
# 里的**唯一文案**认角色，和按系统提示词标题认角色同一条纪律——措辞改了就该报红，
# 而不是静默落进 ``unknown`` 让计数断言失去意义。
_DIRECTOR_PLANNER_MARK = "Segment Planner（导演台分段规划师）"
_DIRECTOR_WRITER_MARK = "本段编号：Segment "
# r2v 写段与三字段写段共用同一个角色标题，靠请求里只有 r2v 才有的预算块分开
# （``prompts.build_segment_request`` 的 ``【本段参考图预算】``）。分开是为了给 r2v
# 一份六节外壳的合法回复：三字段回复会被 ``validate_director_segment`` 判缺段。
_DIRECTOR_R2V_BUDGET_MARK = "【本段参考图预算】"
_REF_SECTIONS_MARK = "参考生视频提示词的两节"


def director_ref_sections_reply(request: str) -> str:
    """导演台 r2v 定义块的确定性回复：按请求里的登记表逐张定义（英文两节）。

    每张登记图都要被定义——``refdefs._guard_issues`` 的 ``PICTURE_UNUSED`` 会把漏定义
    的图判成硬错、耗尽重写预算后抛。登记从**请求正文**现读，不写死张数。
    """
    numbers = re.findall(r"<Picture\s+(\d+)>\s*=", request)
    definitions = "\n".join(
        f"<Picture {number}> is a registered reference image for the scene."
        for number in numbers)
    retention = "\n".join(
        f"<Picture {number}> fully_preserved." for number in numbers)
    return (
        "subject_definitions:\n"
        f"{definitions or '<Picture 1> is a registered reference image.'}\n\n"
        "retention_analysis:\n"
        f"{retention or '<Picture 1> fully_preserved.'}"
    )


# r2v 写段回复：模型只写 summary / detailed_description / 两条音轨，定义块与时间轴
# 注记由真实的 ``normalize_segment_output`` 注入（与桩管线同一分工）。
_DIRECTOR_R2V_SEGMENT_REPLY = (
    "summary:\n"
    "target_video: 4 秒雨夜街口。\n\n"
    "detailed_description:\n"
    "[Shot 1] At 00:00.000, a medium shot of the subject <Picture 1> in the rainy "
    "street, neon reflecting in the puddles.\n\n"
    "overall_soundscape:\n"
    "Rain on the pavement and steady footsteps.\n\n"
    "non_diegetic_music:\n"
    "Low sustained strings."
)


def director_plan_reply(request: str) -> str:
    """导演台规划师的确定性回复：单段开场，窗口覆盖请求里声明的总时长。

    总时长从**请求正文**现读（不猜一个默认值）：用例换时长时，段表要跟着覆盖到
    那一段，否则 ``parse_director_plan`` 会以「没有收在总时长」判红。
    """
    match = re.search(r"视频总时长：([0-9.]+)\s*秒", request)
    total = float(match.group(1)) if match else 4.0
    return json.dumps(
        {"segments": [{
            "type": "opening",
            "start_s": 0,
            "end_s": total,
            "summary": "雨夜街口开场：人物撑伞走过积水",
            "end_hook": "人物推开旧楼木门，抬脚跨过门槛走进门廊",
        }]},
        ensure_ascii=False,
    )


def role_of(messages: List[BaseMessage]) -> str:
    """从出站消息认出角色；认不出返回 ``unknown``（用例会看见它，不会静默当成别人）。"""
    body = "\n".join(str(message.content) for message in messages)
    if _DIRECTOR_PLANNER_MARK in body:
        return "director_planner"
    if _REF_SECTIONS_MARK in body:
        return "director_ref_sections"
    if _DIRECTOR_WRITER_MARK in body:
        return ("director_segment_writer_r2v"
                if _DIRECTOR_R2V_BUDGET_MARK in body
                else "director_segment_writer")
    system = next(
        (str(message.content) for message in messages
         if getattr(message, "type", "") == "system"),
        "",
    )
    if "你是圆桌主持人" in system:
        return "roundtable_moderator"
    if "你正参与圆桌讨论" in system:
        return "roundtable_speaker"
    # 摘要直调走 ``llm.invoke(<字符串>)``，指令落在 HumanMessage 里、没有 system
    # 消息；不单列会让它落进 ``unknown``，把「prompt_engineer 只跑了一次」这类
    # 计数断言污染成「看起来多调了一次」。
    if any("你是中文视频内容解说员" in str(message.content)
           for message in messages):
        return "summary"
    first_line = system.splitlines()[0] if system else ""
    for heading, role in _ROLE_HEADINGS:
        if first_line.startswith(heading):
            return role
    return "unknown"


class DeterministicChatModel(BaseChatModel):
    """按角色返回预置文本的桩模型；``bind_tools`` 返回自身，供 ``create_agent`` 装配。"""

    replies: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_REPLIES))
    calls: List[str] = Field(default_factory=list)
    # 每次调用的出站正文（按原样拼接）：跨会话串味的回归用例要能看见「这一段写段
    # 请求里到底带了哪张尾帧的描述」，只记角色名看不出来。
    requests: List[str] = Field(default_factory=list)
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
        body = "\n".join(str(message.content) for message in messages)
        self.requests.append(body)
        self.calls.append(role)
        if role == self.gate_role and not self._gate_used:
            self._gate_used = True
            self._entered.set()
            # 超时只是防止取消用例写错时把整个测试挂死；正常路径由用例 release。
            if not self._release.wait(timeout=30):
                raise RuntimeError("确定性桩的闸门等待超时：用例没有放行")
        reply = self.replies.get(role, f"{role} 的预置回复")
        if role == "director_planner" and role not in self.replies:
            # 段表必须覆盖请求里声明的总时长：回复随请求现算，不写死一个 4 秒。
            # 用例显式给了 ``director_planner`` 回复时以它为准（坏回复测试）。
            reply = director_plan_reply(
                "\n".join(str(message.content) for message in messages))
        if role == "director_ref_sections" and role not in self.replies:
            reply = director_ref_sections_reply(body)
        if role == "director_segment_writer_r2v" and role not in self.replies:
            reply = _DIRECTOR_R2V_SEGMENT_REPLY
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=reply))])


__all__ = ["DEFAULT_REPLIES", "DeterministicChatModel", "role_of"]
