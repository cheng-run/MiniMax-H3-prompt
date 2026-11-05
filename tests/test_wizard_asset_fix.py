"""已裁决素材画面事实的人工修复入口（#174）：读时告警、就地改写、人工留痕。

缝（票面 Testing Decisions 第 5 条）：校对记录 schema 与描述取值——用例只观察
**终端话、落盘的记录、返回的取材结果**，不碰内部调用顺序。薄接线：走真实调用链
（``run_wizard``），只打桩视觉读图、LLM 与交互输入。

背景（issue #168）：#162 修好的是**新产生**的读图实况/补漏，已经并进英文正文的中文
补漏没有修复入口——唯一修法是手改 ``session-state.json``，没有校验也没有留痕。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.asset_proofread import proofread_descriptions
from minimax_h3_prompt.session_store import (
    load_session,
    save_session,
    STATUS_AWAITING_FRAMES,
)
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.ui import wizard
from minimax_h3_prompt.ui.wizard_asset_fix import EDIT_FILENAME

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n<Subject 1> is 红裙女孩 in <Picture 1>.\n\n"
    "summary:\n[reference generation] The target video shows the girl handing over the key.\n\n"
    "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n[Shot 1] A medium shot frames the girl in the garden.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)

GIRL_PROMPT = ("Professional editorial still-life of one girl in a red dress "
               "holding a bronze key, three-quarter view.")

# 补漏实况里嵌着一段 ≥8 字的中文（#162 的判据 MIN_FOREIGN_RUN）——这正是 #168
# 实跑记录里那条「并进英文正文的中文补漏」。
CHINESE_OMISSION = "左侧旧自行车后轮上方可见弧形挡板"
FIXED_DESCRIPTION = (GIRL_PROMPT + "; a curved fender is visible above the rear wheel "
                     "of the old bicycle on the left")


@pytest.fixture(autouse=True)
def _isolate_injection_card(monkeypatch):
    """注入卡不属本文件考题；返回 True＝容量没超、流程照走（issue #86）。"""
    monkeypatch.setattr(wizard, "_deliver_injection_card", lambda *a, **k: True)


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


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


def _english_girl_state() -> dict:
    """红裙女孩的生图提示词换成英文正文（Qwen／MOSS 档的形态）。"""
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


def _stub_stage2(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage2(state, brief, config, **kwargs):
        if captured is not None:
            captured["state"] = dict(state)
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)


def _stub_describe(monkeypatch, mapping: dict[str, str]) -> list[str]:
    seen: list[str] = []

    def fake_describe(path, instruction=None):
        seen.append(str(path))
        for key, raw in mapping.items():
            if key in str(path):
                return raw
        return '{"claims": []}'

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return seen


