"""Agent 角色提示词（``src/minimax_h3_prompt/prompts/*.md``）与官方规范的一致性。

这些 .md 是 LLM 直接读到的文本——它们说错话，产出就跟着错，而链条上没有别的东西会报红。

2026-09-23（issue #8）的教训：`prompt_engineer.md` 把官方**明令要求**的 edge-stability 句
列为「自创结构」禁令，而同一批改动里分段模板又必须要求它——同一句话，一处禁一处要。
本文件钉住「角色提示词不得与官方规范相反」这一条。
"""
from minimax_h3_prompt.agents import load_role_prompt
from minimax_h3_prompt.tools.h3_validator import EDGE_STABILITY_SENTENCE


def test_prompt_engineer_requires_edge_stability_sentence():
    """整片提示词工程师必须要求块尾 edge-stability 句，且不得再禁它。"""
    prompt = load_role_prompt("prompt_engineer")

    assert EDGE_STABILITY_SENTENCE in prompt, \
        "prompt_engineer.md 未要求官方 edge-stability 原句（每个镜头块都要以此收尾）"
    # 这份 .md 不该再用「防波纹咒语」框架提它——那正是 2026-09-22 误判的原话，
    # 留着它会把这个要求重新读成自创结构
    assert "防波纹" not in prompt, \
        "prompt_engineer.md 仍以「防波纹咒语」框架描述官方要求（那不是自创结构）"
