"""起步前置澄清：把「能说清但从没说」的模糊在生成前钉具体（issue #29 / T7）。

**它承接哪一类模糊**：用户其实说得清、只是从没说出口的那些（朝代、季节、人物年代、
禁忌元素）。用户**看到首帧图之后**才意识到的「不够唯美」不在此列——那类问不出来，
由阶段 1 首帧环节的事后修订机制承接（``user_revisions``，见 ``docs/adr/0005``）。
两者互补，不互相替代。

**为什么澄清走独立字段而不拼进 ``brief.plot``**：``plot`` 同时喂着主题地点守卫
（按**子串**提取地点硬约束）与生图常识判官（「不得新增主题中不存在的主体、地点或道具」
的判据）。把自由文本塞进去等于让澄清文字随机改写两条闸门的判据，且有否定句反噬——
用户说「不要森林，改到庭院」会让守卫**反而开始要求**正文出现「森林」。

**为什么总是问**（而不是「主题模糊才问」）：模糊与否是不可证判据，判它要靠另一个
启发式，而误判的代价由用户承担。改成「总是问 + 一句话即可跳过」——跳过权在用户手里，
判据不必存在。

**已知残余风险（只细化，不推翻主题）**：地点守卫的必需词只从**主题**抽取
（``theme_guard.scene_requirement_groups(brief.plot)``），澄清不进那条通道。于是澄清能
*细化*主题（「北宋的庭院」），但**顶不过**主题已写定的地点——主题写了森林、用户答「改到
庭院」，守卫仍要求 forest/森林，首帧节点会被打回重试，用户的答案在此处失效。故提示词
明确禁止问这类会推翻主题的问题；用户若仍这么答，本模块不做裁决（语义判定不可证，见
spec 的 Out of Scope），只是这条残余风险在此备案。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

CLARIFY_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "clarifier.md"

# 注入块的标题，取 CONTEXT.md 的术语「起步澄清」。它和「用户修订」会在同一个请求里并排
# 出现（首帧节点两样都吃），所以标题必须与术语表逐字一致，不得简写成「用户澄清」。
BLOCK_LABEL = "起步澄清"

# 结束澄清、直入原流程的输入词（大小写不敏感）。刻意不含单个字母：它太容易与
# 一次正常回答撞车，而撞车的代价是用户想说的那句被当成跳过。
SKIP_WORDS = ("跳过", "skip")

# 行首编号/项目符号：模型很爱加，剥掉后问题本身才能原样问给用户。
_PREFIX_RE = re.compile(r"^\s*(?:[-*•·]|[（(]?\d{1,2}[.、)）])\s*")


def _load_instruction() -> str:
    return CLARIFY_PROMPT_PATH.read_text(encoding="utf-8")


def is_skip(text: object) -> bool:
    """用户这行输入是否表示「结束澄清、直接进入原流程」。"""
    return str(text).strip().lower() in SKIP_WORDS


def parse_questions(text: str, limit: int) -> list[str]:
    """从模型输出里取 ≤ ``limit`` 个问句（一行一个，剥掉编号与项目符号）。

    判据：**行内出现问号**即视为问句（提示词要求以问号结尾，这里取更宽的包含判断——
    模型偶尔会在问句后补一句说明，按「结尾必须是问号」会把整条问题丢掉）；**全篇没有
    问号则一个都不取**，于是「无需澄清，主题已清晰」这类寒暄不会被当成问题问给用户。
    """
    if limit <= 0:
        return []
    if "？" not in text and "?" not in text:
        return []
    questions: list[str] = []
    for raw_line in text.splitlines():
        line = _PREFIX_RE.sub("", raw_line).strip()
        if not line or ("？" not in line and "?" not in line):
            continue
        questions.append(line)
        if len(questions) >= limit:
            break
    return questions


def generate_clarifying_questions(topic: str, llm, limit: int) -> list[str]:
    """针对主题生成澄清问题；``limit <= 0`` 表示关闭澄清（连模型都不调）。

    本函数**不吞异常**：网络/网关/空输出由调用方决定怎么办（向导的选择是报出来再
    跳过澄清，绝不静默）。这里只把「没有问句」如实返回成空列表。
    """
    if limit <= 0:
        return []
    request = f"{_load_instruction()}\n\n最多 {limit} 个问题。\n主题：{topic}"
    response = llm.invoke(request)
    return parse_questions(str(getattr(response, "content", response)), limit)


def render_clarification_block(pairs: Iterable[tuple[str, str]] | None) -> str:
    """把（问题, 回答）渲染成注入用的澄清块；空清单返回空串（``_ctx`` 自动跳过）。

    编号与用户修订清单（``user_revisions.render_revision_block``）同形：两块并排出现
    在请求里时，模型读到的是同一套「带编号的清单一律必须落实」的形态。
    """
    entries = [
        (str(question).strip(), str(answer).strip())
        for question, answer in (pairs or [])
        if str(answer).strip()
    ]
    if not entries:
        return ""
    lines = [f"{index}. {question} → {answer}" for index, (question, answer) in enumerate(entries, 1)]
    # 自述效力：块标题由注入方加，光看内容模型会当参考资料。这句话要同时挡住两种失手：
    # 生成时当参考资料（不落实）与质检时当无中生有（重出掉）。
    lines.append("（以上是用户在生成前给出的澄清结论，属于用户原始要求的一部分："
                 "必须落实，不得当成无中生有删掉。）")
    return "\n".join(lines)


def as_block(clarifications: str) -> str:
    """把澄清正文渲染成带标题的注入块；无澄清返回空串。

    给**不走** ``nodes._ctx`` 的调用方用（生图常识判官、阶段 2 的修复指令）——标题和
    ``_ctx(BLOCK_LABEL=...)`` 必须同形，否则同一个概念在请求里以两个名字出现。
    效力说明（「必须落实」「不是无中生有」）已经写在正文里（``render_clarification_block``
    的末行），这里不再各写一套。
    """
    text = str(clarifications or "").strip()
    return f"【{BLOCK_LABEL}】\n{text}" if text else ""


__all__ = [
    "BLOCK_LABEL",
    "CLARIFY_PROMPT_PATH",
    "SKIP_WORDS",
    "as_block",
    "generate_clarifying_questions",
    "is_skip",
    "parse_questions",
    "render_clarification_block",
]
