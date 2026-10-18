"""向导·注入卡接线（issue #45）：视频输出三元组选择器、卡落盘、正文不带槽位名。

薄接线：走真实调用链（``run_wizard`` / ``_phase2_ref_finish``），只打桩 LLM 与交互
输入，断言全落在外部行为上——问过哪些问题、打出哪些菜单、写出哪些文件。
注入卡文本与尺寸契约的规格在 ``tests/test_injection_card.py``，brief 键在
``tests/test_brief_parser.py``。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.injection_card import CARD_FILENAME
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n<Subject 1> is 红裙女孩 in <Picture 1>.\n\n"
    "summary:\n[reference generation] The target video shows the girl handing over the key.\n\n"
    "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n[Shot 1] A medium shot frames the girl in the garden.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)


@pytest.fixture(autouse=True)
def _isolate_proofread(monkeypatch):
    """读图校对（issue #44）不属本文件考题：它会去读素材真图，假 png 一读就截停流程。

    素材循环（issue #50）里校对紧跟在交回之后，本文件的夹具素材不是真图片，读图失败
    会停在「等交回」走不到注入卡。只打桩视觉读图返回**干净读数**——循环节奏本身是真的
    （四张逐张交回归档），偏差三选由 test_wizard_asset_proofread 专测钉住。
    """
    from minimax_h3_prompt.tools import reference_auditor

    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: '{"claims": []}')


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(prompts)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _fake_confirms(monkeypatch, rules: list[tuple[str, bool]]) -> list[str]:
    queue = list(rules)
    asked: list[str] = []

    def fake_confirm(text: str, default: bool = True) -> bool:
        asked.append(text)
        for i, (needle, answer) in enumerate(queue):
            if needle in text:
                queue.pop(i)
                return answer
        return default

    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    return asked


def _ref_stage1_state() -> dict:
    return {
        "script": TOPIC,
        "character_image_prompts": [
            {"name": "蓝衫少年", "positive_prompt": "人物·蓝衫少年专属生图提示词"},
            {"name": "红裙女孩", "positive_prompt": "人物·红裙女孩专属生图提示词"},
        ],
        "prop_image_prompts": [
            {"name": "青铜钥匙", "positive_prompt": "道具·青铜钥匙专属生图提示词"},
        ],
        "scene_image_prompts": [
            {"name": "秋日庭院", "positive_prompt": "场景·秋日庭院专属生图提示词"},
        ],
    }


def _cfg(tmp_path):
    return SimpleNamespace(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )


def _stub_stage1(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "run_stage1",
                        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                                     {"prompt_engineer": object()}))


def _stub_stage2(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "run_stage2",
                        lambda state, brief, config, **kw: (dict(state), _SIX_SECTION_PROMPT))


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _run_full_flow(tmp_path, monkeypatch, tail_inputs: list[str]):
    """四张素材图按素材循环逐张交回 + 「不加视频音频」+ 尾部输入（选择器问答）。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    # 素材循环队列序（issue #50 AC4：人物→场景→道具、组内剧本出场序）
    queue = ["红裙女孩", "蓝衫少年", "秋日庭院", "青铜钥匙"]
    paths = {name: _write_source(tmp_path, f"{name}.png", name.encode()) for name in queue}
    loop: list[str] = []
    for index, name in enumerate(queue):
        if index < len(queue) - 1:
            loop.append("")          # 队列菜单：回车=出队首
        loop += ["1", str(paths[name])]  # 审查满意 → 交回路径
    # 提问次序：主题/澄清跳过/路线 4 → 素材循环脚本 → 选择器（比例档/预算档/…）
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + loop + tail_inputs)
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    return next((tmp_path / "sessions").rglob(CARD_FILENAME))


# ---------------------------------------------------------------------------
# 选择器 + 卡落盘（AC1/AC2/AC3）
# ---------------------------------------------------------------------------

def test_wizard_writes_injection_card_with_default_budget(tmp_path, monkeypatch):
    """素材交回后问视频输出三元组：选比例 → 采用默认预算 → 卡落盘、三元组可照抄。

    「8」＝第 8 档 9:16 (Portrait Widescreen)；「1」＝采用默认（0.4MP · 对齐 32）。
    """
    card = _run_full_flow(tmp_path, monkeypatch, ["8", "1"])
    text = card.read_text(encoding="utf-8")
    assert "红裙女孩" in text and "ref_image_0" in text     # AC1 映射
    assert "9:16 (Portrait Widescreen)" in text             # AC2 比例档（widget 值）
    assert "megapixels：0.4" in text and "对齐倍数：32" in text
    assert "480" in text and "864" in text                  # AC2 最终 W×H
    assert "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2" in text  # AC3 登记名


def test_custom_budget_is_hand_input_megapixels_and_multiple(tmp_path, monkeypatch):
    """「自定义」＝手输 megapixels/对齐倍数（坏值重问不静默）；宽高直填没有入口。"""
    # 「2」=自定义；第一轮 megapixels「大」是坏值 → 整对重问；第二轮 1.0/16 落卡。
    card = _run_full_flow(tmp_path, monkeypatch, ["8", "2", "大", "16", "1.0", "16"])
    text = card.read_text(encoding="utf-8")
    assert "megapixels：1" in text          # :g 格式 1.0 → 1
    assert "对齐倍数：16" in text
    assert "768" in text and "1360" in text  # 9:16 · 1.0MP · 对齐 16 的推导值


def test_selected_triple_lands_in_the_session_brief(tmp_path, monkeypatch):
    """选定三元组回写 brief 并随会话落盘（复审：只落卡不回写＝快照与卡两处对不上账）。"""
    from minimax_h3_prompt.session_store import load_session

    card = _run_full_flow(tmp_path, monkeypatch, ["8", "2", "1.0", "16"])
    session = load_session(card.parent)
    assert session.brief.video_output is not None
    assert (session.brief.video_output.ratio,
            session.brief.video_output.megapixels,
            session.brief.video_output.multiple) == ("9:16 (Portrait Widescreen)", 1.0, 16)


def test_ratio_menu_is_the_ten_node_labels_without_arbitrary_entry(tmp_path, monkeypatch,
                                                                   capsys):
    """比例档菜单＝节点 10 档标签，无任意比例/宽高直填逃生口（AC2 的 UI 面）。"""
    _run_full_flow(tmp_path, monkeypatch, ["8", "1"])
    out = capsys.readouterr().out
    for label in ("1:1 (Square)", "9:16 (Portrait Widescreen)", "21:9 (Ultrawide)"):
        assert label in out, f"比例菜单缺节点档位：{label}"
    assert "无任意比例" in out
    assert "不支持宽高直填" in out


# ---------------------------------------------------------------------------
# AC4：槽位文件名不进提示词正文（卡是对照清单，不是可执行载荷）
# ---------------------------------------------------------------------------

def test_prompt_bodies_carry_no_slot_names_or_asset_files(tmp_path, monkeypatch):
    """注入卡有槽位/正本名，提示词正文一个都不带——两处不许串味。"""
    card = _run_full_flow(tmp_path, monkeypatch, ["8", "1"])
    card_text = card.read_text(encoding="utf-8")
    assert "ref_image_0" in card_text and "picture-01" in card_text  # 卡带槽位与正本名
    prompt = next((tmp_path / "sessions").rglob("video-prompt.md")).read_text(encoding="utf-8")
    for leak in ("ref_image_", "ref_video_", "ref_audio_", "girl.png", "assets/"):
        assert leak not in prompt, f"提示词正文泄漏了接线信息：{leak}"
