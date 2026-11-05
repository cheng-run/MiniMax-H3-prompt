"""读图校对的问答小件（issue #162）：入问答由调用方注入，本模块不依赖 ``wizard``。

为什么搬出来：``ui/wizard.py`` 有行数闸（``tests/test_wizard_size.py``），新逻辑要么
搬出去、要么用等量搬迁换。这里收的正是「问一句、按答案回一个字符串」这类编排边角：
落点钉锚、替换文案的语言、两条要打印的告警——判定仍住 ``asset_proofread``（纯函数），
本模块只负责把话问出去、把话拼好。

``ask`` 是向导的 ``_prompt``：注入而不是 import，避免 ``wizard`` ↔ 本模块成环。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from ..asset_proofread import language_slip, locate_rewrite_span
from ..observability import interactions


def pin_rewrite_quote(dev, running: str, *, name: str, ask: Callable[[str], str]) -> str:
    """「按图改文案」落点钉锚：返回在 ``running`` 上唯一命中、语义名之外的引文；放弃返回空串。

    失锚／多处命中／只命中语义名都引导补锚（粘贴正文原文把落点钉到唯一一处），
    回车＝放弃补锚，由调用方回三选改选其他裁决——三选必须走得到底，不许未捕获崩溃
    （评审：失锚引文的「按图改文案」路径名不副实）。补锚后的引文记进裁决记录：
    ``prompt_quote`` ＝ 实际落笔的锚，不是模型原话。

    从 ``wizard.py`` 搬来（#162）：只做问一句与一次定位，不依赖向导其它状态。
    """
    quote = dev.prompt_quote
    while True:
        try:
            locate_rewrite_span(running, quote, name)
            return quote
        except ValueError as exc:
            print(f"  [提示] {exc}")
            quote = ask(
                "  补锚引文（粘贴描述正文里的原文把落点钉到唯一一处，回车=改选裁决）：").strip()
            if not quote:
                return ""


def pin_replacement_language(candidate: str, *, body: str, ask: Callable[[str], str]) -> str:
    """替换文案的语言要和正文一致（issue #162）：不一致时再问一次，回车＝照原样并入。

    为什么是「再问一次」而不是拦下或静默放行：这条文字是**图里实况**（人可以直接
    用，也可以自己译），硬拦会让人卡在一条改不了的偏差上；静默放行正是这次要治的
    形态——中文整句嵌进英文句子，链上一处都不会响。给的出路是明确的：写英文，或者
    按回车承认「就按原样并入」。

    判据取 ``asset_proofread.language_slip``（与「正文同语言」同一份口径），短中文
    （专名、「红裙女孩」这类指代）不在此列。
    """
    text = str(candidate)
    if language_slip(text, body=body) is None:
        return text
    print("  [提示] 这段替换文案是中文，而正文是英文——混排会原样进段词，请用英文重写。")
    while True:
        answer = ask("  英文替换文案（回车=按原样并入，仍为中文）：").strip()
        if not answer:
            return text
        if language_slip(answer, body=body) is None:
            return answer
        print("  [提示] 这一段仍是中文，请用英文写（回车=按原样并入）。")


def omission_language_tip(facts: Sequence[str], *, body: str) -> str | None:
    """补漏是整批并入的（没有逐条改语言的问答位），所以把话提前说：返回要打印的那行。"""
    slips = [str(f) for f in facts if language_slip(f, body=body) is not None]
    if not slips:
        return None
    return (f"  [提示] {len(slips)} 条补漏事实是中文，而正文是英文——"
            "并入后会中英混排（稍后要自己译成英文）。")


def merged_language_warning(merged: str, *, base: str) -> str | None:
    """合并结果仍是混排时的兜底告警（覆盖续跑重放等不经提问的路径）。

    告警一产生就写交互日志（#171）：这条是「中英混排响过没有」的唯一可查事件；
    返回串仍由调用方打印，本函数不碰终端。
    """
    slip = language_slip(merged, body=base)
    if slip is None:
        return None
    message = (f"  [警告] 素材画面事实里有中英混排：「{slip}」——它会原样进 "
               "<Picture N> 的描述与段词。")
    interactions.notice("language_mixed", message)
    return message


__all__ = [
    "merged_language_warning", "omission_language_tip",
    "pin_replacement_language", "pin_rewrite_quote",
]
