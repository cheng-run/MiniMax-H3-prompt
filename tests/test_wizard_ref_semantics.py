"""向导·多参语义层验收与五档出路（issue #56）：交回 → 剥首末帧 → 读图 → 并排 → 人判 → 出路。

覆盖票面 AC：
- AC1 每段交回后剥**首帧与末帧**、读图，并与本段 ``start_state``/``end_hook`` 并排展示；
- AC2 语义判定**由人下结论**，系统只劝阻、不硬阻断、不替人拍板（「显式接受」照样放行）；
- AC3 判为未达到时给五档出路，**默认不继续**（回车＝停下）；
- AC4 「修改提示词」收意见写回本段提示词；与「重跑本段」（不改词）分得清；
- AC5 改写只动画面正文，那两句被改动时**响错**、原词保持；
- AC6 改写不设硬上限，每轮落一行账（含改写前后全文）；
- AC7 改写后重过交付前校验，不合格当面打出；
- AC8 每次遭遇记入**段产出账**，**不写** gate-log.jsonl。

薄接线：走真实调用链（``run_wizard`` 的多参阶段 2），只打桩 LLM、读图与交互输入。
视频与剥帧都是**真**的（可证层判帧数档、剥帧走 cv2）；只有读图打桩（真读图要 API key，
历史教训：暗读真 key 的用例只在本机能过）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.ref_segments import (
    SEGMENT_EVENT_ENCOUNTER,
    SEGMENT_EVENT_OUTPUT,
    SEGMENT_EVENT_REWRITE,
    read_segment_ledger,
)
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.tools.preflight import EXPECTED_VIDEO_SIZE, H3_TIER_FRAMES
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# 20s 两镜（切点 0/10）→ 机械打包恰得两段各 10s。两句话是「展示的是哪一段」的判据。
SEG1_BODY = "A medium shot frames the girl walking through the garden gate."
SEG2_BODY = "the shot cuts to a close-up of the bronze key in her hand"
# 机械拆段的 start_state/end_hook 取自窗内首/末镜头块的首末句（见 ref_segments._derive_plan）：
# 两段各自只有一句，所以首末态同句——并排展示的对照物就是它们。
SEG1_STATE = "A medium shot frames the girl walking through the garden gate."
SEG2_STATE = "the shot cuts to a close-up of the bronze key in her hand"

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n"
    "<Picture 1> is 红裙女孩.\n<Picture 2> is 秋日庭院.\n"
    "<Picture 3> is 青铜钥匙.\n<Picture 4> is 蓝衫少年.\n\n"
    "summary:\n[reference generation] The target video shows the girl handing over the key.\n\n"
    "retention_analysis:\n<Picture 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n"
    f"[Shot 1] {SEG1_BODY}\n"
    f"[Shot 2] At 00:10.000, {SEG2_BODY}.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)

_FIRST_READING = "画面里女孩正走过庭院小径"
_LAST_READING = "画面停在青铜钥匙的特写上"


def _fake_describe_image(path, prompt):
    """读图桩：按问句里的帧位给不同描述（首/末帧要能分别认出来）。"""
    return _LAST_READING if "末帧" in prompt else _FIRST_READING


class _StubLLM:
    """「修改提示词」用的假模型：只在**改写口**答话，分段那几路一律抛错。

    分段（规划/回退重写）也会走 ``build_chat_model()``：不设这道闸，假模型的回复会被
    当成段提示词落盘，「改写前后对照」就成了「假回复 vs 假回复」——两臂同输入，测了个
    寂寞。抛错让分段回退到机械拆段（确定性，与 #55 的用例同形）。
    """

    def __init__(self, reply: str):
        self.reply = reply
        self.requests: list[str] = []

    def invoke(self, request: str):
        if "【改写意见】" not in request:
            raise RuntimeError("假模型只在改写口说话")
        self.requests.append(request)
        return self.reply


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


def _confirm_stub(verdicts: list[bool]):
    """确认桩：语义层那句（问「达到」）按 verdicts 逐次作答，其余按老规矩（含「继续」＝是）。"""
    queue = iter(verdicts)

    def confirm(text: str, default: bool = True) -> bool:
        if "达到" in text:
            return next(queue)
        return "继续" in text

    return confirm


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


def _make_video(path: Path, seconds: float, *, fps: int = 24) -> Path:
    """写一段真视频：帧数按 H3 实测档位取（可证层按帧数表判档，96 帧的假 4s 落不进档位）。"""
    frames = H3_TIER_FRAMES.get(int(round(seconds)), int(round(seconds * fps)))
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                             EXPECTED_VIDEO_SIZE)
    assert writer.isOpened(), "测试环境缺 MJPG 编码器"
    image = np.full((EXPECTED_VIDEO_SIZE[1], EXPECTED_VIDEO_SIZE[0], 3), 128, np.uint8)
    for i in range(frames):
        image[:] = 128 + (i % 8)
        writer.write(image)
    writer.release()
    actual = video_meta(path).get("duration_s", 0.0)
    assert abs(actual - frames / fps) < 0.05, f"测试视频时长不可控：{actual}"
    return path


def _run(tmp_path, monkeypatch, handbacks: list[str], *, verdicts: list[bool],
         llm_reply: str = "", llm: object | None = None) -> tuple[list[str], int]:
    """全程驱动：阶段 1 → 素材循环（四张全放弃）→ 阶段 2 → 逐段陪跑。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                     {"prompt_engineer": object()}))
    monkeypatch.setattr(
        wizard, "run_stage2",
        lambda state, brief, config, **kw: (dict(state), _SIX_SECTION_PROMPT))
    # 假模型没有 invoke：规划与回退重写都失败 → 走机械拆段（确定性）
    monkeypatch.setattr(model_factory, "build_chat_model",
                        lambda: llm if llm is not None else object())
    monkeypatch.setattr(reference_auditor, "describe_image", _fake_describe_image)
    monkeypatch.setattr(wizard, "_confirm", _confirm_stub(verdicts))
    tail = [TOPIC, "20", "", "4", "", "4", "", "4", "", "4", "4", *handbacks]
    asked = _fake_inputs(monkeypatch, tail)
    return asked, wizard.run_wizard(_cfg(tmp_path))


