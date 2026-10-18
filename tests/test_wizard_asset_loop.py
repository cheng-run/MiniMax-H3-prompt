"""向导·素材循环逐张闭环（issue #50）：单张出词→审查→交回→校对→裁决。

覆盖票面六条 AC：
- AC1 一次只出一张、裁决落账（或显式放弃）后才出下一张；
- AC2 审查打回不入库；「改写提示词」算一轮修订并留痕（追加式轮次账）；
- AC3 校对后「重生成」默认原词再掷、「改词再掷」出口存在且留痕；
- AC4 队列默认人物→场景→道具，支持跳号/指定下一个出谁；
- AC5 展示的画布行与用户选定画布同源（不回落默认表）。

薄接线：走真实调用链（``run_wizard``），只打桩 LLM 与交互输入。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.asset_ledger import load_asset_ledger
from minimax_h3_prompt.asset_loop import ASSET_ROUNDS_FILENAME
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import (
    find_awaiting_sessions,
    load_session,
    save_session,
    STATUS_AWAITING_FRAMES,
)
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# 队列默认序（issue #50 AC4：人物→场景→道具；组内按 <Picture N> 剧本出场序）
QUEUE_ORDER = ["红裙女孩", "蓝衫少年", "秋日庭院", "青铜钥匙"]

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n<Subject 1> is 红裙女孩 in <Picture 1>.\n\n"
    "summary:\n[reference generation] The target video shows the girl handing over the key.\n\n"
    "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n[Shot 1] A medium shot frames the girl in the garden.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)

GIRL_PROMPT = "人物·红裙女孩专属生图提示词：红裙、黑发、银项链"


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


def _fake_confirms(monkeypatch, rules: list[tuple[str, bool]],
                   sink: list[str] | None = None) -> list[str]:
    queue = list(rules)
    asked: list[str] = sink if sink is not None else []

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
            {"name": "红裙女孩", "positive_prompt": GIRL_PROMPT},
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


def _stub_stage1(monkeypatch) -> None:
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                     {"prompt_engineer": object()}),
    )


def _stub_stage2(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage2(state, brief, config, **kwargs):
        if captured is not None:
            captured["state"] = dict(state)
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)


def _stub_vision(monkeypatch, mapping: dict[str, str]) -> list[str]:
    """按正本文件名（picture-NN）路由读图结果；记录调用过的路径。"""
    seen: list[str] = []

    def fake_describe(path, instruction=None):
        seen.append(str(path))
        for key, raw in mapping.items():
            if key in str(path):
                return raw
        return '{"claims": []}'

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return seen


def _stub_rewrite(monkeypatch, new_text: str = "改写后的提示词") -> list[str]:
    """打桩「改写提示词」的 LLM 改写：记录请求、回固定新词。"""
    seen: list[str] = []

    def fake_invoke(message):
        seen.append(str(message))
        return new_text

    monkeypatch.setattr(model_factory, "build_chat_model",
                        lambda: SimpleNamespace(invoke=fake_invoke))
    return seen


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _claims(deviations=(), omissions=()) -> str:
    items = [{"aspect": a, "prompt_quote": q, "observed": o} for a, q, o in deviations]
    items += [{"aspect": a, "prompt_quote": "", "observed": o} for a, o in omissions]
    return json.dumps({"claims": items}, ensure_ascii=False)


def _rounds(tmp_path: Path) -> list[dict]:
    files = list((tmp_path / "sessions").rglob(ASSET_ROUNDS_FILENAME))
    if not files:
        return []
    out: list[dict] = []
    for path in files:
        out.extend(json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines() if line.strip())
    return out


def _run_loop(tmp_path, monkeypatch, tail: list[str], *,
              canvases: dict | None = None,
              vision: dict[str, str] | None = None,
              confirms: list[tuple[str, bool]] | None = None,
              captured: dict | None = None,
              confirm_log: list[str] | None = None) -> tuple[list[str], int, list[str]]:
    """全程驱动：阶段 1 → 素材循环 → 尾账/注入卡 → 阶段 2。返回 (asked, code, vision_calls)。

    ``confirm_log`` 传列表即收下问过的**确认句**（``_confirm`` 的文案；``asked`` 只装
    ``_prompt`` 的），免得「撤登记问没问人」这类断言没处落。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: canvases or {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch, captured)
    calls = _stub_vision(monkeypatch, vision or {})
    # build_chat_model 只在「改写提示词」路径上被调；需要改写的用例先打 _stub_rewrite，
    # 这里不再兜底覆盖它。
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + list(tail))
    # sink 传的是**同一个列表对象**（extend 只拷当时的空表，后续 append 看不见）
    _fake_confirms(monkeypatch, confirms or [("视频/音频", False)], sink=confirm_log)
    code = wizard.run_wizard(_cfg(tmp_path))
    return asked, code, calls


