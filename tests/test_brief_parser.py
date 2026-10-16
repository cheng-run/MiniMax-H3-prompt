"""brief_parser 单测：输入/模型键、参考图段、行内注释、画布键。"""
import pytest

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


def test_input_duocan_alias():
    # 「多参」是术语正名（issue #41），「多参考」旧写法继续可解析
    b = parse_brief_text("输入: 多参\n## 剧情\nx")
    assert b.mode == "ref" and min_refs(b) == 1


def test_ref_mode_normalizes_variant_no_matter_key_order():
    # 「变体」写在「输入: 多参」之前也不许把帧变体带进 ref 管线（issue #41 复审：
    # 帧链只判 variant，残留 FL2VA 会让 ref 会话照跑关键帧节点、产物按 FL2VA 形状走）
    b = parse_brief_text("变体: FL2VA\n输入: 多参\n## 剧情\nx")
    assert b.mode == "ref" and b.variant == "T2VA"


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


# ---------------------------------------------------------------------------
# 画布键（issue #42）：用户选定尺寸进 brief，随请求与产物同取
# ---------------------------------------------------------------------------

def test_canvas_key_per_family():
    """`画布 <家族>: WxH` 按家族选定——构图词分套跟着各家族的画幅族走。"""
    b = parse_brief_text("画布 zimage: 640x1280\n画布 flux2: 1024x1024\n## 剧情\nx")
    assert b.canvases == {"zimage": (640, 1280), "flux2": (1024, 1024)}


def test_canvas_key_bare_applies_to_every_family():
    """裸 `画布: WxH` 是快捷式：一份选定套用到全部家族（spec 测试缝「用户选定尺寸」的形态）。"""
    b = parse_brief_text("画布: 700x1300\n## 剧情\nx")
    assert b.canvases["zimage"] == (700, 1300) and b.canvases["flux2"] == (700, 1300)


def test_canvas_per_family_overrides_bare():
    """家族行覆盖快捷式：先给全局、再单独改一个家族，各得其所。"""
    b = parse_brief_text("画布: 700x1300\n画布 flux2: 1024x1024\n## 剧情\nx")
    assert b.canvases == {"zimage": (700, 1300), "flux2": (1024, 1024)}


def test_canvas_value_accepts_times_sign():
    """`640×1280`（乘号）与 `640x1280` 等价——用户从渲染层抄回来的就是乘号。"""
    b = parse_brief_text("画布 zimage: 640×1280\n## 剧情\nx")
    assert b.canvases["zimage"] == (640, 1280)


def test_canvas_malformed_value_is_loud():
    """画布是精确数据不是自由文本：值坏了必须报错，不许静默回落默认表。

    静默回落的后果是「用户以为选了 700×1300、实际按默认出图」——本仓「绝不静默
    失败」要消灭的正是这种形态。
    """
    with pytest.raises(ValueError):
        parse_brief_text("画布 zimage: 宽六百\n## 剧情\nx")
    with pytest.raises(ValueError):
        parse_brief_text("画布: -10x20\n## 剧情\nx")


def test_canvas_unknown_family_is_loud():
    """`画布 zimge:` 这类拼写错误当场报错——静默忽略＝画布悄悄回落默认表。"""
    with pytest.raises(ValueError):
        parse_brief_text("画布 zimge: 700x1300\n## 剧情\nx")


def test_canvas_key_without_space_still_reads():
    """`画布zimage:`（缺空格）是手误不是别的键——照样读，不许静默忽略（复审点名）。

    键形态拼错曾不进画布分支、无声无息地让选定作废；家族名/值都响亮，键形态也得响亮。
    """
    b = parse_brief_text("画布zimage: 700x1300\n## 剧情\nx")
    assert b.canvases == {"zimage": (700, 1300)}


def test_canvas_key_forms_all_read():
    """键形态全家桶（复审枚举）：英文半边、下划线分隔、行首列表符都读入。

    英文 `canvas` 半边曾零覆盖——删掉英文分支全套件照绿，而 `canvas zimage:` 全部
    退回静默回落（变异实测）。这里把两个半边都钉上。
    """
    for line in ("canvas zimage: 700x1300", "canvaszimage: 700x1300",
                 "Canvas_Zimage: 700x1300", "- 画布 zimage: 700x1300",
                 "1. 画布 zimage: 700x1300", "+ 画布 zimage: 700x1300", "> 画布 zimage: 700x1300"):
        b = parse_brief_text(f"{line}\n## 剧情\nx")
        assert b.canvases == {"zimage": (700, 1300)}, f"键形态没读入：{line!r}"


def test_canvas_empty_value_is_loud_not_skipped():
    """`画布 zimage:`（值为空）是坏值不是「没写」——报错，不许被通用空值跳过吞掉。

    复审实测：通用 `if not value: continue` 先于画布分支执行，整套「坏值大声报错」
    机制被绕过，用户以为选了、实际回落默认表。
    """
    with pytest.raises(ValueError):
        parse_brief_text("画布 zimage:\n## 剧情\nx")


def test_canvas_unparseable_line_is_loud():
    """画布行长得像画布行却解析不了（`=` 分隔、缺分隔符，含英文半边与列表符变体）当场报。

    英文半边曾零覆盖：探针去掉 `canvas` 前缀后全套照绿（复审变异实测）。
    """
    for line in ("画布zimage=700x1300", "画布zimage 700x1300",
                 "canvaszimage=700x1300", "canvas zimage 700x1300",
                 "1. 画布zimage=700x1300"):
        with pytest.raises(ValueError):
            parse_brief_text(f"{line}\n## 剧情\nx")
