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

**AC9 变异检验（2026-10-20 实测三处，本文件为判据所在）**：
1. 摘掉语义层并排展示（把 ``render_ref_segment_semantic_check`` 的调用换成一行抬头）
   → ``test_reached_verdict_shows_both_ends_beside_their_frames`` 红（1 条）；
2. 五档出路砍成三档（``_REF_OUTCOME_ORDER`` 只留 停下/重跑/继续）→ 6 条红；
3. 拆掉锁死判据（删掉 ``revise_ref_segment_body`` 里的
   ``assert_locked_annotations_intact`` 调用）→ 2 条红（本文件 1 条 +
   ``test_ref_segment_revision.py`` 1 条）。
三处扰动都当场还原，全量复跑 1004 passed / 5 skipped。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.ref_segments import (
    SEGMENT_EVENT_ENCOUNTER,
    SEGMENT_EVENT_OUTPUT,
    SEGMENT_EVENT_REWRITE,
    read_segment_ledger,
)
from minimax_h3_prompt.tools import reference_auditor
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

    def invoke(self, request: str, config=None):
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




def _run(tmp_path, monkeypatch, handbacks: list[str], *, verdicts: list[bool],
         llm: object | None = None) -> tuple[list[str], int]:
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

def test_reached_verdict_shows_both_ends_beside_their_frames(tmp_path, monkeypatch, capsys, h3_tier_video):
    """段 1 交回后：首/末帧都剥出来、都读了图，并与声明的起止状态并排摆出。"""
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
    _, code = _run(tmp_path, monkeypatch, [str(first), str(second)],
                   verdicts=[True, True])
    assert code == 0
    out = capsys.readouterr().out

    assert "[本段语义核验] 第 1/2 段" in out
    # 声明与读图结果并排（AC1）：声明在上、实际画面紧跟在下
    assert SEG1_STATE in out
    assert _FIRST_READING in out and _LAST_READING in out
    assert out.index(SEG1_STATE) < out.index(_FIRST_READING)

    # 首帧与末帧两张图都真剥出来了，且路径当面给出（文件名带本段尝试次数，见后文那条）
    frames = sorted((tmp_path / "sessions").rglob("segment-01-*.png"))
    assert [f.name for f in frames] == ["segment-01-try1-first.png",
                                        "segment-01-try1-last.png"]
    assert "segment-01-try1-first.png" in out and "segment-01-try1-last.png" in out

    # 遭遇落账（AC8）：判定与出路逐次可查，且**不写**首尾帧链的 gate-log
    encounters = _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)
    assert [(r["segment"], r["verdict"], r["outcome"], r["attempt"]) for r in encounters] == [
        (1, "reached", "continue", 1), (2, "reached", "continue", 1)]
    assert encounters[0]["first_frame"].endswith("segment-01-try1-first.png")
    assert encounters[0]["last_frame"].endswith("segment-01-try1-last.png")
    assert not list((tmp_path / "sessions").rglob("gate-log.jsonl")), \
        "多参的语义人裁不许写进首尾帧链的闸门遭遇账"

    # 通过行仍然一段一行（#55 AC5 的契约不变）
    outputs = _ledger(tmp_path, SEGMENT_EVENT_OUTPUT)
    assert [r["segment"] for r in outputs] == [1, 2]


def test_not_reached_verdict_offers_five_outcomes_and_stops_by_default(tmp_path, monkeypatch, capsys, h3_tier_video):
    """判未达到：五档出路都摆出来，**回车＝停下**（默认不继续），且不落通过行。"""
    first = h3_tier_video(10.0, "seg1.avi")
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


def test_explicit_accept_continues_without_changing_the_prompt(tmp_path, monkeypatch, capsys, h3_tier_video):
    """「显式接受」＝明知未达到仍继续（AC2 系统只劝阻、不替人拍板），且词不动。"""
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
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

def test_rewrite_prompt_writes_feedback_into_the_segment_file(tmp_path, monkeypatch, capsys, h3_tier_video):
    """「修改提示词」：收意见 → 只改画面正文 → 写回 segment-01.md 并重出给人看。"""
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
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


def test_rerun_segment_keeps_the_prompt_untouched(tmp_path, monkeypatch, capsys, h3_tier_video):
    """「重跑本段」＝**不改词**再掷：段文件逐字不变、不落改写账，只记一次遭遇。"""
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
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


def test_each_attempt_keeps_its_own_frames(tmp_path, monkeypatch, capsys, h3_tier_video):
    """每轮遭遇的帧图**不许互相覆盖**（复审采纳项）。

    账里记帧图路径的理由是「事后复跑那次判定的唯一入口」——定名覆盖（segment-01-first.png）
    会让除最后一轮外的旧遭遇行指向**新一轮**的帧，那句话就不成立了；本仓对视频记 SHA256
    正是为对抗这类可变引用，帧图同理。文件名带该段的尝试次数。
    """
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
    _, code = _run(tmp_path, monkeypatch, [str(first), "3", str(first), str(second)],
                   verdicts=[False, True, True])
    assert code == 0
    names = sorted(p.name for p in (tmp_path / "sessions").rglob("segment-01-*.png"))
    assert names == ["segment-01-try1-first.png", "segment-01-try1-last.png",
                     "segment-01-try2-first.png", "segment-01-try2-last.png"]

    encounters = _ledger(tmp_path, SEGMENT_EVENT_ENCOUNTER)
    assert encounters[0]["first_frame"].endswith("segment-01-try1-first.png")
    assert encounters[1]["first_frame"].endswith("segment-01-try2-first.png")
    for row in encounters:
        for key in ("first_frame", "last_frame"):
            assert Path(row[key]).is_file(), f"{key} 指向的帧图不存在：{row[key]}"


