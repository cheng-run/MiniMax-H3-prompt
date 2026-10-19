"""续跑点（issue #51）：问答答案的增量落盘。

CONTEXT.md「续跑点」：多参路线的进度落盘单位——每答一问、每定稿一个计价产物
（提示词定稿／素材进台账／裁决落账／尺寸定案）即写盘；中断后从最后一个续跑点继续，
已定稿的不重跑不重付。本模块只管**答案账**这一件纯事（state 里的 ``resume_answers``），
问答节奏（什么时候问、什么时候建续跑点）住在 ``ui.wizard``。

答案账**不是产物的替代**：一条路径答案只记「这道题已经答过」，产物（素材正本、
台账登记、校对裁决）一落定就该把它作废（``forget``）——两本账并存时，续跑会读到
一份早已被产物取代的旧答案，而它比产物更旧却更显眼。
"""
from __future__ import annotations

# 答案账在 state 里的键（会话持久化白名单同此名，见 session_store._STATE_KEYS）。
ANSWERS_KEY = "resume_answers"


def answer_book(state: dict) -> dict:
    """答案账的**唯一读口**：``{问题键: 答案}``，键缺失时补出空账并落回 state。

    错形**响**（ValueError）——本仓统一口径：falsy 错形不许被 ``or {}`` 洗成空表
    悄悄起新账（同 ``asset_loop.loop_state`` 与画布字段的严格读回）。
    """
    raw = state.setdefault(ANSWERS_KEY, {})
    if not isinstance(raw, dict):
        raise ValueError(f"答案账字段错形：{raw!r}（应为 问题键→答案 的对象）")
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"答案账条目错形：{ANSWERS_KEY}[{key!r}] = {value!r}（键与值都应为字符串）")
    return raw


def remember(state: dict, key: str, value: str) -> None:
    """记一条答案（答完立刻调；调用方负责紧接着写盘）。"""
    answer_book(state)[str(key)] = str(value)


def recall(state: dict, key: str) -> str | None:
    """读回一条答案；没答过返回 None（与「答了空串」区分开——空串是有效答案）。"""
    return answer_book(state).get(str(key))


def forget(state: dict, key: str) -> None:
    """作废一条答案（产物已落定时调：答案账只补产物落定前的空档）。"""
    answer_book(state).pop(str(key), None)


__all__ = ["ANSWERS_KEY", "answer_book", "forget", "recall", "remember"]
