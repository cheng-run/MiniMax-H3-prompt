"""生图画布：画幅族分套、显示名、用户选定尺寸（issue #42）。

画布改由用户选定（选比例→选尺寸→不符可自定义）后，构图词分套不再绑模型身份、
而是绑**画幅族**（Q21(c) 裁定：族内就近归类、族外降通用）。本文件钉住三件外部行为：

1. **分套判定**：近竖→竖幅套、近方→正方套、横幅与极端→通用构图词——
   票面点名的样本（3:4/2:3 近竖、9:21 极端、横幅不配竖幅词）与起草裁定的
   边界样本（4:5 构图语义接近竖幅）都在这里；
2. **显示名**：画布显示名与尺寸的唯一格式化口，请求与渲染层同取（AC3）；
3. **读取口**：用户选定优先、未选回落 profile 默认表（AC1 的「保留兜底」）。
"""
from __future__ import annotations

from math import gcd

import pytest

from minimax_h3_prompt.generation import (
    CANVAS_RATIO_PRESETS,
    IMAGE_PROFILE_DEFAULTS,
    ImageProfile,
    canvas_display,
    composition_set,
    image_canvas_size,
)


# ---------------------------------------------------------------------------
# 1. 构图词分套：族内就近归类，族外降通用（Q21(c)）
# ---------------------------------------------------------------------------

# (宽, 高, 期望的构图词套)。期望值来自票面与起草裁定的**点名样本**，不从实现推导：
# - 3:4、2:3 是起草 Q21 选项里点名的「近竖」；1:2 是 zimage 原生画布，属竖幅族；
# - 4:5 起草裁定原话「4:5 的构图语义确实接近竖幅」→ 竖幅套（最贴近边界的样本）；
# - 9:21 是票面点名的极端比例；21:9 是它的横侧镜像；
# - 16:9 是 AC2「横幅画面配竖幅构图词」那句里的横幅；5:4 是横侧近方——
#   起草裁定「横幅硬塞任一套都会说错话」，故它也不进正方套。
COMPOSITION_CASES = (
    (768, 1024, "竖幅套"),    # 3:4
    (832, 1248, "竖幅套"),    # 2:3
    (576, 1024, "竖幅套"),    # 9:16
    (640, 1280, "竖幅套"),    # 1:2（zimage 默认画布）
    (1024, 1280, "竖幅套"),   # 4:5 ← 起草裁定边界样本
    (1024, 1024, "正方套"),   # 1:1
    (960, 1152, "正方套"),    # 5:6（近方竖侧）
    (540, 1260, "通用"),      # 9:21 极端竖（票面点名）
    (1260, 540, "通用"),      # 21:9 极端横
    (1280, 720, "通用"),      # 16:9 横幅
    (1280, 1024, "通用"),     # 5:4 横侧近方——横幅不硬塞任一套
    (1024, 768, "通用"),      # 4:3 横幅
)


@pytest.mark.parametrize("width,height,expected", COMPOSITION_CASES)
def test_composition_set_follows_the_aspect_family(width, height, expected):
    """构图词套由画布比例族决定，不绑模型身份。

    这是 Q21(c) 的可证面：用户给 Z-Image 选了近方形比例，落笔就要用正方套；
    横幅与极端比例降为与画幅无关的通用构图词，不硬塞竖幅/正方任一套——
    「横幅画面配竖幅构图词」的说反话产出在分套这一层就被堵死。
    """
    assert composition_set(width, height) == expected


def test_composition_set_never_mixes_the_two_specialized_sets():
    """竖/方两套不互换：同一批画布里，竖幅族样本不出正方套、正方族样本不出竖幅套。

    单看逐例断言可能巧合全绿，这里显式收一次「族→套」的对照面：
    竖幅族（[1.25, 2.0]）恒映射竖幅套、正方族（[1, 1.25)）恒映射正方套。
    """
    portrait_family = [(4, 5), (3, 4), (2, 3), (9, 16), (1, 2)]
    square_family = [(1, 1), (5, 6), (6, 7)]
    for w_ratio, h_ratio in portrait_family:
        assert composition_set(64 * w_ratio, 64 * h_ratio) == "竖幅套", (w_ratio, h_ratio)
    for w_ratio, h_ratio in square_family:
        assert composition_set(64 * w_ratio, 64 * h_ratio) == "正方套", (w_ratio, h_ratio)


# ---------------------------------------------------------------------------
# 2. 画布显示名：单一来源，请求与渲染层同取（AC3）
# ---------------------------------------------------------------------------

# (宽, 高, 期望显示串)。显示名=约分后的比例；括号里是**画幅族类**（分套依据，非朝向）。
DISPLAY_CASES = (
    (640, 1280, "1:2 · 640 × 1280（近竖）"),
    (576, 1024, "9:16 · 576 × 1024（近竖）"),
    (1024, 1024, "1:1 · 1024 × 1024（近方）"),
    (1280, 720, "16:9 · 1280 × 720（横幅）"),
)


