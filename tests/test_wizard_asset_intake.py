"""向导·素材交回接线（issue #43）：三型入库、台账落盘、不完整可中断续接、换版报出。

薄接线：走真实调用链（``run_wizard`` / ``_phase2_ref_finish``），只打桩 LLM 与交互
输入，断言全落在外部行为上——问过哪些问题、写出哪些文件、打出哪些报出字样。
素材台账层的规格在 ``tests/test_asset_ledger.py``。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.asset_ledger import (
    KIND_AUDIO,
    KIND_PICTURE,
    KIND_VIDEO,
    AssetSubmission,
    intake_assets,
    load_asset_ledger,
    resolve_asset_path,
)
from minimax_h3_prompt.brief_parser import Brief, RefItem
from minimax_h3_prompt.session_store import (
    STATUS_AWAITING_FRAMES,
    find_awaiting_sessions,
    load_session,
    save_session,
)
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

# 参考素材登记编号按剧本出场顺序（编号即叙事顺序）：红裙女孩 → 秋日庭院 → 青铜钥匙 → 蓝衫少年
EXPECTED_NUMBERING = [
    ("红裙女孩", 1), ("秋日庭院", 2), ("青铜钥匙", 3), ("蓝衫少年", 4),
]


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        try:
            return next(prompts)
        except StopIteration:
            # 逐段陪跑的交回口（issue #57 起 ≤15 秒直出也走这一套收尾）：喂完输入就
            # 停在这里。本文件钉的不是收尾，让 StopIteration 冒出去只会冒充失败。
            if "输出视频路径" in text:
                return ""
            raise

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _fake_confirms(monkeypatch, rules: list[tuple[str, bool]]) -> list[str]:
    """按问题字面配答案，规则答一次即消费（同一问题会反复问，如「还要交回吗」）。"""
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


def _cfg(tmp_path, **overrides):
    base = dict(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_stage2(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "run_stage2",
                        lambda state, brief, config, **kw: (dict(state), _SIX_SECTION_PROMPT))


def _stub_stage1(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "run_stage1",
                        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                                     {"prompt_engineer": object()}))


def _stub_vision_clean(monkeypatch) -> None:
    """读图校对（issue #44）在交回入库后必跑——打桩成无偏差读数，本文件只测入库。"""
    from minimax_h3_prompt.tools import reference_auditor

    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: '{"claims": []}')


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


# ---------------------------------------------------------------------------
# 交回入库：三型可入库、台账五字段落盘（AC1/AC2）
# ---------------------------------------------------------------------------

def test_ref_flow_collects_pictures_and_writes_ledger(tmp_path, monkeypatch, capsys):
    """全程：四张素材图交回 → 台账落盘，编号与剧本出场序一致（AC4）。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)

    _stub_vision_clean(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    paths = {
        "红裙女孩": _write_source(tmp_path, "girl.png", b"girl"),
        "秋日庭院": _write_source(tmp_path, "garden.png", b"garden"),
        "青铜钥匙": _write_source(tmp_path, "key.png", b"key"),
        "蓝衫少年": _write_source(tmp_path, "boy.png", b"boy"),
    }
    # 素材循环（issue #66 纯 Picture 序）：逐张交回，顺序＝剧本出场序；
    # 每张「队列菜单回车=出队首 → 审查回车/1=满意 → 填路径」；随后不再加视频/音频；
    # 视频输出不再问答（issue #49 自动推导），故无尾部选择器输入
    queue = ["红裙女孩", "秋日庭院", "青铜钥匙", "蓝衫少年"]
    loop: list[str] = []
    for index, name in enumerate(queue):
        if index < len(queue) - 1:
            loop.append("")
        loop += ["1", str(paths[name])]
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + loop)
    confirms = _fake_confirms(monkeypatch, [("视频/音频", False)])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    assert any("素材图路径" in text for text in asked), "多参路线要收素材图"
    # 帧图是首尾帧链的概念：多参不收「首帧/尾帧图片路径」（菜单措辞含「首尾帧」不算）
    assert not any("首帧图片" in text or "尾帧图片" in text for text in asked), "多参路线不收帧图"

    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    ledger = load_asset_ledger(ledger_dir)
    # 编号仍取参考素材登记的剧本出场序（交回次序变了，编号不许跟着变）
    assert sorted(((e.name, e.number) for e in ledger.entries), key=lambda t: t[1]) \
        == EXPECTED_NUMBERING
    assert all(e.kind == KIND_PICTURE for e in ledger.entries)
    # 正本复制进素材库（issue #85）：位置一律过统一解析口，不在测试里另拼路径
    for entry in ledger.entries:
        assert resolve_asset_path(ledger, entry, session_dir=ledger_dir).is_file()
    assert ledger.library_root, "台账没记库根"
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))
    out = capsys.readouterr().out
    assert "[素材台账]" in out


def test_three_kinds_via_video_audio_addflow(tmp_path, monkeypatch, capsys):
    """按需附加素材视频/音频：三型都可交回入库（AC1）。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)

    _stub_vision_clean(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    girl = _write_source(tmp_path, "girl.png", b"girl")
    video = _write_source(tmp_path, "cam.mp4", b"vid")
    audio = _write_source(tmp_path, "voice.wav", b"aud")
    # 素材循环：红裙女孩交回，其余三张显式放弃（issue #50）；随后视频/音频按需挂账；
    # 视频输出不再问答（issue #49 自动推导），故无尾部选择器输入
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4",
                               "", "1", str(girl),
                               "", "4", "", "4", "4",
                               "1", "运镜参考", str(video), "2", "红裙女孩音色", str(audio)])
    _fake_confirms(monkeypatch, [("视频/音频", True), ("视频/音频", True), ("视频/音频", False)])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    ledger = load_asset_ledger(next((tmp_path / "sessions").rglob("asset-ledger.json")).parent)
    kinds = {(e.name, e.kind) for e in ledger.entries}
    assert ("运镜参考", KIND_VIDEO) in kinds
    assert ("红裙女孩音色", KIND_AUDIO) in kinds


