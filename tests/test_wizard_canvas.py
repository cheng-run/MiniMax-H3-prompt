"""生图侧画布三步选择器（issue #42）：选比例→选尺寸→不符可自定义。

选择器是**薄接线**（spec 测试决策：向导/CLI/尺寸选择器全部薄接线，不设专用缝），
所以这里只钉它的输入→选定值契约，构图分套等纯函数层在 ``tests/test_canvas.py``。
断言落在 ``_choose_canvases`` 的返回值上：它是 Brief.canvases 的唯一生产者，
「选定值写进产物与请求」的物证链从这里起步。
"""
from __future__ import annotations

from types import SimpleNamespace

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.ui import wizard


def _run_selector(monkeypatch, inputs: list[str]) -> tuple[dict, list[str]]:
    """喂一串脚本输入给选择器，返回 (选定画布, 各次提问的提示语)。"""
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(prompts)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return wizard._choose_canvases(), asked


# ---------------------------------------------------------------------------
# 预设路径：选比例 → 选尺寸
# ---------------------------------------------------------------------------

def test_preset_ratio_then_preset_size_fills_the_only_family(monkeypatch):
    """现存唯一家族走一遍三步：zimage 取 1:2 档 640×1280（ADR 0013 Flux.2 全链退役）。

    期望值是**字面量**（不是从预设表算的）：表换了档位而选择器没跟上时这条会红。
    「不得再问 Flux.2」是退役的可证面（issue #48 AC1/AC5）：把 flux2 行放回家族表，
    这条会红——变异检验已实测。
    """
    canvases, asked = _run_selector(monkeypatch, ["1", "1"])

    assert canvases == {"zimage": (640, 1280)}
    assert any("Z-Image" in q for q in asked), "没按家族提问（Z-Image 缺席）"
    assert not any("Flux.2" in q or "flux2" in q for q in asked), \
        "Flux.2 已全链退役，向导不得再出现 Flux.2 字样提问"
    assert len([q for q in asked if "第 1 步" in q]) == 1, \
        "家族表应随退役收缩为一家——还在按多家提问"


def test_ratio_menu_shows_the_composition_family(monkeypatch, capsys):
    """比例菜单带画幅族标注——用户选比例时就看见构图词后果（近竖/近方/横幅）。"""
    _run_selector(monkeypatch, ["1", "1"])
    screen = capsys.readouterr().out
    for label in ("近竖", "近方", "横幅"):
        assert label in screen, f"比例菜单没标画幅族：{label}"


# ---------------------------------------------------------------------------
# 自定义兜底：选尺寸不符 → 自由输入；比例档没有的 → 自定义入口
# ---------------------------------------------------------------------------

def test_custom_size_is_free_input(monkeypatch):
    """尺寸档不符时的自定义是自由输入（AC1「自定义为自由输入，保留兜底」）。"""
    canvases, _ = _run_selector(monkeypatch, ["2", "3", "700x1300"])
    assert canvases["zimage"] == (700, 1300), "自定义尺寸没按自由输入落定"


def test_custom_ratio_entry_takes_free_dimensions(monkeypatch):
    """比例菜单的自定义入口直吃 宽x高（如 540x1260 = 9:21）：档外比例一步到位。

    自定义比例时构图词按画幅族就近归类是纯函数层的事（test_canvas.py），
    这里只钉「档外比例进得来」。
    """
    canvases, _ = _run_selector(monkeypatch, ["9", "540x1260"])
    assert canvases["zimage"] == (540, 1260), "自定义比例入口没吃到自由输入"


def test_final_echo_shows_the_actual_family_after_mismatched_custom(monkeypatch, capsys):
    """自定义尺寸不合所选比例时，回显按**实际落点**说话（issue #42 复审）。

    样本必须是**真翻族**：比例步选 16:9（第 8 档，横幅→通用的预告），尺寸步自定义
    700×1300（近竖→竖幅套的实际落点）。此前的样本 9:16→700×1300 两头都是近竖，
    预告与落点同族——「回显误按预设族而非最终宽高」这一可错形态测不红（复审点名）。
    """
    canvases, _ = _run_selector(monkeypatch, ["8", "3", "700x1300"])
    assert canvases["zimage"] == (700, 1300)
    screen = capsys.readouterr().out
    assert "7:13 · 700 × 1300（近竖）" in screen, "回显没有按实际落点展示画布"
    assert "构图用竖幅套" in screen, "回显没说清最终的构图词套（还停在横幅预告上）"


def test_times_sign_accepted_in_custom_input(monkeypatch):
    """自定义输入收 `700×1300`（乘号）——用户从产物上抄回来的就是乘号。"""
    canvases, _ = _run_selector(monkeypatch, ["9", "700×1300"])
    assert canvases["zimage"] == (700, 1300)


# ---------------------------------------------------------------------------
# 坏输入不静默：重问
# ---------------------------------------------------------------------------

def test_invalid_entries_reask_instead_of_silently_defaulting(monkeypatch):
    """0/99/乱码都重问，不静默落默认——画布是数据，错值必须当场拦。"""
    canvases, asked = _run_selector(monkeypatch, ["0", "x", "1", "99", "1"])
    assert canvases["zimage"] == (640, 1280), "坏输入后没回到正轨"
    ratio_asks = [q for q in asked if "第 1 步" in q]
    size_asks = [q for q in asked if "第 2 步" in q]
    assert len(ratio_asks) >= 3, "坏比例输入没有重问（静默吞了）"
    assert len(size_asks) >= 2, "坏尺寸输入没有重问（静默吞了）"