def _generation_dir(tmp_path) -> Path:
    return next((tmp_path / "sessions").rglob("session-state.json")).parent


def _ledger(tmp_path, event: str | None = None) -> list[dict]:
    return read_segment_ledger(_generation_dir(tmp_path), event=event)


def _segment_text(tmp_path, position: int) -> str:
    return next((tmp_path / "sessions").rglob(f"segment-{position:02d}.md")).read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1 / AC2 / AC8：并排展示、人下结论、遭遇落账
# ---------------------------------------------------------------------------

def test_reached_verdict_shows_both_ends_beside_their_frames(tmp_path, monkeypatch, capsys):
    """段 1 交回后：首/末帧都剥出来、都读了图，并与声明的起止状态并排摆出。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    _, code = _run(tmp_path, monkeypatch, [str(first), str(second)],
                   verdicts=[True, True])
    assert code == 0
    out = capsys.readouterr().out

    assert "[本段语义核验] 第 1/2 段" in out
    # 声明与读图结果并排（AC1）：声明在上、实际画面紧跟在下
    assert SEG1_STATE in out
    assert _FIRST_READING in out and _LAST_READING in out
    assert out.index(SEG1_STATE) < out.index(_FIRST_READING)

    # 首帧与末帧两张图都真剥出来了，且路径当面给出
    frames = sorted((tmp_path / "sessions").rglob("segment-01-*.png"))
    assert [f.name for f in frames] == ["segment-01-first.png", "segment-01-last.png"]
    assert "segment-01-first.png" in out and "segment-01-last.png" in out

    # 遭遇落账（AC8）：判定与出路逐次可查，且**不写**首尾帧链的 gate-log
    encounters = _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)
    assert [(r["segment"], r["verdict"], r["outcome"], r["attempt"]) for r in encounters] == [
        (1, "reached", "continue", 1), (2, "reached", "continue", 1)]
    assert encounters[0]["first_frame"].endswith("segment-01-first.png")
    assert encounters[0]["last_frame"].endswith("segment-01-last.png")
    assert not list((tmp_path / "sessions").rglob("gate-log.jsonl")), \
        "多参的语义人裁不许写进首尾帧链的闸门遭遇账"

    # 通过行仍然一段一行（#55 AC5 的契约不变）
    outputs = _ledger(tmp_path, SEGMENT_EVENT_OUTPUT)
    assert [r["segment"] for r in outputs] == [1, 2]


def test_not_reached_verdict_offers_five_outcomes_and_stops_by_default(tmp_path, monkeypatch, capsys):
    """判未达到：五档出路都摆出来，**回车＝停下**（默认不继续），且不落通过行。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    asked, code = _run(tmp_path, monkeypatch, [str(first), ""], verdicts=[False])
    assert code == 0
    out = capsys.readouterr().out

    for label in ("继续", "修改提示词", "重跑本段", "显式接受", "停下"):
        assert label in out, f"五档出路缺了「{label}」：{out[-1500:]}"
    assert "默认**不继续**" in out
    assert any("选择出路" in text and "回车=1 停下" in text for text in asked), asked[-4:]

    # 默认停下：通过行不落，遭遇照记（判定/出路/尝试次数）
    assert _ledger(tmp_path, SEGMENT_EVENT_OUTPUT) == []
    encounters = _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)
    assert [(r["segment"], r["verdict"], r["outcome"], r["attempt"]) for r in encounters] == [
        (1, "not_reached", "stop", 1)]
    assert SEG2_BODY not in out, "停下之后不许再出下一段"


