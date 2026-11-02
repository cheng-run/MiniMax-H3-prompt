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
# 2026-10-30 有意更新：实跑发现关键帧词里出现「同一列／同样四节」这类指代（生图模型
# 没有记忆，读不懂），以及「侧向视角 + 洞口在车的侧面」的通行几何错误。两条规则加进
# 角色提示词（自包含 / 通行几何），base 与导演台的关键帧词**都**受影响——这是有意的
# 一次显式决定，不是 ref 侧的改动顺带漏进 base。上一版哈希：
# fa325b547dc2c9952ce5fd7104348049ea2bd956100d1953b69bb2355b70671a
# 同一轮还补了一句：JSON 的 `zimage` 只是示例子键，实际子键以请求【出图模型】块为准。
# 2026-11-01 有意更新（#132 / L5 八步骨架）：关键帧角色提示词文末新增三节——Qwen 官方
# 八步骨架、首帧给视频用的两条（社区一手，本仓未验证）、状态节点＝段边界。三节**追加到
# 末尾**，既有行号不动（人读文档有 4 处按行号引用这份文件）。同一个关键帧节点，base 与
# 导演台两条路线的关键帧词**同时变**——这是有意的一次显式决定。上一版哈希：
# 03f32ec9de273b586c0c292ed28ccfb4c1f537fe24799bbba35f775fc4cabb3e
_BASE_FRAME_PROMPT_SHA256 = "8897ef01b66257feab0e47f5de65a762b3d144266fd583041575fccae6f03940"


def test_base_frame_prompt_is_byte_identical_to_the_pre_change_baseline():
    text = load_role_prompt("frame_prompt_engineer")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == _BASE_FRAME_PROMPT_SHA256, (
        "base 关键帧提示词变了：spec #84 D11 要求首尾帧路线逐字节不变。"
        "若这是有意的 base 改动，更新本基线；若是 ref 侧的模型差异，"
        "请写进 image_prompt_disciplines/ 的纪律块，而不是共用生图词表。"
    )
