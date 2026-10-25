"""spec #84 D11：base 路线的静态生图系统提示词必须逐字节不变。

为什么钉 ``frame_prompt_engineer``：它和资产图那条路的 ``image_prompt_engineer`` 共用
同一份生图词表（``knowledge/film-language-image.md``）。issue #87 第一次落地时把「正文
语言／负向提示词按所选模型」写进了这份**共用**词表，base 的关键帧提示词因此从 8936
字符涨到 9197 字符——全量测试全绿、产物逐字节对照也看不出来（它只影响下一次模型调用），
是 2026-10-25 协调复核时按角色提示词哈希抓到的。

修法：共用词表逐字不动，模型差异只写进 ``prompts/image_prompt_disciplines/``，并在装配
处把纪律块排在词表**之后**（让它真正压过通用条款）。这条用例就是那个边界的守卫：
ref 侧再想加模型措辞时，改纪律块，别动共用词表。
"""

from __future__ import annotations

import hashlib

from minimax_h3_prompt.agents import load_role_prompt

# 2026-10-25 改动前基线（ec42e75）。有意的 base 提示词改动可以更新这个值，但必须当成
# 一次显式决定——不能顺手在 ref 票里改共用词表把它带过去。
_BASE_FRAME_PROMPT_SHA256 = "fa325b547dc2c9952ce5fd7104348049ea2bd956100d1953b69bb2355b70671a"


def test_base_frame_prompt_is_byte_identical_to_the_pre_change_baseline():
    text = load_role_prompt("frame_prompt_engineer")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == _BASE_FRAME_PROMPT_SHA256, (
        "base 关键帧提示词变了：spec #84 D11 要求首尾帧路线逐字节不变。"
        "若这是有意的 base 改动，更新本基线；若是 ref 侧的模型差异，"
        "请写进 image_prompt_disciplines/ 的纪律块，而不是共用生图词表。"
    )
