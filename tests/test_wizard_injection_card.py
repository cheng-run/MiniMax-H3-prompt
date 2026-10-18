"""向导·注入卡接线（issue #45/#49）：视频输出自动推导（不再提问）、卡落盘、正文不带槽位名。

薄接线：走真实调用链（``run_wizard`` / ``_phase2_ref_finish``），只打桩 LLM 与交互
输入，断言全落在外部行为上——问过哪些问题、打出哪些菜单、写出哪些文件。
注入卡文本与尺寸契约的规格在 ``tests/test_injection_card.py``，brief 键在
``tests/test_brief_parser.py``。issue #49 起视频尺寸**不再单独提问**：三元组由
画布自动推导（比例档继承画布比例、megapixels 按面积反推取档），原「必须问」断言
反转为「不得出现」。
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


def _run_full_flow(tmp_path, monkeypatch, canvases: dict | None = None):
    """四张素材图按素材循环逐张交回 + 「不加视频音频」；视频输出**无问答**（issue #49 自动推导）。

    返回 ``(注入卡路径, 全部提问文案)``——提问文案供「不得再问视频尺寸」类断言用。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: dict(canvases or {}))
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
    # 提问次序：主题/澄清跳过/路线 4 → 素材循环脚本（此后到注入卡无任何视频尺寸问答）
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + loop)
    asked += _fake_confirms(monkeypatch, [("视频/音频", False)])
    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    return next((tmp_path / "sessions").rglob(CARD_FILENAME)), asked


# ---------------------------------------------------------------------------
# 自动推导 + 卡落盘（AC1/AC2）
# ---------------------------------------------------------------------------

def test_wizard_derives_video_output_without_asking(tmp_path, monkeypatch):
    """AC1/AC2：向导不再出现视频尺寸问答；三元组由画布自动推导并复现当日手选值
    ——画布 1280×720 推出 16:9 · 0.9MP · 32 → 1280×736。"""
    card, asked = _run_full_flow(tmp_path, monkeypatch, {"zimage": (1280, 720)})
    for question in asked:
        assert "视频输出" not in question and "生成尺寸" not in question, question
    text = card.read_text(encoding="utf-8")
    assert "红裙女孩" in text and "ref_image_0" in text     # AC1 映射
    assert "16:9 (Widescreen)" in text                      # AC2 比例档（继承画布）
    assert "megapixels：0.9" in text and "对齐倍数：32" in text
    assert "1280 × 736" in text                             # AC2 最终 W×H
    assert "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2" in text  # AC3 登记名


def test_derived_triple_lands_in_the_session_brief(tmp_path, monkeypatch):
    """推导三元组回写 brief 并随会话落盘（复审：只落卡不回写＝快照与卡两处对不上账）。"""
    from minimax_h3_prompt.session_store import load_session

    card, _asked = _run_full_flow(tmp_path, monkeypatch, {"zimage": (1280, 720)})
    session = load_session(card.parent)
    assert session.brief.video_output is not None
    assert (session.brief.video_output.ratio,
            session.brief.video_output.megapixels,
            session.brief.video_output.multiple) == ("16:9 (Widescreen)", 0.9, 32)


def test_video_size_questions_and_ratio_menu_are_gone(tmp_path, monkeypatch, capsys):
    """AC1 的 UI 面：视频尺寸问答与 10 档比例菜单整段消失（issue #47 断言反转——
    原「必须问/必须选」的钉子翻成「不得出现」）。"""
    _card, asked = _run_full_flow(tmp_path, monkeypatch, {"zimage": (1280, 720)})
    out = capsys.readouterr().out
    for ghost in ("请选择生成尺寸", "无任意比例", "不支持宽高直填", "视频输出·比例",
                  "视频输出·像素预算"):
        assert ghost not in out, f"已删问题还在出声：{ghost}"
    assert not any("视频输出" in q or "生成尺寸" in q for q in asked)


def test_resume_keeps_explicit_video_output_over_derivation(tmp_path, monkeypatch):
    """续接会话里已定案的三元组优先于画布推导（issue #49 AC3 的向导面）：
    画布 1280×720 推得出 16:9·0.9MP，但既定案的 21:9·0.4MP 不得被回写重算。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.injection_card import VideoOutputParams
    from minimax_h3_prompt.session_store import (
        STATUS_AWAITING_FRAMES,
        load_session,
        save_session,
    )

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC,
                  canvases={"zimage": (1280, 720)},
                  video_output=VideoOutputParams(ratio="21:9 (Ultrawide)", megapixels=0.4))
    directory = tmp_path / "sessions" / "s" / "GEN001"
    directory.mkdir(parents=True)
    save_session(directory, brief, _ref_stage1_state(), status=STATUS_AWAITING_FRAMES)
    session = load_session(directory)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    # 素材循环四张全放弃（issue #50）；「继续」放行进阶段 2（视频输出无问答，#49）
    _fake_inputs(monkeypatch, ["", "4", "", "4", "", "4", "4"])
    _fake_confirms(monkeypatch, [("视频/音频", False), ("继续", True)])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    card = next((tmp_path / "sessions").rglob(CARD_FILENAME))
    text = card.read_text(encoding="utf-8")
    assert "21:9 (Ultrawide)" in text and "megapixels：0.4" in text
    assert "16:9" not in text and "0.9" not in text  # 推导值不得顶掉既定案值


# ---------------------------------------------------------------------------
# AC4：槽位文件名不进提示词正文（卡是对照清单，不是可执行载荷）
# ---------------------------------------------------------------------------

def test_prompt_bodies_carry_no_slot_names_or_asset_files(tmp_path, monkeypatch):
    """注入卡有槽位/正本名，提示词正文一个都不带——两处不许串味。"""
    card, _asked = _run_full_flow(tmp_path, monkeypatch, {"zimage": (1280, 720)})
    card_text = card.read_text(encoding="utf-8")
    assert "ref_image_0" in card_text and "picture-01" in card_text  # 卡带槽位与正本名
    prompt = next((tmp_path / "sessions").rglob("video-prompt.md")).read_text(encoding="utf-8")
    for leak in ("ref_image_", "ref_video_", "ref_audio_", "girl.png", "assets/"):
        assert leak not in prompt, f"提示词正文泄漏了接线信息：{leak}"
