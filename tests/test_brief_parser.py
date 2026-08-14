"""brief_parser 单测：输入/模型键、参考图段、行内注释。"""
from minimax_h3_prompt.brief_parser import brief_uses_refs, min_refs, parse_brief_text


def test_model_fl2va():
    b = parse_brief_text("模型: fl2va\n## 剧情\n首尾帧")
    assert b.mode == "base" and b.variant == "FL2VA"
    assert brief_uses_refs(b)


def test_model_ref2va():
    b = parse_brief_text("模型: ref2va\n## 剧情\n多参考")
    assert b.mode == "ref"
    assert brief_uses_refs(b)


def test_legacy_mode_variant():
    b = parse_brief_text("模式: base\n变体: T2VA\n## 剧情\n纯文字")
    assert b.mode == "base" and b.variant == "T2VA"
    assert not brief_uses_refs(b)


def test_model_overrides_mode():
    # 模型键优先：即使后面又写了模式，也不覆盖
    b = parse_brief_text("模型: fl2va\n模式: ref\n## 剧情\nx")
    assert b.mode == "base" and b.variant == "FL2VA"


def test_refs_section():
    b = parse_brief_text(
        "模型: fl2va\n## 剧情\nx\n## 参考图\n"
        "- Picture 1: 首帧 — 仙师缓抬手\n- Picture 2: 尾帧 — 青年行礼"
    )
    assert [(r.picture, r.name) for r in b.refs] == [(1, "首帧"), (2, "尾帧")]


def test_inline_comment_stripped():
    b = parse_brief_text("模式: ref # 这是注释\n## 剧情\nx")
    assert b.mode == "ref"


def test_duration_non_numeric_ok():
    b = parse_brief_text("时长: unknown\n## 剧情\nx")
    assert b.duration == 5.0  # 默认值


def test_input_i2va():
    b = parse_brief_text("输入: 首帧\n## 剧情\nx")
    assert b.mode == "base" and b.variant == "I2VA"
    assert min_refs(b) == 1


def test_input_l2va():
    b = parse_brief_text("输入: 尾帧\n## 剧情\nx")
    assert b.variant == "L2VA" and min_refs(b) == 1


def test_input_fl2va():
    b = parse_brief_text("输入: 首尾帧\n## 剧情\nx")
    assert b.variant == "FL2VA" and min_refs(b) == 2


def test_input_ref2va():
    b = parse_brief_text("输入: 多参考\n## 剧情\nx")
    assert b.mode == "ref" and min_refs(b) == 1


def test_input_overrides_model():
    # 输入键优先于模型键
    b = parse_brief_text("模型: fl2va\n输入: 尾帧\n## 剧情\nx")
    assert b.variant == "L2VA"


def test_ref_path_parsing():
    b = parse_brief_text(
        "输入: 首帧\n## 剧情\nx\n## 参考图\n"
        "- Picture 1: 自拍首帧 — 女孩微笑  (/mnt/d/images/selfie.png)"
    )
    assert b.refs[0].path == "/mnt/d/images/selfie.png"
    assert b.refs[0].description == "女孩微笑"


def test_ref_without_desc_separator():
    # 允许没有「— 描述」，只有名称 + 路径
    b = parse_brief_text("## 参考图\n- Picture 1: 首帧  (/mnt/d/a.png)")
    assert b.refs[0].name == "首帧"
    assert b.refs[0].description == ""
    assert b.refs[0].path == "/mnt/d/a.png"


def test_windows_path_normalized():
    # Windows 路径（引号 + 反斜杠）自动转 WSL：D:\ → /mnt/d/
    line = '- Picture 1: 自拍首帧 ("D:\\Comfyui\\output\\图片\\a.png")'
    b = parse_brief_text("输入: 首帧\n## 剧情\nx\n## 参考图\n" + line)
    assert b.refs[0].name == "自拍首帧"
    assert b.refs[0].path == "/mnt/d/Comfyui/output/图片/a.png"