# ---------------------------------------------------------------------------
# AC4：队列默认人物→场景→道具；组内剧本出场序
# ---------------------------------------------------------------------------

def test_queue_defaults_character_scene_prop(tmp_path, monkeypatch, capsys):
    """四张全落账：出词顺序必须是 人物→场景→道具（AC4），不是 state 存储序。"""
    paths = {name: _write_source(tmp_path, f"{name}.png", name.encode())
             for name in QUEUE_ORDER}
    # 每张：审查满意→路径；队列菜单 回车=默认队首（最后一张自动出，不问）
    tail: list[str] = []
    for i, name in enumerate(QUEUE_ORDER):
        if i < len(QUEUE_ORDER) - 1:
            tail.append("")  # 队列菜单：回车=出队首
        tail += ["1", str(paths[name])]
    tail += ["8", "1"]  # 视频输出选择器（issue #45）
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)

    assert code == 0
    out = capsys.readouterr().out
    positions = [out.index(f"当前待办：{name}") for name in QUEUE_ORDER]
    assert positions == sorted(positions), f"出词顺序不是人物→场景→道具：{QUEUE_ORDER}"
    # 每张出词只出现一次（一次只展示一张，AC1）
    assert out.count(GIRL_PROMPT) == 1
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))


def test_queue_supports_skip_and_specify_next(tmp_path, monkeypatch, capsys):
    """跳号/指定下一个出谁（AC4）：首问直接打序号 3，先出场景那张。"""
    paths = {name: _write_source(tmp_path, f"{name}.png", name.encode())
             for name in QUEUE_ORDER}
    tail: list[str] = [
        "3",              # 队列菜单：跳号，先出 3 号（秋日庭院）
        "1", str(paths["秋日庭院"]),
        "", "4",          # 出队首（蓝衫少年？不——红裙女孩是队首），放弃
        "", "4",
        "4",              # 最后一张自动出，放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    out = capsys.readouterr().out
    # 第一张出的是 3 号（跳号生效）
    first = min(out.index(f"当前待办：{name}") for name in QUEUE_ORDER)
    assert "当前待办：秋日庭院" in out
    assert out.index("当前待办：秋日庭院") == first, "跳号没生效：3 号应当先出"
    assert any("跳号/指定下一个出谁" in text for text in asked), "队列菜单缺跳号入口"


# ---------------------------------------------------------------------------
# AC1：一次只出一张；裁决落账（或显式放弃）后才出下一张
# ---------------------------------------------------------------------------

def test_one_prompt_at_a_time_and_advances_only_after_settle(tmp_path, monkeypatch, capsys):
    """出词→审查→交回→校对→裁决 串成单张节奏；下一张的词在本张落账后才出现。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "1", str(girl),     # 红裙女孩：满意→交回（干净读图→直接落账）
        "", "4",                # 蓝衫少年：显式放弃
        "", "4",                # 秋日庭院：显式放弃
        "4",                    # 青铜钥匙（最后一张自动出）：显式放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    out = capsys.readouterr().out
    # 张与张串行：本张的审查问题出现在下一张出词之前
    girl_pos = out.index("当前待办：红裙女孩")
    boy_pos = out.index("当前待办：蓝衫少年")
    assert girl_pos < boy_pos
    assert any("显式放弃这张资产" in text for text in asked), "审查菜单缺显式放弃出口"
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))


def test_abandon_keeps_asset_out_of_ledger_and_marks_state(tmp_path, monkeypatch, capsys):
    """显式放弃＝不入库、写进 asset_loop.abandoned，续接不再排它。"""
    tail = ["", "4", "", "4", "", "4", "4", "8", "1"]  # 四张全放弃
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json"), None)
    if ledger_dir is not None:
        assert not load_asset_ledger(ledger_dir.parent).entries, "放弃了的资产不许入库"
    session = load_session(next((tmp_path / "sessions").rglob("session-state.json")).parent)
    assert sorted(session.stage_state["asset_loop"]["abandoned"]) == sorted(QUEUE_ORDER)


def test_pause_at_review_stays_resumable(tmp_path, monkeypatch, capsys):
    """审查菜单 q=先到这里：不写 video-prompt.md，会话保持可续接（current 待办在盘）。"""
    tail = ["", "q"]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert not list((tmp_path / "sessions").rglob("video-prompt.md"))
    assert find_awaiting_sessions(tmp_path / "sessions"), "暂停必须留成可续接态"
    session = find_awaiting_sessions(tmp_path / "sessions")[0]
    assert session.stage_state["asset_loop"]["current"] == "红裙女孩"


# ---------------------------------------------------------------------------
# AC2：审查打回不入库；改写提示词留痕一轮
# ---------------------------------------------------------------------------

def test_review_external_reroll_never_registers(tmp_path, monkeypatch, capsys):
    """打回·外部重掷：不登记被拒图（无台账行、无轮次账），系统只把球放回审查。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "2", "1", str(girl),  # 打回外部重掷 → 再审 → 满意交回
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert any("外部重掷" in text for text in asked)
    assert not _rounds(tmp_path), "外部重掷不许留痕（不登记）"
    # 打回发生在填路径之前——被拒图从未拿到路径
    out = capsys.readouterr().out
    assert "被拒图不登记" in out
    ledger = load_asset_ledger(next((tmp_path / "sessions").rglob("asset-ledger.json")).parent)
    assert [e.name for e in ledger.entries] == ["红裙女孩"]


def test_review_rewrite_logs_round_and_rereleases(tmp_path, monkeypatch, capsys):
    """打回·改写提示词：算一轮修订、留痕追加账、新词重新出站（AC2）。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    rewrite_calls = _stub_rewrite(monkeypatch, "人物·红裙女孩专属生图提示词：宝蓝裙")
    tail = [
        "", "3", "裙子改成宝蓝色", "1", str(girl),  # 打回改写→意见→新词→满意→交回
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert any("改写要求" in text for text in asked), "改写要先问意见"
    assert rewrite_calls, "改写提示词没调 LLM"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["review_reword"]
    assert rounds[0]["feedback"] == "裙子改成宝蓝色"
    assert rounds[0]["prompt_before"] == GIRL_PROMPT
    assert rounds[0]["prompt_after"] == "人物·红裙女孩专属生图提示词：宝蓝裙"
    assert rounds[0]["round"] == 1
    out = capsys.readouterr().out
    assert "人物·红裙女孩专属生图提示词：宝蓝裙" in out, "改写后的新词没有重新出站"
    # 对照底本换新词：校对请求里带的是新词
    ledger = load_asset_ledger(next((tmp_path / "sessions").rglob("asset-ledger.json")).parent)
    assert [e.name for e in ledger.entries] == ["红裙女孩"]


def test_review_rewrite_cancel_keeps_prompt(tmp_path, monkeypatch, capsys):
    """改写意见留空＝取消：不动词、不留痕，回到审查菜单。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "3", "", "1", str(girl),  # 改写→空意见（取消）→满意→交回
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert not _rounds(tmp_path), "取消的改写不许留痕"
    out = capsys.readouterr().out
    assert "已取消改写" in out
    assert GIRL_PROMPT in out


# ---------------------------------------------------------------------------
# AC3：校对后重生成——默认原词再掷；改词再掷留痕
# ---------------------------------------------------------------------------

def test_regen_defaults_to_same_prompt_reroll_and_logs(tmp_path, monkeypatch, capsys):
    """裁决「重生成」→ 回车=原词再掷：留痕 regen_reroll，词不动。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"picture-01": _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),
        "2",          # 三选：重生成这张素材图
        "",           # 重生成菜单：回车=原词再掷
        "4",          # 重掷后回审查——直接放弃这张收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    assert any("原词再掷" in text for text in asked), "重生成菜单缺默认口"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_reroll"]
    assert rounds[0]["prompt_before"] == rounds[0]["prompt_after"] == GIRL_PROMPT
    out = capsys.readouterr().out
    assert "原提示词重掷" in out


def test_regen_reword_exit_rewrites_and_logs(tmp_path, monkeypatch, capsys):
    """「改词再掷」：留痕 regen_reword、新词出站（词图皆错时连词一起换）。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"picture-01": _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    rewrite_calls = _stub_rewrite(monkeypatch, "人物·红裙女孩专属生图提示词：宝蓝裙")
    tail = [
        "", "1", str(girl),
        "2",                    # 三选：重生成
        "2", "裙子改成宝蓝色",   # 改词再掷→意见
        "4",                    # 新词出站后审查——这张先放弃收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    assert any("改词再掷" in text for text in asked), "重生成菜单缺改词再掷出口"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_reword"]
    assert rounds[0]["prompt_after"] == "人物·红裙女孩专属生图提示词：宝蓝裙"
    out = capsys.readouterr().out
    assert "人物·红裙女孩专属生图提示词：宝蓝裙" in out, "改词再掷的新词没有出站"


# ---------------------------------------------------------------------------
# 放弃后撤登记要问人；循环外换版退回待办；台账漂移照报
# ---------------------------------------------------------------------------

def test_abandon_after_intake_asks_before_withdrawing(tmp_path, monkeypatch, capsys):
    """重生成后弃坑：撤不撤台账登记问人（默认撤＝废图不进片），不静默删凭据。"""
    from minimax_h3_prompt.asset_ledger import load_asset_ledger

    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"picture-01": _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),      # 交回
        "2",                     # 三选：重生成
        "3",                     # 重生成菜单：显式放弃这张资产
        "", "4", "", "4", "4",   # 其余三张放弃
        "8", "1",
    ]
    confirm_log: list[str] = []
    asked, code, _ = _run_loop(
        tmp_path, monkeypatch, tail, vision=vision,
        confirms=[("撤下", True), ("视频/音频", False)], confirm_log=confirm_log)
    assert code == 0
    assert any("撤下" in text for text in confirm_log), "撤登记没问人（静默删凭据）"
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    assert not load_asset_ledger(ledger_dir).entries, "点头撤下后台账还留着这版"