@pytest.mark.parametrize("width,height,expected", DISPLAY_CASES)
def test_canvas_display_is_the_one_format_for_name_and_size(width, height, expected):
    """显示名与尺寸只在一处格式化：比例约分 + 尺寸 + 画幅族类。

    改动前请求块（nodes._canvas_block）与渲染层（「推荐尺寸」行）各写一份排版，
    正是 AC3 点名的漂移温床；现在两边都调这一个口。族类标签与行尾分套指示同源，
    不会出「（竖幅）；构图用正方套」这种自相矛盾的行。
    """
    assert canvas_display(width, height) == expected


def test_canvas_display_labels_extreme_ratios_as_extreme():
    """极端比例标「极端」不标朝向——9:21 竖着但不近竖，标签就是分套依据本身。"""
    assert "（极端）" in canvas_display(540, 1260)   # 9:21
    assert "（极端）" in canvas_display(1260, 540)   # 21:9


# ---------------------------------------------------------------------------
# 3. 读取口：用户选定优先，未选回落默认表（AC1）
# ---------------------------------------------------------------------------

def test_image_canvas_size_prefers_the_user_selection(monkeypatch):
    """选定值是数据不是默认值的装饰：传了就用它，不用 profile 默认表。

    「没选的家族不能跟着别的家族跑」用临时加的第二家族验证（Flux.2 退役后只剩一家，
    原样本是 flux2；家族清单从表派生，测试加行与生产加行同一条路）。
    """
    assert image_canvas_size("zimage", {"zimage": (700, 1300)}) == (700, 1300)
    monkeypatch.setitem(IMAGE_PROFILE_DEFAULTS, "zimage_wide",
                        ImageProfile("zimage_wide_v1", "12", 768, 768, "candidate"))
    assert image_canvas_size("zimage_wide", {"zimage": (700, 1300)}) == (768, 768), \
        "没选的家族必须回落默认表，不能跟着别的家族跑"


def test_image_canvas_size_falls_back_to_the_profile_table():
    """未选（含旧会话、--brief 无画布键）时行为与改动前逐字一致——兜底不是摆设。"""
    assert image_canvas_size("zimage") == (
        IMAGE_PROFILE_DEFAULTS["zimage"].width, IMAGE_PROFILE_DEFAULTS["zimage"].height)


def test_image_canvas_size_rejects_unknown_family():
    """表外家族照旧报错；flux2 随全链退役进「未知生图模型」档（issue #48 AC6）。

    读取口与 brief 画布键同款报错：家族合法性先于选定值，带了选定值也照报——
    「画布 flux2: …」与 `image_canvas_size("flux2", …)` 都不得静默回落默认表。
    """
    with pytest.raises(ValueError):
        image_canvas_size("sdxl")
    with pytest.raises(ValueError):
        image_canvas_size("sdxl", {"sdxl": (512, 512)})
    with pytest.raises(ValueError, match="未知生图模型"):
        image_canvas_size("flux2")
    with pytest.raises(ValueError, match="未知生图模型"):
        image_canvas_size("flux2", {"flux2": (512, 512)})


