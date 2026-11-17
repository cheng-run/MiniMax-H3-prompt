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


def test_only_the_director_instruction_file_carries_placeholders():
    """提示词目录里只有导演台指令带占位符：别的文件没人替换，多一个就是出站字面量。"""
    offenders = sorted(
        str(path.relative_to(PROMPTS_DIR)).replace("\\", "/")
        for path in PROMPTS_DIR.rglob("*")
        if path.is_file() and "{{" in path.read_text(encoding="utf-8"))
    assert offenders == ["segment_writer_director.md"], \
        f"提示词目录里带占位符的文件应只有导演台指令，实际：{offenders}"
