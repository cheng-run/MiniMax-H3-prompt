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

def test_preset_ratio_then_preset_size_fills_both_families(monkeypatch):
    """两家族各走一遍三步：zimage 取 1:2 档 640×1280，flux2 取 1:1 档 1024×1024。

    期望值是**字面量**（不是从预设表算的）：表换了档位而选择器没跟上时这条会红。
    家族顺序取生图默认表的遍历序——与【画布】块的行序同源。
    """
    canvases, asked = _run_selector(monkeypatch, ["1", "1", "6", "1"])

    assert canvases == {"zimage": (640, 1280), "flux2": (1024, 1024)}
    assert any("Z-Image" in q for q in asked), "没按家族提问（Z-Image 缺席）"
    assert any("Flux.2" in q for q in asked), "没按家族提问（Flux.2 缺席）"


def test_ratio_menu_shows_the_composition_family(monkeypatch, capsys):
    """比例菜单带画幅族标注——用户选比例时就看见构图词后果（近竖/近方/横幅）。"""
    _run_selector(monkeypatch, ["1", "1", "1", "1"])
    screen = capsys.readouterr().out
    for label in ("近竖", "近方", "横幅"):
        assert label in screen, f"比例菜单没标画幅族：{label}"


# ---------------------------------------------------------------------------
# 自定义兜底：选尺寸不符 → 自由输入；比例档没有的 → 自定义入口
# ---------------------------------------------------------------------------

def test_custom_size_is_free_input(monkeypatch):
    """尺寸档不符时的自定义是自由输入（AC1「自定义为自由输入，保留兜底」）。"""
    canvases, _ = _run_selector(monkeypatch, ["2", "3", "700x1300", "1", "1"])
    assert canvases["zimage"] == (700, 1300), "自定义尺寸没按自由输入落定"


def test_custom_ratio_entry_takes_free_dimensions(monkeypatch):
    """比例菜单的自定义入口直吃 宽x高（如 540x1260 = 9:21）：档外比例一步到位。

    自定义比例时构图词按画幅族就近归类是纯函数层的事（test_canvas.py），
    这里只钉「档外比例进得来」。
    """
    canvases, _ = _run_selector(monkeypatch, ["9", "540x1260", "1", "1"])
    assert canvases["zimage"] == (540, 1260), "自定义比例入口没吃到自由输入"


def test_final_echo_shows_the_actual_family_after_mismatched_custom(monkeypatch, capsys):
    """自定义尺寸不合所选比例时，回显按**实际落点**说话（issue #42 复审）。

    样本必须是**真翻族**：比例步选 16:9（第 8 档，横幅→通用的预告），尺寸步自定义
    700×1300（近竖→竖幅套的实际落点）。此前的样本 9:16→700×1300 两头都是近竖，
    预告与落点同族——「回显误按预设族而非最终宽高」这一可错形态测不红（复审点名）。
    """
    canvases, _ = _run_selector(monkeypatch, ["8", "3", "700x1300", "1", "1"])
    assert canvases["zimage"] == (700, 1300)
    screen = capsys.readouterr().out
    assert "7:13 · 700 × 1300（近竖）" in screen, "回显没有按实际落点展示画布"
    assert "构图用竖幅套" in screen, "回显没说清最终的构图词套（还停在横幅预告上）"


def test_times_sign_accepted_in_custom_input(monkeypatch):
    """自定义输入收 `700×1300`（乘号）——用户从产物上抄回来的就是乘号。"""
    canvases, _ = _run_selector(monkeypatch, ["9", "700×1300", "1", "1"])
    assert canvases["zimage"] == (700, 1300)


# ---------------------------------------------------------------------------
# 坏输入不静默：重问
# ---------------------------------------------------------------------------

def test_invalid_entries_reask_instead_of_silently_defaulting(monkeypatch):
    """0/99/乱码都重问，不静默落默认——画布是数据，错值必须当场拦。"""
    canvases, asked = _run_selector(monkeypatch, ["0", "x", "1", "99", "1", "1", "1"])
    assert canvases["zimage"] == (640, 1280), "坏输入后没回到正轨"
    ratio_asks = [q for q in asked if "第 1 步" in q]
    size_asks = [q for q in asked if "第 2 步" in q]
    assert len(ratio_asks) >= 3, "坏比例输入没有重问（静默吞了）"
    assert len(size_asks) >= 2, "坏尺寸输入没有重问（静默吞了）"