def test_ratio_preset_sizes_match_their_ratio_exactly():
    """比例档下的每个预设尺寸都要与档名**逐档约分一致**（表完整性守卫）。

    菜单显示「9:16」档却给出 4:5 的尺寸，用户按显示选、拿到的东西对不上号——
    这类错位在菜单文本里看不出来，只能从表的不变量咬。
    """
    for preset in CANVAS_RATIO_PRESETS:
        # 档名与比例份数也要一致：菜单按 name 显示、尺寸按 sizes 给——名字写 9:16
        # 比例却配 4:5 时，用户按名字选、拿到的对不上号（复审：名↔比例曾不钉）。
        assert preset.name == f"{preset.w_ratio}:{preset.h_ratio}", \
            f"比例档名与份数不符：{preset.name} ≠ {preset.w_ratio}:{preset.h_ratio}"
        for width, height in preset.sizes:
            divisor = gcd(width, height)
            assert (width // divisor, height // divisor) == (preset.w_ratio, preset.h_ratio), \
                f"{preset.name} 档的 {width}×{height} 不是 {preset.name} 比例"
        assert preset.sizes, f"{preset.name} 档没有预设尺寸"


def test_composition_hints_have_their_definitions_in_the_wordlist():
    """请求行尾的分套指示与生图词表真源对得上：指示里出现的词套，词表里必须有定义。

    指示说「构图用竖幅套」而词表里没有「竖幅套」这一套的写法——指针悬空，模型只能
    猜。这是两个**独立来源**（``generation.composition_hint`` 与
    ``film-language-image.md``）之间的到达性契约，不是同义反复。
    """
    import re

    from minimax_h3_prompt.film_language import image_source_text

    source = image_source_text()
    for needle in ("竖幅套", "正方套", "通用构图词", "族外降通用"):
        assert needle in source, f"生图词表缺「{needle}」——分套指示没有落点"
    # 映射**方向**也钉住：词表的族→套对照表与 `_COMPOSITION_SET_BY_FAMILY` 同向。
    # 只钉词的存在时，表格写反（近竖→正方套）照样绿——两处漂了没人知道（复审点名）。
    # 模式锚在表格行的第三列上，正文里同向的散句不给假绿。
    assert re.search(r"\*\*近竖\*\*\s*\|[^\n]*\|\s*竖幅套\s*\|", source), \
        "词表对照表的近竖→竖幅套没了/反了"
    assert re.search(r"\*\*近方\*\*\s*\|[^\n]*\|\s*正方套\s*\|", source), \
        "词表对照表的近方→正方套没了/反了"
    # 措辞纪律（AC4）也在真源里活着：不写比例数字、只写构图关系。
    assert "不写比例数字" in source and "构图关系" in source


def test_image_wordlist_uses_aspect_family_wording_without_model_pairs():
    """生图词表按画幅族口径表述（issue #48 AC4）：「按模型分两套」旧叙述已更正。

    正方构图套必须**仍在**（它绑画幅族不绑模型，ADR 0013 明文不跟着删）；
    「多模型版本分别重写」的双模型纪律与 Flux.2 字样不得再出现在真源里——真源是
    逐字喂给模型的，带着它就等于把退役模型重新引进提示词。变异检验：把旧叙述或
    Flux.2 字样放回词表，这条会红（已实测）。
    """
    from minimax_h3_prompt.film_language import image_source_text

    source = image_source_text()
    assert "正方套" in source, "正方构图套必须保留（绑画幅族不绑模型）"
    assert "分别重写" not in source, "「多模型分别重写」旧叙述应更正为画幅族口径"
    assert "Flux.2" not in source and "flux.2" not in source.lower(), \
        "生图词表真源不得再出现 Flux.2 字样"


# ---------------------------------------------------------------------------
# 4. 选定值的留痕与传递链（issue #42 复审：漏一跳就是「规则在、接线不在」）
# ---------------------------------------------------------------------------

def test_brief_snapshot_records_the_selected_canvases():
    """generation.json 的 brief 快照要记选定画布——它也是逐字段显式清单（复审点名）。

    不记的话，产物是按 700×1300 出的、快照里却没有这个选择，事后对不上账。
    """
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.generation import brief_snapshot

    brief = Brief(mode="base", variant="I2VA", plot="主题", canvases={"zimage": (700, 1300)})
    snapshot = brief_snapshot(brief)
    assert snapshot["canvases"] == {"zimage": [700, 1300]}, "brief 快照丢了画布字段"


def test_create_video_from_topic_puts_canvases_into_the_brief(tmp_path, monkeypatch):
    """topic 入口自建 Brief——canvases 不递进来就是静默回落系统默认。"""
    from types import SimpleNamespace

    from minimax_h3_prompt import topic_generation

    config = SimpleNamespace(sessions_root=tmp_path / "sessions", default_duration=5.0,
                             default_style="", default_language="Chinese")
    captured: dict = {}

    def fake_run(brief, config, **kwargs):
        captured["canvases"] = brief.canvases
        raise RuntimeError("到此为止")  # 不真跑管线

    monkeypatch.setattr(topic_generation, "run_pipeline_structured", fake_run)
    with pytest.raises(RuntimeError):
        topic_generation.create_video_from_topic("主题", config, canvases={"zimage": (700, 1300)})
    assert captured["canvases"] == {"zimage": (700, 1300)}, "topic 入口丢了画布"


def test_cli_brief_path_forwards_canvases_to_generation(tmp_path, monkeypatch):
    """CLI --brief 路径把解析出的画布**递到**生成入口（复审实测的丢参现场）。

    main._run_generate_prompts 曾解析出 brief.canvases 却在最后一跳不传——
    brief 文件里的选定画布静默作废，正是「规则在、接线不在」族的又一现场。
    """
    import argparse

    from minimax_h3_prompt.main import _run_generate_prompts

    brief_file = tmp_path / "brief.md"
    brief_file.write_text("画布 zimage: 700x1300\n## 剧情\n主题", encoding="utf-8")
    forwarded: dict = {}

    def fake_create(topic, config, **kwargs):
        forwarded.update(kwargs)
        raise RuntimeError("到此为止")

    monkeypatch.setattr("minimax_h3_prompt.topic_generation.create_video_from_topic", fake_create)
    args = argparse.Namespace(brief=str(brief_file), variant=None, dry_run=False,
                              topic=None, duration=None, style=None, language=None)
    with pytest.raises(RuntimeError):
        _run_generate_prompts(args)
    assert forwarded.get("canvases") == {"zimage": (700, 1300)}, \
        "CLI --brief 路径没把画布递到生成入口"
