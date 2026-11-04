"""导演台成片画幅的单一真源（#161，ADR 0028）。

为什么单独立用例：这一票要治的正是「两处口径」——向导问的画幅与导演包／节点声明的
画幅各说各话（2026-11-04 实跑：包写 736×1280、节点在跑 864×480）。判据全在这一个
模块里，用例就钉三件事：**32 对齐**（插件后端会拒绝非 32 倍数的画布）、**关键帧画布
从成片画幅派生**（同一份答案的两处消费）、**包参数与节点交代同取这一份答案**。
"""
from __future__ import annotations

from types import SimpleNamespace

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.director.film_canvas import (
    film_frame_canvases,
    film_settings,
    film_size_for,
    node_setup_lines,
    snap_film_size,
)
from minimax_h3_prompt.director.pack import DirectorPackSettings
from minimax_h3_prompt.ui import wizard

SETTINGS = DirectorPackSettings(
    width=736, height=1280, ref_max_size=864, ref_image_size="match")


def _brief(canvases: dict[str, tuple[int, int]] | None) -> Brief:
    return Brief(mode="director", variant="T2VA", director_mode="fl2v",
                 duration=20.0, style="", language="Chinese", plot="题目",
                 canvases=canvases or {})


def test_snap_rounds_to_the_plugin_grid_half_up():
    """1920×1080 与 720×1280 都要落到 32 的倍数上，且就近取整（720 不许被银行家
    舍入拉去 704）。"""
    assert snap_film_size(1920, 1080) == (1920, 1088)
    assert snap_film_size(720, 1280) == (736, 1280)
    assert snap_film_size(640, 1280) == (640, 1280), "本来就对齐的尺寸不许被挪"
    assert snap_film_size(1, 31) == (32, 32), "低于一格也要抬到最小可执行尺寸"


def test_frame_canvas_is_derived_from_the_film_answer():
    """关键帧画布＝成片画幅同尺寸，并挂在**当前关键帧模型**的家族键上。

    这是「只问一次」的落点：用户答的是成片画幅，帧请求拿到的画布是它派生的，
    不再有第二道画布问答。
    """
    assert film_frame_canvases((1920, 1088), "qwen") == {"qwen": (1920, 1088)}
    # 旧键位按别名归一（``zimage``→``z_image``），不另立一份键。
    assert film_frame_canvases((640, 1280), "zimage") == {"z_image": (640, 1280)}


def test_film_settings_takes_width_height_from_the_answer_only():
    """包参数里的 width/height 换成成片画幅；ref 尺寸仍取配置那一份。"""
    brief = _brief({"qwen": (1920, 1088)})

    settings = film_settings(SETTINGS, brief, frame_model="qwen")

    assert (settings.width, settings.height) == (1920, 1088)
    assert settings.ref_max_size == SETTINGS.ref_max_size
    assert settings.ref_image_size == SETTINGS.ref_image_size


def test_film_settings_falls_back_for_old_sessions():
    """旧会话没有成片画幅答案（画布表没有本档键）时，沿用配置声明——不抛错、不猜。"""
    assert film_size_for(_brief(None), frame_model="qwen") is None
    assert film_settings(SETTINGS, _brief(None), frame_model="qwen") is SETTINGS


def test_film_size_ignores_other_families_canvases():
    """别的生图档的选定值不能冒充成片画幅（#144 D 的同一纪律）。"""
    brief = _brief({"z_image": (640, 1280)})
    assert film_size_for(brief, frame_model="qwen") is None


def test_node_setup_lines_carry_the_same_numbers_as_the_pack():
    """向导交代节点的那几行必须与包 output 同数——路线 B 的全部意义就在这里。"""
    lines = "\n".join(node_setup_lines(SETTINGS))
    assert "width" in lines and "736" in lines
    assert "height" in lines and "1280" in lines
    assert "864" in lines, "ref_max_size 也要交代，节点上它是独立 widget"


# ---------------------------------------------------------------------------
# 向导接线：导演台路线问的是成片画幅，而且只问这一道
# ---------------------------------------------------------------------------

def _scripted(monkeypatch, answers: list[str]) -> list[str]:
    prompts = iter(answers)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(prompts)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def test_director_film_selector_snaps_and_derives_the_frame_canvas(monkeypatch, capsys):
    """选 16:9 的 1920×1080：落到 1920×1088，关键帧画布取同一尺寸。"""
    monkeypatch.setattr(
        "minimax_h3_prompt.image_config.load_frame_image_model", lambda: "qwen")
    # 第 8 档＝16:9；第 2 档＝1920×1080。
    asked = _scripted(monkeypatch, ["8", "2"])

    canvases = wizard._choose_film_canvases()

    assert canvases == {"qwen": (1920, 1088)}
    assert any("成片画幅" in q for q in asked), "问句抬头没有说成片画幅"
    screen = capsys.readouterr().out
    assert "1920×1088" in screen, "对齐后的实际执行尺寸没有说破"


def _cfg(tmp_path):
    return SimpleNamespace(default_duration=5.0, default_language="Chinese",
                           sessions_root=tmp_path / "sessions",
                           max_clarification_rounds=0)


def test_phase1_new_director_route_uses_the_film_question_only(tmp_path, monkeypatch):
    """导演台路线不许再问通用画布：成片画幅那一答就是 brief.canvases 的全部来源。"""
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def _boom():
        raise AssertionError("导演台路线又走回了通用画布问答")

    monkeypatch.setattr(wizard, "_choose_canvases", _boom)
    monkeypatch.setattr(
        wizard, "_choose_film_canvases", lambda: {"qwen": (1920, 1088)})
    captured: dict = {}

    def fake_run_stage1(brief, config, **kwargs):
        captured["brief"] = brief
        return {"script": "分场剧本"}, object(), {"prompt_engineer": object()}

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)
    # 主题 / 时长 / 风格 / 方式 5=导演台 / 模式 3=fl2v（不要再答画布）
    _scripted(monkeypatch, ["题目", "", "", "5", "3"])

    session = wizard._phase1_new(_cfg(tmp_path))

    assert session is not None
    assert captured["brief"].canvases == {"qwen": (1920, 1088)}
    assert film_size_for(captured["brief"], frame_model="qwen") == (1920, 1088)