# ---------------------------------------------------------------------------
# 复审采纳项：重过校验的口径必须与交付时同源
# ---------------------------------------------------------------------------

def _segment_for(plan, *, total_s: int, total_segments: int) -> str:
    """按**交付口径**（``run_ref_segmentation`` 的 total_int）拼一段自包含六段式。"""
    from minimax_h3_prompt.segment_prompts import (
        assemble_ref_segment,
        render_ref_segment_annotations,
    )

    return assemble_ref_segment(
        subject_definitions="<Picture 1> is 红裙女孩.\n<Picture 2> is 青铜钥匙.",
        retention_analysis="<Picture 1>: fully_preserved - ok.",
        task_type="reference generation", summary_body="The girl sets the jar down.",
        detailed_body="[Shot 1] A medium shot frames the girl raising the jar.",
        overall_soundscape="Soft room tone continues.", non_diegetic_music="N/A",
        annotations=render_ref_segment_annotations(
            plan.index + 1, total_segments, plan.start_s, plan.end_s, total_s,
            plan.start_state, plan.end_hook),
    )


def test_rewrite_revalidation_uses_the_delivery_total_not_the_last_plan_end(
        tmp_path, monkeypatch, capsys, h3_tier_video):
    """末段 end 可以略超全片时长（规划器留余量）——重过校验必须用**交付时那个** total。

    ``segment_planner.parse_segment_plan``：「最后一段 end 可以略超 total（留余量）但不许
    欠」，所以 ``plans[-1].end_s`` 未必等于注记句里的 "of the full Ns video"。拿它当
    total，改写后的重过校验会算出另一个期望注记，打出**假的 SEGMENT_TIMELINE_MISSING**
    ——而同一份文本在交付时按 total_int 校验是通过的（同一个校验器两处接线给了两个口径）。
    """
    from minimax_h3_prompt.ref_segments import RefSegmentResult
    from minimax_h3_prompt.segment_planner import SegmentPlan

    film_total, plans = 20, [
        SegmentPlan(index=0, start_s=0, end_s=10, shots_in_segment=(1,),
                    summary="起", start_state="the jar is held", end_hook="the jar is raised"),
        # 11s 段落在 4–15s 档内，但 end=21 超出全片 20s（规划器允许的那点余量）
        SegmentPlan(index=1, start_s=10, end_s=21, shots_in_segment=(2,),
                    summary="收", start_state="the jar is raised", end_hook="the jar is set down"),
    ]
    texts = [_segment_for(p, total_s=film_total, total_segments=len(plans)) for p in plans]
    seg_dir = tmp_path / "segments"
    seg_dir.mkdir(parents=True)
    paths = []
    for position, text in enumerate(texts, 1):
        path = seg_dir / f"segment-{position:02d}.md"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    result = RefSegmentResult(plans=plans, texts=texts, segment_paths=paths,
                              total_s=film_total)

    v1 = h3_tier_video(10.0, "seg1.avi")
    v1b = h3_tier_video(10.0, "seg1b.avi")
    v2 = h3_tier_video(11.0, "seg2.avi")
    _silence(monkeypatch)
    monkeypatch.setattr(reference_auditor, "describe_image", _fake_describe_image)
    monkeypatch.setattr(model_factory, "build_chat_model",
                        lambda: _StubLLM("[Shot 1] The girl sets the jar down on the table."))
    monkeypatch.setattr(wizard, "_confirm", _confirm_stub([False, True, True]))
    _fake_inputs(monkeypatch, [str(v1), "2", "末态应该是罐子已经放下", str(v1b), str(v2)])

    assert wizard._run_ref_segment_handoff(result, tmp_path) is True
    out = capsys.readouterr().out
    assert "SEGMENT_TIMELINE_MISSING" not in out, \
        f"同一份文本交付时是过的，重过校验不许按另一个 total 判它缺注记：{out[-1500:]}"
    assert "SEGMENT_STATE_MISSING" not in out


# ---------------------------------------------------------------------------
# AC5 / AC7：改写有牙——锁死那两句响错，交付前校验当面打
# ---------------------------------------------------------------------------

def test_tampering_with_the_locked_lines_keeps_the_old_prompt(tmp_path, monkeypatch, capsys, h3_tier_video):
    """模型顺手改了起止状态声明 → 响错、不采用这一轮、段文件保持原样（AC5）。"""
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
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


def test_rewritten_prompt_is_revalidated_and_problems_are_shown(tmp_path, monkeypatch, capsys, h3_tier_video):
    """改写后重过交付前校验：不合格**当面打出**（AC7），但不静默丢弃这一轮。"""
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
    # 指代前文 = 校验器 error 级（SEGMENT_ANAPHORA）：每段独立生成、模型无跨段记忆
    llm = _StubLLM("[Shot 1] As mentioned before, the girl lowers the jar.")

    _, code = _run(tmp_path, monkeypatch,
                   [str(first), "2", "补一句衔接", str(first), str(second)],
                   verdicts=[False, True, True], llm=llm)
    assert code == 0
    out = capsys.readouterr().out
    assert "SEGMENT_ANAPHORA" in out, f"改写后的校验问题必须当面打出：{out[-2000:]}"
    assert "As mentioned before" in _segment_text(tmp_path, 1)