def test_custom_size_rejects_garbage_and_reasks(monkeypatch):
    """自定义尺寸的自由输入也校验格式：`宽六百` 重问，不静默回落默认表。"""
    canvases, asked = _run_selector(monkeypatch, ["1", "3", "宽六百", "700x1300"])
    assert canvases["zimage"] == (700, 1300)
    custom_asks = [q for q in asked if "自定义" in q and "宽" in q]
    assert len(custom_asks) >= 2, "坏自定义尺寸没有重问"


# ---------------------------------------------------------------------------
# 接线在：选择器的选定值真进 brief（否则脚本喂了答案也照样全绿）
# ---------------------------------------------------------------------------

def test_phase1_new_puts_the_selected_canvases_into_the_brief(tmp_path, monkeypatch):
    """``_phase1_new`` 真把选择器结果写进 Brief.canvases——「规则在、接线不在」的守卫。

    上面的用例只测选择器本身：把 ``_choose_canvases`` 从 ``_phase1_new`` 里删掉，
    它们照样绿（只是脚本输入多出四答没人吃）。这条钉的就是那一行接线。
    """
    # 主题/时长/风格/变体/画布两答（zimage 取 1:2 档；Flux.2 退役后只剩一家）
    inputs = iter(["主题", "", "", "1", "1", "1"])
    monkeypatch.setattr(wizard, "_prompt", lambda text, *a, **k: next(inputs))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_ask_clarifications", lambda config, topic: "")
    # 展示口不是本条的缝（另有出站用例钉它们），打成空操作免得空 bundle 去过解析。
    # 展示口不是本条的缝（另有出站用例钉它们），打成空操作免得空 bundle 去过解析。
    # 资产出站口已随 #40 回滚从首尾帧链移除，这里不再有第二个要打桩的展示口。
    monkeypatch.setattr(wizard, "_show_frame_prompts", lambda *a, **k: None)

    captured: dict = {}

    def fake_run_stage1(brief, config, **kwargs):
        captured["brief"] = brief
        return ({"fl2va_prompt_bundle": {}, "shot_table": "[Shot 1] 庭院"}, object(),
                {"frame_prompt_engineer": object()})

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)
    cfg = SimpleNamespace(default_duration=5.0, default_language="Chinese",
                          sessions_root=tmp_path / "sessions", max_clarification_rounds=0)
    wizard._phase1_new(cfg)

    assert captured["brief"].canvases == {"zimage": (640, 1280)}, \
        "选择器的选定值没进 brief——那一行接线丢了"


def test_asset_loop_outlet_forwards_the_selected_canvas(tmp_path, monkeypatch, capsys):
    """素材循环的单张出站口按 brief.canvases 渲染（issue #42 AC5 / #50 AC5）。

    #40 回滚后，向导里**唯一**的资产图出站口是多参素材循环（``_issue_asset_prompt``）；
    首尾帧链不再有资产出站口。这条守卫原先盯的是首尾帧链那两个调用点，随回滚改到
    真出口上：选定非默认画布时，打印出来的是选定尺寸，不是默认表。
    """
    from minimax_h3_prompt.asset_loop import QueueItem

    assert callable(wizard._issue_asset_prompt)
    monkeypatch.setattr(wizard, "_sync_released_asset_files", lambda *a, **k: None)
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot="主题",
                  canvases={"zimage": (700, 1300)})
    state = {
        "character_image_prompts": [
            {"name": "红裙女孩", "positive_prompt": "红裙女孩的生图提示词"},
        ],
    }
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()

    wizard._issue_asset_prompt(
        state, brief, generation_dir, QueueItem(picture=1, kind="character", name="红裙女孩"))

    out = capsys.readouterr().out
    assert "700" in out and "1300" in out, f"素材循环出站没按选定画布渲染：{out}"


def test_wizard_frame_renderer_shows_the_selected_canvas():
    """向导帧出站渲染器的画布行显示**选定值**（复审：它此前无直接断言）。

    这份渲染器直解 bundle（节点落盘时尺寸已按选定解析），画布行取 ``canvas_display``
    ——generation 侧渲染有测试、向导侧这份没有，「同取单一来源」在它身上是裸奔的。
    """
    from minimax_h3_prompt.generation import fl2va_bundle_from_dict
    from minimax_h3_prompt.ui.wizard import render_fl2va_frame_markdown_from_bundle

    payload = {
        "scene_anchor": "光斑",
        "first": {"zimage": {"positive_prompt": "水母群与光斑的第一帧画面"}},
        "continuity_constraints": ["保持同一片水母群"],
    }
    brief = Brief(variant="I2VA", duration=5, plot="漂浮的水母群与光斑",
                  canvases={"zimage": (700, 1300)})
    bundle = fl2va_bundle_from_dict(payload, brief)

    rendered = render_fl2va_frame_markdown_from_bundle(bundle, "first")
    assert "画布：`7:13 · 700 × 1300（近竖）`" in rendered, "向导帧渲染器没显示选定画布"
    assert "推荐尺寸" not in rendered, "「推荐尺寸」措辞在向导帧渲染器里还活着"
