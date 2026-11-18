"""三族写作纪律的唯一可写来源与导演台占位符替换（#312）。

为什么要这组用例：纪律改一处要四条写段模式同时生效。真源一旦有第二份拷贝、或某条
模式漏了替换，模型那一侧就会静默收到「{{...}}」字面量或过时措辞——出站请求才是证据。
"""
from __future__ import annotations

from minimax_h3_prompt import writing_disciplines as wd
from minimax_h3_prompt.director.prompts import PROMPTS_DIR, load_instruction

WRITE_MODES = ("t2v", "i2v", "fl2v", "r2v")


def test_module_holds_only_text_constants():
    """真源模块只放文本：任何非字符串的公开名都说明逻辑混了进来。"""
    public = {name: value for name, value in vars(wd).items()
              if not name.startswith("__")}
    assert public, "写作纪律真源模块里没有任何常量"
    assert all(isinstance(value, str) for value in public.values()), \
        f"真源模块混入了非文本内容：{sorted(public)}"


def test_every_write_mode_receives_the_three_disciplines_without_placeholders():
    """四种模式的加载口都要拿到三条纪律，且不残留占位符形态。"""
    for mode in WRITE_MODES:
        text = load_instruction(mode)
        assert "{{" not in text, f"{mode} 的写段指令残留未替换的占位符"
        assert wd.CAMERA_SPEED_DISCIPLINE in text, f"{mode} 缺机位速度纪律"
        assert wd.FUTURE_EVENTS_DISCIPLINE in text, f"{mode} 缺时间窗外剧情禁区纪律"
        assert wd.ANAPHORA_DISCIPLINE in text, f"{mode} 缺禁止指代前文的纪律"


def test_placeholder_replacement_is_identity_on_plain_text():
    """不含占位符的文本经过替换后逐字节不变（替换是幂等的 no-op）。"""
    from minimax_h3_prompt.director.prompts import (
        _apply_writing_discipline_placeholders,
    )

    sample = (
        "# 普通提示词\n\n"
        "- 这条纪律不含任何占位符，替换前后必须逐字节相同。\n"
        "- 单个花括号 {CAMERA_SPEED_DISCIPLINE} 不是占位符形态。\n"
    )
    assert _apply_writing_discipline_placeholders(sample) == sample


# #328：三条纪律（道具归属、跨门槛逐人、配乐同一支 cue 延续）的文本从导演台指令
# 文件搬进真源模块。五个常量名在这里点名写下（不用反射扫模块）：反射会把「常量改名」
# 也当成绿，而本票的 AC 是这五条具体文本必须存在。
THREE_DISCIPLINE_CONSTANTS = (
    "PROP_OWNERSHIP_DISCIPLINE",
    "THRESHOLD_CROSSING_DISCIPLINE_DIRECTOR",
    "THRESHOLD_CROSSING_DISCIPLINE_BASE",
    "MUSIC_CUE_DISCIPLINE_DIRECTOR",
    "MUSIC_CUE_DISCIPLINE_BASE",
)

# 导演台指令文件里那三处内联正文换成的占位符所指向的常量：导演台只吃「强化版」，
# 弱化版是给 base 两条写段路径的（#329／#330），本票只创建不接线。
DIRECTOR_DISCIPLINE_CONSTANTS = (
    "PROP_OWNERSHIP_DISCIPLINE",
    "THRESHOLD_CROSSING_DISCIPLINE_DIRECTOR",
    "MUSIC_CUE_DISCIPLINE_DIRECTOR",
)


def test_three_discipline_constants_are_non_empty_text():
    """五个新常量都要存在、非空、是纯文本（模块仍只含文本常量）。"""
    for name in THREE_DISCIPLINE_CONSTANTS:
        assert hasattr(wd, name), f"真源模块缺常量 {name}"
        value = getattr(wd, name)
        assert isinstance(value, str), f"{name} 不是文本"
        assert value.strip(), f"{name} 是空的——空串会让「在出站文本里」恒真"


def test_three_disciplines_reach_every_director_mode_from_the_source_module():
    """导演台出站文本仍含这三条纪律全文，且文本就是真源里那一份。

    这是「搬了家但漏了占位符」的守卫：指令文件里的内联正文已换成占位符，若
    ``_WRITING_DISCIPLINE_PLACEHOLDERS`` 漏了一个键，模型那一侧会收到
    ``{{...}}`` 字面量或整条纪律缺席。断言在**加载口返回值**（出站系统提示）上，
    不是模块常量自比自。先断言真源非空，否则清空常量的变异会被 ``"" in text``
    恒真骗过。
    """
    for name in DIRECTOR_DISCIPLINE_CONSTANTS:
        assert getattr(wd, name), f"{name} 为空——空串让 in 断言恒真，守卫会失效"
    for mode in WRITE_MODES:
        text = load_instruction(mode)
        for name in DIRECTOR_DISCIPLINE_CONSTANTS:
            assert getattr(wd, name) in text, f"{mode} 的写段指令缺真源 {name} 的全文"


def test_only_the_director_instruction_file_carries_placeholders():
    """提示词目录里只有导演台指令带占位符：别的文件没人替换，多一个就是出站字面量。"""
    offenders = sorted(
        str(path.relative_to(PROMPTS_DIR)).replace("\\", "/")
        for path in PROMPTS_DIR.rglob("*")
        if path.is_file() and "{{" in path.read_text(encoding="utf-8"))
    assert offenders == ["segment_writer_director.md"], \
        f"提示词目录里带占位符的文件应只有导演台指令，实际：{offenders}"
