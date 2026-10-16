"""多参路线入口测试（issue #41）：向导可选、真实可达、≤15 秒直出、首尾帧不受影响。

走**真实调用链**（``_phase1_new`` / ``run_wizard``），只打桩 LLM 管线与交互输入，
断言全落在外部行为上：brief 的 mode、写出的 video-prompt.md、问过哪些问题。
"""
from __future__ import annotations

from types import SimpleNamespace

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import STATUS_AWAITING_FRAMES, load_session, save_session
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


def _cfg(tmp_path, **overrides):
    base = dict(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_stage2(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage2(state, brief, config, **kwargs):
        if captured is not None:
            captured["brief"] = brief
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)


def _stub_stage1(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage1(brief, config, **kwargs):
        if captured is not None:
            captured["brief"] = brief
        return _ref_stage1_state(), object(), {"prompt_engineer": object()}

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)


# ---------------------------------------------------------------------------
# 入口可选多参路线
# ---------------------------------------------------------------------------

def test_choose_generation_mode_accepts_ref_route(tmp_path, monkeypatch):
    """方式四选一：4=多参(REF2VA)；3=首尾帧(FL2VA) 行为不变。"""
    _silence(monkeypatch)
    _fake_inputs(monkeypatch, ["4"])
    assert wizard._choose_generation_mode() == "REF2VA"
    _fake_inputs(monkeypatch, ["3"])
    assert wizard._choose_generation_mode() == "FL2VA"
    _fake_inputs(monkeypatch, [""])
    assert wizard._choose_generation_mode() == "FL2VA"  # 回车默认仍是首尾帧


def test_phase1_new_ref_route_builds_ref_brief(tmp_path, monkeypatch):
    """选多参 → Brief(mode=ref)；选首尾帧 → mode=base 不受影响。"""
    _silence(monkeypatch)
    # base 分支会进人机修改循环（首尾帧路线行为不变的现场）；一律答「没有意见」。
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    captured: dict = {}
    _stub_stage1(monkeypatch, captured)
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4"])
    session = wizard._phase1_new(_cfg(tmp_path))
    assert captured["brief"].mode == "ref"
    assert session is not None and session.brief.mode == "ref"

    captured2: dict = {}
    _stub_stage1(monkeypatch, captured2)
    _fake_inputs(monkeypatch, [TOPIC, "", "", "3"])
    wizard._phase1_new(_cfg(tmp_path))
    assert captured2["brief"].mode == "base"
    assert captured2["brief"].variant == "FL2VA"


# ---------------------------------------------------------------------------
# ≤15 秒直出一份 ref 六段式
# ---------------------------------------------------------------------------

def test_ref_route_direct_prompt_end_to_end(tmp_path, monkeypatch, capsys):
    """run_wizard 全程：多参路线不收帧图，直出一份六段式写进 video-prompt.md。"""
    _silence(monkeypatch)
    captured: dict = {}
    _stub_stage1(monkeypatch, captured)
    _stub_stage2(monkeypatch, captured)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4"])  # 主题/时长/风格/方式，无帧图路径

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    assert captured["brief"].mode == "ref"
    assert not any("图片路径" in text for text in asked), f"多参路线不该收帧图：{asked}"

    outputs = list((tmp_path / "sessions").rglob("video-prompt.md"))
    assert len(outputs) == 1
    text = outputs[0].read_text(encoding="utf-8")
    # 段头判据从权威常量派生（ADR 0008 裁定二：测试里不另抄字面量）。
    from minimax_h3_prompt.tools.h3_validator import REF_SECTIONS
    for header in REF_SECTIONS:
        assert f"{header}:" in text
    out = capsys.readouterr().out
    assert "<Picture 1> 红裙女孩" in out  # 编号按剧本出场顺序的登记一览
    assert "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2" in out  # 登记名


def test_ref_route_over_15s_delivers_with_segment_ticket_notice(tmp_path, monkeypatch, capsys):
    """>15 秒：仍出整片六段式（存档），并明说拆段在票 #46——不静默装作已拆。"""
    _silence(monkeypatch)
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    _fake_inputs(monkeypatch, [TOPIC, "20", "", "4"])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    outputs = list((tmp_path / "sessions").rglob("video-prompt.md"))
    assert len(outputs) == 1
    assert "#46" in capsys.readouterr().out


def test_unmatched_semantic_name_is_flagged(tmp_path, monkeypatch, capsys):
    """语义名没在剧本出场 → 登记一览点名提示（issue #41 复审 F5，绝不静默失败）。"""
    _silence(monkeypatch)
    state = _ref_stage1_state()
    state["script"] = TOPIC.replace("蓝衫少年", "那个少年")  # 只留一个名字对不上
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    wizard._show_ref_registry(state, brief)
    out = capsys.readouterr().out
    assert "未在剧本中找到出场" in out and "蓝衫少年" in out


def test_ref_resume_awaiting_still_delivers_asset_prompts(tmp_path, monkeypatch, capsys):
    """续接 awaiting 的 ref 会话必须补展资产图提示词并落盘（issue #41 复审）。

    修复前：_resume_awaiting_frames 的 ref 分支直接早退，run_wizard 却置 frames_shown=True
    ——补展接线永远不触发，续接的 ref 会话全程拿不到资产提示词（复审实测复现）。
    """
    _silence(monkeypatch)
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    directory = tmp_path / "sessions" / "s" / "GEN001"
    directory.mkdir(parents=True)
    save_session(directory, brief, _ref_stage1_state(), status=STATUS_AWAITING_FRAMES)
    session = load_session(directory)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    out = capsys.readouterr().out
    assert "人物·红裙女孩专属生图提示词" in out, "续接后资产图提示词没有补展"
    assert (directory / "character-prompt.md").is_file(), "补展没有落盘"
    assert (directory / "video-prompt.md").is_file()