def test_abandon_after_intake_can_keep_the_registration(tmp_path, monkeypatch, capsys):
    """同一个问句答「不撤」：登记照旧保留，屏上明说卡还会接它。"""
    from minimax_h3_prompt.asset_ledger import load_asset_ledger

    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"picture-01": _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = ["", "1", str(girl), "2", "3", "", "4", "", "4", "4", "8", "1"]
    _asked, code, _ = _run_loop(
        tmp_path, monkeypatch, tail, vision=vision,
        confirms=[("撤下", False), ("视频/音频", False)])
    assert code == 0
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    assert [e.name for e in load_asset_ledger(ledger_dir).entries] == ["红裙女孩"]
    assert "登记保留" in capsys.readouterr().out


def test_externally_replaced_asset_returns_to_queue(tmp_path, monkeypatch, capsys):
    """循环外换版（正本 sha 变而裁决没重做）→ 旧裁决不冒充新版：退回待办重新校对。"""
    from minimax_h3_prompt.asset_ledger import AssetSubmission, intake_assets, load_asset_ledger

    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    _asked, code, calls = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0 and len(calls) == 1

    # 循环外换版：非交互 intake（on_replace 缺省＝接受替换）把正本换掉
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    girl2 = _write_source(tmp_path, "girl2.png", b"girl-v2")
    intake_assets(ledger_dir, [AssetSubmission("红裙女孩", "picture", str(girl2))])

    session = load_session(ledger_dir)
    save_session(ledger_dir, session.brief, session.stage_state, status=STATUS_AWAITING_FRAMES)
    session = load_session(ledger_dir)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    calls.clear()
    # 续接：红裙女孩重新回到待办（其余三张已落账/放弃）
    asked2 = _fake_inputs(monkeypatch, ["", "1", str(girl2), "", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    wizard.run_wizard(_cfg(tmp_path))
    assert calls, "换版后旧裁决冒充了新版——没有退回重新读图"
    assert any("当前待办：红裙女孩" in text or "素材图路径" in text for text in asked2)


def test_ledger_drift_is_reported_at_loop_entry(tmp_path, monkeypatch, capsys):
    """台账漂移（正本文件被换而登记没换）在循环入口照报，不静默。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    _asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0

    # 循环外动手脚：把会话里的正本复制改掉（登记里的 sha 没跟着变 → 漂移）
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    entry = load_asset_ledger(ledger_dir).entries[0]
    (ledger_dir / entry.path).write_bytes(b"tampered")

    session = load_session(ledger_dir)
    save_session(ledger_dir, session.brief, session.stage_state, status=STATUS_AWAITING_FRAMES)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(ledger_dir))
    _fake_inputs(monkeypatch, ["", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    capsys.readouterr()
    wizard.run_wizard(_cfg(tmp_path))
    out = capsys.readouterr().out
    assert "[对账]" in out and "正本与记录不符" in out, "台账漂移没在循环入口照报"


def test_regen_reword_cancel_stays_in_menu_and_does_not_log_reroll(tmp_path, monkeypatch, capsys):
    """重掷菜单里选「改词再掷」又留空意见＝取消：留在菜单重选，不许默默当原词再掷。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"picture-01": _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),
        "2",        # 三选：重生成
        "2", "",    # 改词再掷 → 空意见（取消）
        "1",        # 留在重掷菜单：这次选原词再掷
        "4",        # 回审查后放弃这张收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    assert "已取消改写" in capsys.readouterr().out, "取消没有可见交代"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_reroll"], \
        f"取消的改词不该留痕、也不该记成原词再掷：{[r['trigger'] for r in rounds]}"
    assert sum(1 for text in asked if "重生成·红裙女孩" in text) >= 2, \
        "取消改词后没回重掷菜单重选"


# ---------------------------------------------------------------------------
# AC5：画布行与用户选定画布同源
# ---------------------------------------------------------------------------

def test_canvas_rows_follow_user_selection(tmp_path, monkeypatch, capsys):
    """选了非默认画布：出站画布行走 brief.canvases，不再回落默认表。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    asked, code, _ = _run_loop(
        tmp_path, monkeypatch, tail,
        canvases={"zimage": (700, 1300)},
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "700 × 1300" in out, "展示画布行没走用户选定"
    assert "640 × 1280" not in out, "回落了默认表"
    # 落盘文件同样同源
    files = list((tmp_path / "sessions").rglob("character-prompt.md"))
    assert files and "700 × 1300" in files[0].read_text(encoding="utf-8")
