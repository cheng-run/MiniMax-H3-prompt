"""L5 八步骨架只进关键帧角色提示词（#132 / L5-03…L5-11，红线 D2）。

两向守卫，都按**装配后的系统提示词**判，不按 .md 的字面位置判：

1. 关键帧系统提示词含八步骨架——base 与导演台共用同一个关键帧节点，所以两条路线一起变；
2. 资产图系统提示词（三档模型各一份）**不含**八步骨架——``qwen.md`` 同时是人物／道具／
   场景三个资产节点的系统提示词，把骨架写进纪律块就等于要求参考图也写 400–500 词、
   8–14 个位置短语。这条是防实现票「图省事把骨架写进纪律块」的守卫。

**形态从常量派生**：节标题与九条片语只在下面声明一次，切片函数按标题切出那一节。
守卫认的是「标题 ＋ 这一节里的片语」，不是「全文任意位置出现过某个词」——后者在把骨架
搬进资产图纪律之后仍然为真，等于永不为红。变异检验见文件末尾三条。
"""
from __future__ import annotations

import json
import sys
from typing import List

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from minimax_h3_prompt.agents import build_role_agents, load_role_prompt
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.image_config import IMAGE_MODEL_KEYS

# 八步骨架那一节的标题（逐字，含 Markdown 层级）。改标题＝改守卫的锚：角色提示词里的
# 标题、这个常量、下面的切片函数要一起看，否则切片落空、本文件会红（这正是想要的）。
EIGHT_STEP_HEADING = "## 帧要怎么写（Qwen 官方八步骨架）"

# 九条各收一个关键片语：开场句／位置短语／走帧顺序／直双引号／光线独立句／整帧收尾句／
# 规模／枚举不概括／语言。只认片语、不抄整句——整句抄下来就是第二份可写来源，
# 措辞微调时测试会无意义地红，真正被删的那一条反而看不出来。
EIGHT_STEP_ANCHORS = (
    "开场句",
    "8–14",
    "走帧顺序",
    "直双引号",
    "光线",
    "整帧收尾",
    "400–500",
    "枚举",
    "英文",
)

# 资产图侧必无的片语：挑**只可能来自八步**的那几个，避开纪律块本来就有的共性词
# （qwen 纪律自己也写着「正文用英文」「文字」，拿这些当判据会把合规文本判红）。
EIGHT_STEP_EXCLUSIVE_ANCHORS = ("8–14", "400–500", "整帧收尾", "直双引号")