def test_custom_size_rejects_garbage_and_reasks(monkeypatch):
    """自定义尺寸的自由输入也校验格式：`宽六百` 重问，不静默回落默认表。"""
    canvases, asked = _run_selector(monkeypatch, ["1", "3", "宽六百", "700x1300", "1", "1"])
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
    # 主题/时长/风格/变体/画布四答（zimage 取 1:2 档，flux2 取 1:1 档）
    inputs = iter(["主题", "", "", "1", "1", "1", "6", "1"])
    monkeypatch.setattr(wizard, "_prompt", lambda text, *a, **k: next(inputs))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_ask_clarifications", lambda config, topic: "")
    # 展示口不是本条的缝（另有出站用例钉它们），打成空操作免得空 bundle 去过解析。
    monkeypatch.setattr(wizard, "_show_frame_prompts", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_show_asset_prompts", lambda *a, **k: None)

    captured: dict = {}

    def fake_run_stage1(brief, config, **kwargs):
        captured["brief"] = brief
        return ({"fl2va_prompt_bundle": {}, "shot_table": "[Shot 1] 庭院"}, object(),
                {"frame_prompt_engineer": object()})

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)
    cfg = SimpleNamespace(default_duration=5.0, default_language="Chinese",
                          sessions_root=tmp_path / "sessions", max_clarification_rounds=0)
    wizard._phase1_new(cfg)

    assert captured["brief"].canvases == {"zimage": (640, 1280), "flux2": (1024, 1024)}, \
        "选择器的选定值没进 brief——那一行接线丢了"


def test_both_asset_outlet_call_sites_forward_the_canvases(tmp_path, monkeypatch):
    """向导两个资产出站口都把 brief.canvases 递进去（复审：删第三参后全套照绿）。

    「规则在、接线不在」的守卫：``_show_asset_prompts`` 自己认 canvases，但修改循环与
    阶段 2 收尾两个**调用点**不传的话，选定非默认画布时打印与落盘照样回落默认尺寸。
    用探针包住出站口记录实参，两条流程各走一遍。
    """
    seen: list = []
    monkeypatch.setattr(wizard, "_show_frame_prompts", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_show_asset_prompts",
                        lambda state, directory, canvases=None: seen.append(canvases))
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    brief = Brief(mode="base", variant="I2VA", duration=5.0, plot="主题",
                  canvases={"zimage": (700, 1300), "flux2": (1280, 720)})
    state = {"fl2va_prompt_bundle": {}, "shot_table": "[Shot 1] 庭院"}
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    cfg = SimpleNamespace(default_duration=5.0, default_language="Chinese",
                          sessions_root=tmp_path / "sessions", max_clarification_rounds=0)

    # 流程一：修改循环的循环顶出站口
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    wizard._user_revision_loop(state, generation_dir, brief, cfg)

    # 流程二：阶段 2 收尾的补展示出站口——展示后收帧图路径，用哨兵异常跳出
    class _Stop(Exception):
        pass

    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: (_ for _ in ()).throw(_Stop()))
    from minimax_h3_prompt.session_store import SessionState
    session = SessionState(generation_dir, brief, state, "awaiting_frames")
    try:
        wizard._phase2_collect_and_finish(cfg, session, show_frame_prompts=True)
    except _Stop:
        pass

    assert seen == [brief.canvases, brief.canvases], \
        f"两个出站口没都递到画布：{seen}"


def test_wizard_frame_renderer_shows_the_selected_canvas():
    """向导帧出站渲染器的画布行显示**选定值**（复审：它此前无直接断言）。

    这份渲染器直解 bundle（节点落盘时尺寸已按选定解析），画布行取 ``canvas_display``
    ——generation 侧渲染有测试、向导侧这份没有，「同取单一来源」在它身上是裸奔的。
    """
    from minimax_h3_prompt.generation import fl2va_bundle_from_dict
    from minimax_h3_prompt.ui.wizard import render_fl2va_frame_markdown_from_bundle

    payload = {
        "scene_anchor": "光斑",
        "first": {"zimage": {"positive_prompt": "水母群与光斑的第一帧画面"},
                  "flux2": {"positive_prompt": "水母群与光斑的第一帧画面"}},
        "continuity_constraints": ["保持同一片水母群"],
    }
    brief = Brief(variant="I2VA", duration=5, plot="漂浮的水母群与光斑",
                  canvases={"zimage": (700, 1300), "flux2": (1280, 720)})
    bundle = fl2va_bundle_from_dict(payload, brief)

    rendered = render_fl2va_frame_markdown_from_bundle(bundle, "first")
    assert "画布：`7:13 · 700 × 1300（近竖）`" in rendered, "向导帧渲染器没显示选定画布"
    assert "推荐尺寸" not in rendered, "「推荐尺寸」措辞在向导帧渲染器里还活着"