# ---------------------------------------------------------------------------
# 中途收工可续接：还没生成完参考素材就不硬闯阶段 2（进度在完成前必须能接）
# ---------------------------------------------------------------------------

def test_pause_at_review_stays_resumable(tmp_path, monkeypatch, capsys):
    """审查菜单 q=先到这里 → 不写 video-prompt.md，会话保持可续接（重跑接着出这张）。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)

    _stub_vision_clean(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4", "", "q"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    assert not list((tmp_path / "sessions").rglob("video-prompt.md"))
    out = capsys.readouterr().out
    assert "已保留进度" in out
    # 会话仍是可续接态：重跑向导从当前待办继续
    assert find_awaiting_sessions(tmp_path / "sessions")


def test_complete_loop_proceeds_without_pause_question(tmp_path, monkeypatch):
    """四张全落账 → 不问「继续吗」，直接出注入卡与视频提示词——完整时不添无谓一问。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)

    _stub_vision_clean(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    queue = ["红裙女孩", "秋日庭院", "青铜钥匙", "蓝衫少年"]
    paths = {name: _write_source(tmp_path, f"{name}.png", name.encode()) for name in queue}
    loop: list[str] = []
    for index, name in enumerate(queue):
        if index < len(queue) - 1:
            loop.append("")
        loop += ["1", str(paths[name])]
    # 「继续」规则答 False：若被问到就会中断，video-prompt.md 不会出现（视频输出无问答，issue #49）
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + loop)
    confirms = _fake_confirms(monkeypatch, [("视频/音频", False), ("继续", False)])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))
    assert not any("继续" in text for text in confirms), "齐了后不该问「继续吗」"


# ---------------------------------------------------------------------------
# 换版：SHA256 变化可发现并报出，不静默（AC3）
# ---------------------------------------------------------------------------

def test_replacement_is_reported_and_needs_confirmation(tmp_path, monkeypatch, capsys):
    """同名换版：报出新旧哈希、问人拿主意；没点头就保留原登记（AC3）。"""
    import hashlib

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)

    _stub_vision_clean(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    directory = tmp_path / "sessions" / "s" / "GEN001"
    directory.mkdir(parents=True)
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    save_session(directory, brief, _ref_stage1_state(), status=STATUS_AWAITING_FRAMES)
    v1 = _write_source(tmp_path, "girl-v1.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(v1))],
                  registry=[RefItem(picture=1, name="红裙女孩", description="")])
    old_entry = load_asset_ledger(directory).entries[0]

    v2 = _write_source(tmp_path, "girl-v2.png", b"content-v2-different")
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(directory))
    # 素材循环：红裙女孩已入库（续跑菜单 2=换一张图重新交回）→ 审查满意 → 交回新版
    # （换版问人→不换），其余三张显式放弃（issue #50）；视频输出不再问答（issue #49）
    _fake_inputs(monkeypatch, ["", "2", "1", str(v2), "", "4", "", "4", "4"])
    confirms = _fake_confirms(monkeypatch, [
        ("替换", False), ("视频/音频", False),   # 换版不换；不加视频/音频
    ])

    code = wizard.run_wizard(_cfg(tmp_path))
    assert code == 0
    out = capsys.readouterr().out
    assert "换版" in out
    assert old_entry.sha256[:16] in out                                   # 旧哈希报出
    assert hashlib.sha256(b"content-v2-different").hexdigest()[:16] in out  # 新哈希报出
    # 没点头 → 原登记保留
    assert load_asset_ledger(directory).entries[0].sha256 == old_entry.sha256
    assert any("替换" in text for text in confirms), "换版必须问人"