def _section(text: str, heading: str) -> str:
    """按二级标题切出一节（到下一个 ``## `` 标题为止）；没有这一节返回空串。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != heading:
            continue
        body = []
        for following in lines[index + 1:]:
            if following.startswith("## "):
                break
            body.append(following)
        return "\n".join(body)
    return ""


def _has_eight_step_skeleton(text: str) -> bool:
    """八步骨架在场判据：那一节在，且九条片语都在这**一节**里。"""
    section = _section(text, EIGHT_STEP_HEADING)
    return bool(section.strip()) and all(
        anchor in section for anchor in EIGHT_STEP_ANCHORS)


def _asset_is_free_of_the_skeleton(text: str) -> bool:
    """资产图侧判据：没有那一节，也没有只属于八步的片语。"""
    return (
        EIGHT_STEP_HEADING not in text
        and not _has_eight_step_skeleton(text)
        and all(anchor not in text for anchor in EIGHT_STEP_EXCLUSIVE_ANCHORS)
    )


def _frame_prompt() -> str:
    return load_role_prompt("frame_prompt_engineer")


def _asset_prompt(model: str) -> str:
    return load_role_prompt("image_prompt_engineer", image_model=model)


# ---------------------------------------------------------------------------
# 1. 关键帧侧：八步真的到了系统提示词里
# ---------------------------------------------------------------------------

def test_the_keyframe_system_prompt_carries_the_eight_step_skeleton():
    """base 与导演台共用的关键帧系统提示词里，八步骨架是完整的一节（不是一句「按八步写」）。"""
    prompt = _frame_prompt()

    assert _has_eight_step_skeleton(prompt), (
        "关键帧系统提示词里没有完整的八步骨架（或某一节被拆散了）："
        f"{_section(prompt, EIGHT_STEP_HEADING)!r}"
    )
    # 文字上限必须写在关键帧这一侧：纪律块里那条「逐条写」已按 D1 声明只管资产图，
    # 这边的「≤2」是它的对应口径——两句话少一句，模型就只剩一个上限可用。
    assert "≤2" in _section(prompt, EIGHT_STEP_HEADING), \
        "八步骨架里没写「必须可读的文字 ≤2 条」"


# ---------------------------------------------------------------------------
# 2. 资产图侧：八步一条都不许过去（红线 D2）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", IMAGE_MODEL_KEYS)
def test_the_asset_image_system_prompt_has_no_eight_step_skeleton(model):
    """三档资产图系统提示词都不含八步——写进去等于要求参考图也写 400–500 词。"""
    prompt = _asset_prompt(model)

    assert EIGHT_STEP_HEADING not in prompt, \
        f"{model} 档的资产图系统提示词里出现了八步骨架那一节"
    assert _asset_is_free_of_the_skeleton(prompt), \
        f"{model} 档的资产图系统提示词里混进了八步骨架的条目：{prompt!r}"


# ---------------------------------------------------------------------------
# 3. 到达性：出站给模型的 system 消息里真有八步（不是只在加载口返回）
# ---------------------------------------------------------------------------

class RecordingChatModel(BaseChatModel):
    """记录型桩模型：把每次调用的**真实出站消息**（含 system）存下来，再回固定 JSON。"""

    reply: str = ""
    seen: List[List[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "ticket132-recording-stub"

    def _generate(self, messages: List[BaseMessage], stop=None,
                  run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _bundle_reply() -> str:
    """一份形状合规的关键帧回复（首尾帧都在，地点词与主题对得上）。"""
    return json.dumps({
        "scene_anchor": "medieval tavern interior",
        "first": {"zimage": {"positive_prompt":
                             "Three adventurers sit around a wooden table inside a medieval "
                             "tavern interior, with mugs and quest gear visible."}},
        "last": {"zimage": {"positive_prompt":
                            "The same three adventurers raise their mugs inside the same "
                            "medieval tavern interior, with the same table and quest gear."}},
        "continuity_constraints": ["Keep the same characters, clothing and props."],
    }, ensure_ascii=False)


def test_the_keyframe_request_really_carries_the_skeleton_to_the_model():
    """到达性：帧节点真正发出去的 system 消息里有八步骨架。

    打桩 ``run_agent`` 只能证明「节点调了谁」；这里用记录型桩模型抓真实出站消息——
    本仓「规则在、接线不在」的复发形态，靠这条兜底（spec #128 的测试取舍）。
    """
    model = RecordingChatModel()
    model.reply = _bundle_reply()
    agents = build_role_agents(model, [], "base", 5.0, "FL2VA")
    node = nodes._make_fl2va_frame_prompt_node(agents)
    brief = Brief(variant="FL2VA", duration=5, plot="中世纪酒馆室内")

    node({
        "brief": brief,
        "script": "script",
        "character_design": "characters",
        "prop_design": "props",
        "background_design": "tavern interior",
        "art_design": "integrated tavern design",
        "shot_table": "start and end states",
        "shot_review_lock": "locked shot",
        "visual_design": "medium-wide composition",
    })

    systems = [
        str(message.content)
        for messages in model.seen
        for message in messages
        if getattr(message, "type", "") == "system"
    ]

    assert systems, "帧节点没有向模型发出任何请求"
    assert any(_has_eight_step_skeleton(system) for system in systems), \
        "出站 system 消息里没有八步骨架——角色提示词里的那一节没接到帧节点"


# ---------------------------------------------------------------------------
# 4. 变异检验：判据不是永不为红
# ---------------------------------------------------------------------------

def test_the_guard_bites_when_the_heading_moves():
    """变异一：把节标题改掉（模拟有人把骨架整节挪走、只留正文），判据必须红。

    这条防的是「守卫按全文找片语」——那样标题挪走它仍为真，等于没有守卫。
    """
    moved = _frame_prompt().replace(EIGHT_STEP_HEADING, "## 帧要怎么写（草稿）")

    assert not _has_eight_step_skeleton(moved)


def test_the_guard_bites_when_the_asset_prompt_gains_the_skeleton():
    """变异二：把骨架整节塞进资产图系统提示词（模拟图省事的实现），判据必须红。

    这就是本文件存在的理由：八步写进纪律块后，人物／道具／场景三张参考图也会被要求
    写 400–500 词、8–14 个位置短语。
    """
    polluted = (
        _asset_prompt("qwen") + "\n" + _section(_frame_prompt(), EIGHT_STEP_HEADING)
    )

    assert not _asset_is_free_of_the_skeleton(polluted)


def test_the_anchor_list_is_load_bearing(monkeypatch):
    """变异三：把片语表换成节里不存在的词，判据必须红——片语表不是摆设。

    若判据实际看的是别的字面（例如整份 .md），换表不会红，守卫就是永不为红。
    """
    monkeypatch.setattr(
        sys.modules[__name__], "EIGHT_STEP_ANCHORS", ("不存在的片语",))

    assert not _has_eight_step_skeleton(_frame_prompt())