def test_explicit_accept_continues_without_changing_the_prompt(tmp_path, monkeypatch, capsys):
    """「显式接受」＝明知未达到仍继续（AC2 系统只劝阻、不替人拍板），且词不动。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    _run(tmp_path, monkeypatch, [str(first), str(second)], verdicts=[True, True])
    before = _segment_text(tmp_path, 1)
    import shutil
    shutil.rmtree(tmp_path / "sessions")

    _, code = _run(tmp_path, monkeypatch, [str(first), "5", str(second)],
                   verdicts=[False, True])
    assert code == 0
    out = capsys.readouterr().out
    assert "显式接受" in out
    encounters = _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)
    assert [(r["verdict"], r["outcome"]) for r in encounters] == [
        ("not_reached", "accept"), ("reached", "continue")]
    assert [r["segment"] for r in _ledger(tmp_path, SEGMENT_EVENT_OUTPUT)] == [1, 2]
    assert _segment_text(tmp_path, 1) == before, "接受现状不等于改词"


# ---------------------------------------------------------------------------
# AC4：五档里的「修改提示词」与「重跑本段」分得清
# ---------------------------------------------------------------------------

def test_rewrite_prompt_writes_feedback_into_the_segment_file(tmp_path, monkeypatch, capsys):
    """「修改提示词」：收意见 → 只改画面正文 → 写回 segment-01.md 并重出给人看。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    old_text = None
    llm = _StubLLM("[Shot 1] A wide shot frames the girl lowering the jar onto the table.")
    asked: list[str] = []

    # 先跑一遍拿到改写前的段文件（同一套输入，只是这次走改写支路）
    _run(tmp_path, monkeypatch, [str(first), str(second)], verdicts=[True, True])
    old_text = _segment_text(tmp_path, 1)
    assert "lowering the jar" not in old_text

    import shutil
    shutil.rmtree(tmp_path / "sessions")

    asked, code = _run(tmp_path, monkeypatch,
                       [str(first), "2", "末态应该是罐子已经放下", str(first), str(second)],
                       verdicts=[False, True, True], llm=llm)
    assert code == 0
    out = capsys.readouterr().out

    # 收意见的问句当面问出来了，且改写结果落回**本段**文件
    assert any("改写意见" in text for text in asked), asked[-6:]
    assert "末态应该是罐子已经放下" in llm.requests[0]
    new_text = _segment_text(tmp_path, 1)
    assert "lowering the jar onto the table" in new_text
    assert new_text != old_text
    # 只动正文：定义块与锁死声明逐字不变（AC5）
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections
    assert (split_ref_sections(new_text)["subject_definitions"]
            == split_ref_sections(old_text)["subject_definitions"])
    assert (split_ref_sections(new_text)["detailed_description"].splitlines()[0]
            == split_ref_sections(old_text)["detailed_description"].splitlines()[0])
    # 新词重出给人看（不然用户不知道拿什么去重掷）
    assert "lowering the jar onto the table" in out

    # 每轮一行改写账，含改写前后全文（AC6）
    rounds = _ledger(tmp_path, SEGMENT_EVENT_REWRITE)
    assert len(rounds) == 1
    assert rounds[0]["segment"] == 1 and rounds[0]["round"] == 1
    assert rounds[0]["feedback"] == "末态应该是罐子已经放下"
    assert rounds[0]["prompt_before"] == old_text
    assert rounds[0]["prompt_after"] == new_text
    # 遭遇：改写这一轮与随后通过的那一轮都记（AC8）
    assert [(r["outcome"], r["attempt"]) for r in _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)] == [
        ("rewrite_prompt", 1), ("continue", 2), ("continue", 1)]