def _claims(deviations=(), omissions=()) -> str:
    items = [{"aspect": a, "prompt_quote": q, "observed": o} for a, q, o in deviations]
    items += [{"aspect": a, "prompt_quote": "", "observed": o} for a, o in omissions]
    return json.dumps({"claims": items}, ensure_ascii=False)


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _settle_girl_with_chinese_omission(tmp_path, monkeypatch) -> tuple[str, Path]:
    """首跑：红裙女孩交回落账（补漏并进一段中文），其余三张放弃。

    返回 ``(asked 文本, 生成目录)``。这一段复现 #168 的存量形态：英文正文 ＋ 中文补漏。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    monkeypatch.setattr(wizard, "run_stage1",
                          lambda brief, config, **kw: (_english_girl_state(), object(),
                                                       {"prompt_engineer": object()}))
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    _stub_describe(monkeypatch, {"红裙女孩": _claims(omissions=[("挡泥板", CHINESE_OMISSION)])})
    girl = _write_source(tmp_path, "girl.png", b"girl")
    prompts = iter([TOPIC, "", "", "4",
                    "", "1", str(girl),        # 素材循环：红裙女孩 出词/审查/交回
                    "", "4", "", "4", "4",     # 其余三张放弃
                    "8", "1"])
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        try:
            return next(prompts)
        except StopIteration:
            if "输出视频路径" in text:
                return ""
            raise

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    _fake_confirms(monkeypatch, [("并入", True), ("视频/音频", False)])
    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    directory = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    return "\n".join(asked), directory


def _resume(tmp_path, monkeypatch, directory: Path, fake_prompt, confirms=()):
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    save_session(directory, load_session(directory).brief,
                 load_session(directory).stage_state, status=STATUS_AWAITING_FRAMES)
    session = load_session(directory)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    _fake_confirms(monkeypatch, list(confirms) + [("视频/音频", False)])
    return wizard.run_wizard(_cfg(tmp_path))


def _record(directory: Path) -> dict:
    return load_session(directory).stage_state["asset_proofreads"]["红裙女孩"]


# ---------------------------------------------------------------------------
# AC：已裁决记录读到中英混排 → 点名资产并给出那段中文
# ---------------------------------------------------------------------------

def test_settled_record_with_chinese_reminder_is_flagged_on_read(tmp_path, monkeypatch, capsys):
    """续接素材循环读到已裁决记录：屏幕点名资产与那段中文，并落一条交互日志告警。"""
    _ask0, directory = _settle_girl_with_chinese_omission(tmp_path, monkeypatch)
    capsys.readouterr()  # 首跑的输出不参与断言

    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if "改素材画面事实" in text:
            return "1"  # 跳过这次修复：本用例只考「读到就告警」
        return "" if "输出视频路径" in text else ""

    assert _resume(tmp_path, monkeypatch, directory, fake_prompt) == 0
    out = capsys.readouterr().out

    assert "已裁决落定" in out, "没进已裁决项的菜单"
    assert any("改素材画面事实" in text for text in asked), f"菜单里没有修复入口：{asked}"
    assert "红裙女孩" in out and CHINESE_OMISSION in out, "告警没点名资产与那段中文"
    log = directory.parent / "interactions.jsonl"
    assert log.is_file(), "告警没进交互日志（#171 的唯一取证物）"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    notices = [row for row in rows if row["event"] == "notice"
               and row["question"].startswith("[language_mixed]")]
    assert notices, f"交互日志里没有 language_mixed 告警：{rows}"
    assert any("红裙女孩" in row["question"] for row in notices), \
        f"告警行没点名资产：{notices}"


# ---------------------------------------------------------------------------
# AC：就地编辑同 sha 写回、不触发视觉调用、留改前/改后/时间
# ---------------------------------------------------------------------------

def test_settled_description_edit_updates_record_without_reread(tmp_path, monkeypatch,
                                                                capsys):
    """菜单选 2 → 编辑临时文本 → 同 sha 写回，人工改写留痕；视觉调用一次都不多花。"""
    _ask0, directory = _settle_girl_with_chinese_omission(tmp_path, monkeypatch)
    before = _record(directory)
    assert CHINESE_OMISSION in before["description"], "前提不成立：存量混排没造出来"
    capsys.readouterr()

    calls = _stub_describe(monkeypatch, {"红裙女孩": '{"claims": []}'})
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if "改素材画面事实" in text:
            return "2"
        if "改好后回车" in text:
            # 用户此刻在编辑器里改文件（路径就在会话目录下，屏幕上已打印）
            edit_file = next(iter(directory.glob("proofread-edit*.txt")))
            edit_file.write_text(FIXED_DESCRIPTION, encoding="utf-8")
            return ""
        return ""  # 其余问句一律回车（逐段陪跑的交回口回车＝停下）

    assert _resume(tmp_path, monkeypatch, directory, fake_prompt) == 0
    out = capsys.readouterr().out

    assert not calls, "就地改写不该触发视觉调用"
    record = _record(directory)
    assert record["description"] == FIXED_DESCRIPTION, "改写没写回记录"
    assert record["sha256"] == before["sha256"], "同 sha 改写却动了钉子"
    edits = record["manual_edits"]
    assert len(edits) == 1, f"人工改写没留痕：{edits}"
    assert edits[0]["before"] == before["description"]
    assert edits[0]["after"] == FIXED_DESCRIPTION
    assert edits[0]["at"].strip(), "改写没记时间"
    assert not list(directory.glob("proofread-edit*.txt")), "临时文本文件没删"
    assert "已更新" in out
    # 下游取材口立刻拿到新值（登记与段词同源）
    assert proofread_descriptions(load_session(directory).stage_state)["红裙女孩"] == \
        FIXED_DESCRIPTION


def test_failed_temp_cleanup_warns_on_stderr(tmp_path, monkeypatch, capsys):
    """临时文件删不掉时 stderr 必须说一句：清理失败不中断流程，但不许静默留垃圾。

    「绝不静默失败」是仓里的红线；清理又处在 ``finally`` 里，吞掉异常就等于把一个
    真实的「文件被占用」现场变成没人知道的小毛病。
    """
    _ask0, directory = _settle_girl_with_chinese_omission(tmp_path, monkeypatch)
    capsys.readouterr()

    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == EDIT_FILENAME:
            raise OSError("被别的程序占用")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    def fake_prompt(text: str) -> str:
        if "改素材画面事实" in text:
            return "2"
        if "改好后回车" in text:
            edit_file = next(iter(directory.glob("proofread-edit*.txt")))
            edit_file.write_text(FIXED_DESCRIPTION, encoding="utf-8")
            return ""
        return ""

    assert _resume(tmp_path, monkeypatch, directory, fake_prompt) == 0
    err = capsys.readouterr().err

    assert EDIT_FILENAME in err, f"清理失败没有在 stderr 说一句：{err!r}"
    assert "临时文件" in err
    # 清理是收尾动作：它失败不该抹掉已经写回的改写。
    assert _record(directory)["description"] == FIXED_DESCRIPTION


def test_edit_to_same_text_is_not_written(tmp_path, monkeypatch, capsys):
    """未改动 → 不落盘并提示；再回车一次仍没改也照旧；用户 q 取消。"""
    _ask0, directory = _settle_girl_with_chinese_omission(tmp_path, monkeypatch)
    before = _record(directory)
    capsys.readouterr()

    asked: list[str] = []
    attempts = {"left": 2}

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if "改素材画面事实" in text:
            return "2"
        if "改好后回车" in text:
            if attempts["left"] > 0:
                attempts["left"] -= 1
                return ""      # 用户没动文件（或存了同样的内容）
            return "q"          # 放弃这次修复
        return ""

    assert _resume(tmp_path, monkeypatch, directory, fake_prompt) == 0
    out = capsys.readouterr().out

    assert "没有变化" in out, "未改动没有提示"
    assert _record(directory)["description"] == before["description"], "未改动却落了盘"
    assert not _record(directory)["manual_edits"], "未改动却留了痕"
    assert not list(directory.glob("proofread-edit*.txt")), "取消后临时文件没删"


def test_empty_edit_is_rejected_and_can_be_retried(tmp_path, monkeypatch, capsys):
    """改后为空 → 不落盘并提示；改对了再回车才写回。"""
    _ask0, directory = _settle_girl_with_chinese_omission(tmp_path, monkeypatch)
    capsys.readouterr()

    attempts = {"left": 1}

    def fake_prompt(text: str) -> str:
        if "改素材画面事实" in text:
            return "2"
        if "改好后回车" in text:
            edit_file = next(iter(directory.glob("proofread-edit*.txt")))
            if attempts["left"]:
                attempts["left"] = 0
                edit_file.write_text("   \n", encoding="utf-8")
            else:
                edit_file.write_text(FIXED_DESCRIPTION, encoding="utf-8")
            return ""
        return ""

    assert _resume(tmp_path, monkeypatch, directory, fake_prompt) == 0
    out = capsys.readouterr().out

    assert "改后为空" in out, "空改写没有提示"
    assert _record(directory)["description"] == FIXED_DESCRIPTION


def test_mixed_after_edit_asks_before_keeping(tmp_path, monkeypatch, capsys):
    """改后仍中英混排 → 打印一行并确认；答「不保留」时描述保持原样。"""
    _ask0, directory = _settle_girl_with_chinese_omission(tmp_path, monkeypatch)
    before = _record(directory)
    capsys.readouterr()

    def fake_prompt(text: str) -> str:
        if "改素材画面事实" in text:
            return "2"
        if "改好后回车" in text:
            edit_file = next(iter(directory.glob("proofread-edit*.txt")))
            # 改了、但仍是中英混排（与改前**不同**，否则先撞「内容没有变化」那一档）
            edit_file.write_text(GIRL_PROMPT + "；" + CHINESE_OMISSION + "，另有木槌",
                                 encoding="utf-8")
            return ""
        return ""

    assert _resume(tmp_path, monkeypatch, directory, fake_prompt,
                   confirms=[("保留这次修改", False)]) == 0
    out = capsys.readouterr().out

    assert "中英混排" in out, "改后混排没有再报一行"
    assert _record(directory)["description"] == before["description"], \
        "答了「不保留」却写回了混排文本"
    assert not _record(directory)["manual_edits"]
