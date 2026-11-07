"""三位资产设计师：画面内可见文字的语言按故事设定决定，不跟工作语言（issue #195）。

实测形态（#191）：一支故事发生在美国的丧尸预告片，画面里被烧进了中文角标
「咬伤后60秒（时间压缩）」。来源是设计层把**工作语言**（``brief.language``）当成了
画面可见文字的语言——工作语言说的是这套流水线的产出正文（中文 markdown），
而招牌、包装、标签、海报、烧录角标这类**画面里看得见**的字，语言由故事发生地/时代定。

判据取锚点句常量：三份设计师提示词逐字共有同一句口径，各自的举例按角色不同。常量只在本
文件写一份，不按文件各抄一遍。共用的「画面可见文字保留原语言」规则住在这条链路的下游
（``prompt_engineer`` / ``frame_prompt_engineer`` / 分段提示词），本票只加「怎么定那个原
语言」，不动保留规则，也不在这里重复钉它。

为什么查 ``load_role_prompt`` 而不只读 .md：它才是真正进 system prompt 的那份文本，
三份角色都恰好没有额外附加块，所以返回值就是 .md 本身；用它判「到得了模型眼前」。
"""
from minimax_h3_prompt.agents import load_role_prompt

DESIGNER_ROLES = ("character_designer", "prop_designer", "background_designer")

# 三份 .md 共有的口径句。改口径只改这一行与三份 .md，其它断言从它派生。
VISIBLE_TEXT_LANGUAGE_ANCHOR = "画面内可见文字的语言由故事发生地/时代决定，不跟随工作语言"
# 一正一反两个例子：#191 的失败形态就是只写了「保留原语言」却没有说原语言是哪一种，
# 模型便默认跟了工作语言。两个方向都在，才挡得住「一律写英文」和「跟着工作语言走」两种误读。
US_STORY_EXAMPLE = "故事发生在美国就写英文"
CN_STORY_EXAMPLE = "故事发生在中国就写中文"


def test_designers_set_visible_text_language_from_the_story_not_the_working_language():
    for role in DESIGNER_ROLES:
        prompt = load_role_prompt(role)
        assert VISIBLE_TEXT_LANGUAGE_ANCHOR in prompt, (
            f"{role} 没写「画面内可见文字语言按故事设定」：设计层会继续把工作语言当成"
            "画面文字语言（#191 的中文角标形态）")
        assert US_STORY_EXAMPLE in prompt, f"{role} 缺正例：美国故事写英文"
        assert CN_STORY_EXAMPLE in prompt, f"{role} 缺反例：中国故事写中文"