def test_rerun_segment_keeps_the_prompt_untouched(tmp_path, monkeypatch, capsys):
    """「重跑本段」＝**不改词**再掷：段文件逐字不变、不落改写账，只记一次遭遇。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    _run(tmp_path, monkeypatch, [str(first), str(second)], verdicts=[True, True])
    before = _segment_text(tmp_path, 1)
    import shutil
    shutil.rmtree(tmp_path / "sessions")

    _, code = _run(tmp_path, monkeypatch, [str(first), "3", str(first), str(second)],
                   verdicts=[False, True, True])
    assert code == 0
    out = capsys.readouterr().out
    assert "不改词" in out
    assert _segment_text(tmp_path, 1) == before
    assert _ledger(tmp_path, SEGMENT_EVENT_REWRITE) == []
    assert [(r["outcome"], r["attempt"]) for r in _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)] == [
        ("rerun", 1), ("continue", 2), ("continue", 1)]


# ---------------------------------------------------------------------------
# AC5 / AC7：改写有牙——锁死那两句响错，交付前校验当面打
# ---------------------------------------------------------------------------

def test_tampering_with_the_locked_lines_keeps_the_old_prompt(tmp_path, monkeypatch, capsys):
    """模型顺手改了起止状态声明 → 响错、不采用这一轮、段文件保持原样（AC5）。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    _run(tmp_path, monkeypatch, [str(first), str(second)], verdicts=[True, True])
    before = _segment_text(tmp_path, 1)
    import shutil
    shutil.rmtree(tmp_path / "sessions")

    llm = _StubLLM("[Shot 1] The girl sets the key down.\n"
                   "This segment ends with the key already on the table.")
    _, code = _run(tmp_path, monkeypatch,
                   [str(first), "2", "末态改成钥匙已经放下", str(first), str(second)],
                   verdicts=[False, True, True], llm=llm)
    assert code == 0
    out = capsys.readouterr().out
    assert "[错误] 改写失败" in out
    assert "锁死" in out
    assert _segment_text(tmp_path, 1) == before, "被拒的那一轮不许写回段文件"
    assert _ledger(tmp_path, SEGMENT_EVENT_REWRITE) == []
    # 遭照记：改写失败也算一次遭遇（用户随后重交并判通过）
    assert [r["outcome"] for r in _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)] == [
        "rewrite_prompt", "continue", "continue"]


def test_rewritten_prompt_is_revalidated_and_problems_are_shown(tmp_path, monkeypatch, capsys):
    """改写后重过交付前校验：不合格**当面打出**（AC7），但不静默丢弃这一轮。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    # 指代前文 = 校验器 error 级（SEGMENT_ANAPHORA）：每段独立生成、模型无跨段记忆
    llm = _StubLLM("[Shot 1] As mentioned before, the girl lowers the jar.")

    _, code = _run(tmp_path, monkeypatch,
                   [str(first), "2", "补一句衔接", str(first), str(second)],
                   verdicts=[False, True, True], llm=llm)
    assert code == 0
    out = capsys.readouterr().out
    assert "SEGMENT_ANAPHORA" in out, f"改写后的校验问题必须当面打出：{out[-2000:]}"
    assert "As mentioned before" in _segment_text(tmp_path, 1)
