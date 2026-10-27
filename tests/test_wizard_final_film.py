"""首尾帧链收尾自动接出成片（issue #96）：全段通过 → ``final.mp4``。

钉住的是**外部可见**的事实：成片在不在、帧数对不对、屏幕上有没有落点与段数、
失败时会话是否仍停在「分段进行中」（能续拼）。拼接判据本身（缺段/SHA/覆盖）
住在共用件里，已由 ``test_final_film.py`` 钉住；这里钉的是**向导有没有把它接上**——
「规则在、接线不在」是本仓反复栽过的形态（第 1 次是阈值未贯通，第 9 次是
``stream_usage`` 没开导致成本表看着还在、数是空的）。

两条产出路径（规划式与机械回退式）各一条端到端：全程驱动 ``run_wizard``，
只打桩 LLM／写段／读图与闸门，**ffmpeg 真拼**几段真视频。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import (
    model_factory,
    segment_planner,
    segment_prompts,
    session_store,
    shot_ledger,
)
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.segment_prompts import ShotPrompt
from minimax_h3_prompt.session_store import load_session, save_session
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.ui import wizard

FIXTURE = Path(__file__).parent / "fixtures" / "official_shot01_i2va.md"

# 两镜头的整片提示词：机械拆分得到 0-4s / 4-8s 两段，与 ``_plans(2)`` 的边界一致。
# 两个路径必须看到**同一套段序**，否则「两条路径各测一次」测的是两件事。
TWO_SHOT_PROMPT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:04.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _brief(duration: float = 8.0) -> Brief:
    return Brief(mode="base", variant="I2VA", duration=duration, style="写实",
                 plot="深夜便利店一只橘猫推门跳上柜台蹭店员的手")


def _start_session(tmp_path, *, duration: float = 8.0):
    """造一个「分段陪跑跑到一半」的会话，让 ``run_wizard`` 直接落到续接入口。

    这样不必重放阶段 1/2：那两步与本票无关，而它们要打的桩（澄清、素材、读图）
    会把用例拖成另一件事的回归。
    """
    gen = tmp_path / "sessions" / "某主题" / "GEN001"
    gen.mkdir(parents=True)
    save_session(gen, _brief(duration), {"shot_table": "[Shot 1] 深夜空店内……"},
                 status=session_store.STATUS_SEGMENTED_RUNNING)
    (gen / "video-prompt.md").write_text(TWO_SHOT_PROMPT, encoding="utf-8")
    return gen, load_session(gen)


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(sessions_root=str(tmp_path / "sessions"),
                           vision_model=SimpleNamespace(model="stub-vision"))


def _plans(total: int = 2) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=i, start_s=i * 4, end_s=(i + 1) * 4, shots_in_segment=(i + 1,),
                    summary=f"第 {i + 1} 段中文摘要", end_hook=f"第 {i + 1} 段末态")
        for i in range(total)
    ]


def _write_plans(directory: Path, total: int = 2) -> None:
    (directory / "segments").mkdir(parents=True, exist_ok=True)
    (directory / "segments" / "plan.json").write_text(
        json.dumps([plan.to_dict() for plan in _plans(total)], ensure_ascii=False, indent=2),
        encoding="utf-8")


def _stub_flow(monkeypatch, tmp_path) -> None:
    """打桩交互、闸门与读图；不动输入序列本身（新题只在 ``_prompt`` 里按题意作答）。

    ``_capture_bridge_frame`` 被拦住＝剥帧与读图都不发生（那是另一张票的地盘），
    但**用户交回的路径**照样经 ``on_pass`` 落账、照样进入拼接——用例要测的正是
    「账上的视频真的被接起来」。
    """
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    monkeypatch.setattr(wizard, "_capture_bridge_frame",
                        lambda *a, **k: ([], "桥接帧画面", frame))
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())


def _answers(monkeypatch, sequence: list[str]) -> list[str]:
    iterator = iter(sequence)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(iterator, "")

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _video(tmp_video, name: str, *, frames: int = 96):
    """4s / 24fps 的真视频：与 ``_plans`` 的段时长一致，成片时长核对不至于响得没道理。"""
    return tmp_video(name, frames=frames, fps=24, width=128, height=72)


# ---------------------------------------------------------------------------
# 1. 规划式（v2）端到端：全段通过 → final.mp4
# ---------------------------------------------------------------------------

def test_v2_path_concats_the_film_after_every_segment_passes(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, _session = _start_session(tmp_path)
    first = _video(tmp_video, "seg1.mp4")
    last = _video(tmp_video, "seg2.mp4")
    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans(2))
    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))
    # 续接菜单选 1 → 段 1 生成好 → 交段 1 视频 → 段 2 生成好 → 交末段视频
    _answers(monkeypatch, ["", "", str(first), "", str(last)])

    wizard.run_wizard(_cfg(tmp_path))

    final = gen / "final.mp4"
    out = capsys.readouterr().out
    assert final.is_file(), out
    assert video_meta(final)["frames"] == 96 + 96 - 1, \
        "两段各 96 帧，接缝裁掉第 2 段的首帧（issue #98）"
    assert str(final) in out, "落点必须当面给出"
    assert "2 段" in out, "段数必须当面给出"
    assert load_session(gen).status == session_store.STATUS_COMPLETED


# ---------------------------------------------------------------------------
# 2. 机械回退式端到端：段表在收尾补写，成片同样接得出来
# ---------------------------------------------------------------------------

def test_fallback_path_writes_its_segment_table_and_concats(
        tmp_path, monkeypatch, tmp_video, capsys):
    """回退路径没有 plan 层，收尾必须补写 ``segments/plan.json``（段序真源）。

    只在**全部段通过之后**写：中途写会让中断后的续跑在 ``cached and done`` 分支
    误走规划式重写——机械段边界与规划器不是一套，按它续写等于换了一套段。
    """
    gen, _session = _start_session(tmp_path)
    first = _video(tmp_video, "seg1.mp4")
    last = _video(tmp_video, "seg2.mp4")
    _stub_flow(monkeypatch, tmp_path)

    def broken_planner(*_a, **_k):
        raise RuntimeError("规划挂了，按 [Shot N] 机械拆分")

    monkeypatch.setattr(segment_planner, "plan_segments", broken_planner)
    # 回退写段也打桩：本用例测收尾，不测声音重写；返回 None＝用机械拆分的原文。
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: None)
    _answers(monkeypatch, ["", "", str(first), "", str(last)])

    wizard.run_wizard(_cfg(tmp_path))

    final = gen / "final.mp4"
    out = capsys.readouterr().out
    assert final.is_file(), out
    assert video_meta(final)["frames"] == 96 + 96 - 1
    plan = json.loads((gen / "segments" / "plan.json").read_text(encoding="utf-8"))
    assert [(item["start_s"], item["end_s"]) for item in plan] == [(0, 4), (4, 8)]
    assert [item["shots_in_segment"] for item in plan] == [[1], [2]]
    assert all(item["summary"] == "" and item["end_hook"] == "" for item in plan), \
        "机械段表没有 plan 层的概述与末态声明，留空而不是编一句"
    assert load_session(gen).status == session_store.STATUS_COMPLETED


def test_fallback_writes_no_table_until_every_segment_has_passed(
        tmp_path, monkeypatch, tmp_video):
    """机械段表只在**全部段通过之后**补写。

    中途写会让中断后的续跑在 ``cached and done`` 分支误走规划式重写：机械段边界与
    规划器不是一套，按它续写等于换了一套段——而且**不报错**。
    """
    gen, _session = _start_session(tmp_path)
    first = _video(tmp_video, "seg1.mp4")
    _stub_flow(monkeypatch, tmp_path)

    def broken_planner(*_a, **_k):
        raise RuntimeError("规划挂了，按 [Shot N] 机械拆分")

    monkeypatch.setattr(segment_planner, "plan_segments", broken_planner)
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: None)
    # 末段视频回车＝放弃继续（`_acquire_last_video` 的停下口）
    _answers(monkeypatch, ["", "", str(first), "", ""])

    wizard.run_wizard(_cfg(tmp_path))

    assert not (gen / "segments" / "plan.json").exists(), \
        "没跑完就写段表，会把中断后的续跑引到规划式重写"
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


# ---------------------------------------------------------------------------
# 2b. 机械回退但段提示词没有时间戳：拼不出成片，但会话必须收尾（不循环）
# ---------------------------------------------------------------------------

def _no_timestamp_segments() -> list[ShotPrompt]:
    """机械拆分的段**没有时间窗**：补不出段表（``_write_mechanical_plan`` 的 False 形态）。"""
    return [
        ShotPrompt(shot_number=1, text="[Shot 1] The store interior is still.",
                   start_seconds=None, duration_seconds=None),
        ShotPrompt(shot_number=2, text="[Shot 2] The cat walks to the counter.",
                   start_seconds=None, duration_seconds=None),
    ]


def _stub_timestampless_fallback(monkeypatch, tmp_path) -> list[str]:
    """打桩「规划失败 → 机械回退」且段提示词没有时间窗；返回用户输入序列。"""
    first = _fake_video(tmp_path, "seg1.mp4")
    last = _fake_video(tmp_path, "seg2.mp4")
    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_planner, "plan_segments", _broken_planner)
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt", lambda *a, **k: None)
    monkeypatch.setattr(segment_prompts, "split_shots_from_prompt",
                        lambda *a, **k: _no_timestamp_segments())
    return ["", "", str(first), "", str(last)]


def _fake_video(tmp_path, name: str) -> Path:
    """一个真实存在的占位视频文件（本组用例不真拼它，账上只需路径可读）。"""
    path = tmp_path / name
    path.write_bytes(b"fake-mp4")
    return path


def _broken_planner(*_a, **_k):
    raise RuntimeError("规划挂了，按 [Shot N] 机械拆分")


def test_timestampless_mechanical_fallback_completes_instead_of_looping(
        tmp_path, monkeypatch, capsys):
    """没有时间戳 → 成片不可用，但**会话必须标成完成**（复审裁定，2026-10-27）。

    修复前的形态：回退流程只打一句「拼接不可用」就 ``return True``，收尾处因
    ``plan.json`` 缺席拒绝拼接、**不**标完成；重进命中 ``RESUME_NO_TABLE``，出口还是
    不标完成——用户每进一次向导都回到同一句话，永远收不了尾。而这种形态**补不出**
    段表（段提示词本来就没有时间窗），「恢复段表」这条路对它根本不存在。
    所以它必须像旧会话「拼接不可用」那样**说破原因并收尾**。
    """
    gen, _ = _start_session(tmp_path)
    _answers(monkeypatch, _stub_timestampless_fallback(monkeypatch, tmp_path))

    wizard.run_wizard(_cfg(tmp_path))

    out = capsys.readouterr().out
    assert not (gen / "final.mp4").exists(), "段表补不出来，不能产出伪成片"
    assert "成片不可用" in out, out[-800:]
    assert "时间戳" in out, "原因必须写清（不是笼统的「拼接不可用」）"
    assert load_session(gen).status == session_store.STATUS_COMPLETED, \
        "这种形态补不出段表，停在 running 只会让重进向导无限循环"


def test_reentering_after_a_timestampless_film_is_not_offered_for_resume(
        tmp_path, monkeypatch, capsys):
    """收尾之后**再进向导不再回到这条续接**：它已不在「未完成」列表里。

    死循环的全部机制就是「会话停在 segmented_running + 重进落回续接」；标成完成
    之后那两件事都不成立。这里直接把续接入口打成断言，防的是将来谁把它改回 running。
    """
    gen, _ = _start_session(tmp_path)
    _answers(monkeypatch, _stub_timestampless_fallback(monkeypatch, tmp_path))
    wizard.run_wizard(_cfg(tmp_path))
    capsys.readouterr()

    resumable = [s.directory for s in session_store.find_awaiting_sessions(
        tmp_path / "sessions")]
    assert gen not in resumable, "已收尾的会话还被列为可续接——下次进向导又会绕回来"

    def boom(*_a, **_k):
        raise AssertionError("已收尾的会话又被送进分段陪跑续接")

    monkeypatch.setattr(wizard, "_resume_segmented", boom)
    monkeypatch.setattr(wizard, "_phase1_new", lambda config: None)
    assert wizard.run_wizard(_cfg(tmp_path)) == 1


# ---------------------------------------------------------------------------
# 2c. 部分续跑不丢首帧锚：每段通过后落盘（issue #94 复审）
# ---------------------------------------------------------------------------

ANCHOR_DESC = "便利店收银台中景：玻璃店门完整关闭，画面中没有猫；店员右手平放在台面上。"

# 未被任何用例打桩过的真写段函数（见 ``test_fallback_resume_keeps_...`` 的说明）。
_REAL_REWRITE = segment_prompts.rewrite_segment_prompt


class _RecordingLLM:
    """记录写段请求的桩模型：断言的是**出站请求**里有没有那个读图描述。"""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        return "For the target video, at 0.00 seconds, [Shot 1] a rewrite."


def test_fallback_resume_keeps_the_bridge_anchor_for_the_next_segment(
        tmp_path, monkeypatch, capsys):
    """第 1 段通过后中断 → 续跑时第 2 段的**写段请求**必须仍带着第 1 段桥接帧的读图结果。

    修复前：``_anchor_state`` 只写内存，``save_session`` 只在 ``_run_long_form`` 首尾各
    一次——续跑时账上已通过的那段不再过闸门（不再剥帧读图），而 state 里又没有这条锚，
    写段请求就退回「图片是唯一事实源」（issue #94 引入的回归）。修法是**每段通过后
    落盘**，让续跑零额外提问、零额外读图。
    """
    gen, _ = _start_session(tmp_path)
    video = _make_fake_video(monkeypatch, tmp_path)
    reads: list[str] = []

    def fake_describe(path, instruction=None):
        reads.append(str(path))
        return ANCHOR_DESC

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(segment_planner, "plan_segments", _broken_planner)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)

    # 第一轮：段 1 确认 → 段 1 视频（闸门通过，锚写进 state 并落盘）→ 段 2 确认
    #        → 末段视频回车（放弃继续）＝一次「跑完第 1 段后中断」。
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt",
                        lambda *a, **k: "REWRITTEN")
    _answers(monkeypatch, ["", str(video), "", ""])
    session = load_session(gen)
    wizard._run_segmented_flow(session.brief, session, TWO_SHOT_PROMPT,
                               state=session.stage_state)
    capsys.readouterr()

    # 续跑：从盘上重新读会话（这正是修复前丢锚的那一步）。
    resumed = load_session(gen)
    assert resumed.stage_state.get("bridge_frame_descriptions") == [
        {"segment": 1, "description": ANCHOR_DESC},
    ], "第 1 段通过后锚没有落盘——续跑时下一段就退回无锚定"

    recorder = _RecordingLLM()
    # 真 ``rewrite_segment_prompt``（真组装写段请求）＋记录桩模型：要断言的是**出站请求**。
    # 原函数在**模块导入时**就摘下来（``_REAL_REWRITE``）：打桩之后
    # ``segment_prompts.rewrite_segment_prompt`` 已经变成壳，再取它就是自我递归。
    monkeypatch.setattr(
        segment_prompts, "rewrite_segment_prompt",
        lambda segment, full_prompt, llm, **kw:
            _REAL_REWRITE(segment, full_prompt, recorder, **kw))
    reads.clear()
    _answers(monkeypatch, ["", ""])  # 段 2 确认 → 末段视频回车

    wizard._run_segmented_flow(resumed.brief, resumed, TWO_SHOT_PROMPT,
                               state=resumed.stage_state)

    assert recorder.requests, "续跑没有重写第 2 段？"
    assert ANCHOR_DESC in recorder.requests[0], \
        "续跑后第 2 段的写段请求丢了第 1 段桥接帧的读图描述"
    assert reads == [], "已通过段的桥接帧在续跑时被重新读图了（多付一次、并要求重交视频）"
    assert shot_ledger.read_shot_ledger(gen) != [], "第 1 段的通过行不该丢"


def _broken_planner(*_a, **_k):
    raise RuntimeError("规划挂了，按 [Shot N] 机械拆分")


def _make_fake_video(monkeypatch, tmp_path) -> Path:
    """真剥帧/读图都打桩，但视频文件本身要存在（段产出账要算 SHA256）。"""
    from minimax_h3_prompt.tools import frame_auditor

    video = tmp_path / "seg1.mp4"
    video.write_bytes(b"fake-mp4")

    def fake_extract(video_path, output_path, **_kwargs):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return out

    monkeypatch.setattr(frame_auditor, "extract_last_frame", fake_extract)
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    return video


def test_v2_resume_keeps_the_bridge_anchor_for_the_next_segment(
        tmp_path, monkeypatch, capsys):
    """同一条回归的 v2 一侧：第 1 段通过后中断，续跑写第 2 段时锚仍在。

    两条产出路径各存一次（v2 在闸门放行处、回退在下一段闸门放行处）——只修一条的话，
    另一条照样在续跑时丢锚，而丢的是「不报错」的那种。
    """
    gen, _ = _start_session(tmp_path)
    video = _make_fake_video(monkeypatch, tmp_path)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: ANCHOR_DESC)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)

    class _FirstOnlyLLM:
        """第 1 次调用给合格正文（让段 1 过闸），之后一律空串＝写段失败（模拟中断）。"""

        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, request, config=None):
            self.calls += 1
            return FIXTURE.read_text(encoding="utf-8") if self.calls == 1 else ""

    session = load_session(gen)
    _answers(monkeypatch, ["", str(video), ""])  # 段 1 确认 → 段 1 视频 → 末段视频回车
    wizard._run_segmented_flow_v2(session, _plans(2), session.stage_state,
                                  _FirstOnlyLLM())
    capsys.readouterr()

    resumed = load_session(gen)
    assert resumed.stage_state.get("bridge_frame_descriptions") == [
        {"segment": 1, "description": ANCHOR_DESC},
    ], "v2 路径第 1 段通过后锚没有落盘"
    assert not (gen / "segments" / "shot-02.md").exists(), \
        "第 2 段没写出来才不会在续跑时被复用——否则这里测不到出站请求"

    recorder = _RecordingLLM()
    _answers(monkeypatch, ["", ""])  # 段 2 确认 → 末段视频回车
    wizard._run_segmented_flow_v2(resumed, _plans(2), resumed.stage_state,
                                  recorder, plans_from_cache=True)

    assert recorder.requests, "续跑没有重写第 2 段？"
    assert ANCHOR_DESC in recorder.requests[0], \
        "v2 续跑后第 2 段的写段请求丢了第 1 段桥接帧的读图描述"


def test_missing_table_with_timestamps_is_not_treated_as_unavailable(
        tmp_path, monkeypatch, tmp_video, capsys):
    """段表**丢了**但段提示词有时间戳 → 这不是「补不出」，仍按原行为停住等恢复。

    两种「plan.json 缺席」必须分开（2026-10-27 复审）：段提示词没有时间窗时永远补不出
    段表（说破原因并收尾）；而这里只是文件丢了，恢复 segments/plan.json 后还能拼——
    若也标完成，用户就再也拼不出那条**本来拼得出来**的片子。收尾处直接调
    （``_run_long_form`` 的续拼短路会在同一形态先说「恢复段表」并停住，走不到这里）。
    """
    gen, session = _start_session(tmp_path)
    shot_ledger.append_shot_output(
        gen, segment=1, start_s=0, end_s=4, video_path=_video(tmp_video, "s1.mp4"))
    shot_ledger.append_shot_output(
        gen, segment=2, start_s=4, end_s=8, video_path=_video(tmp_video, "s2.mp4"))

    result = wizard._assemble_film_and_complete(
        gen, session.brief, session.stage_state, prompt=TWO_SHOT_PROMPT)

    out = capsys.readouterr().out
    assert result == {}
    assert "找不到分段规划" in out, out[-600:]
    assert "成片不可用" not in out, "段表能恢复，不该按「补不出」判死"
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


def test_reentry_with_a_full_ledger_and_timestampless_prompts_completes(
        tmp_path, monkeypatch, capsys):
    """账满 1..N + 无 ``plan.json`` + 段提示词缺时间戳 → 重进向导也必须收尾。

    这是「缺时间戳 → 标完成」那条裁定在**重进入口**的同一形态：收尾处已经说破并收尾，
    但中断若恰好落在收尾之前（或旧版本留下的会话），重进会落到 ``RESUME_NO_TABLE``——
    那里一律打「恢复段表后重进」，而这条形态**没有**段表可恢复，于是每进一次向导都回到
    同一句、永远收不了尾（2026-10-27 复审第二轮的残留窗口）。
    """
    gen, _session = _start_session(tmp_path)
    for number in (1, 2):
        shot_ledger.append_shot_output(
            gen, segment=number, start_s=(number - 1) * 4, end_s=number * 4,
            video_path=_fake_video(tmp_path, f"seg{number}.mp4"))
    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(segment_prompts, "split_shots_from_prompt",
                        lambda *a, **k: _no_timestamp_segments())
    _answers(monkeypatch, [""])  # 续接菜单：回车＝1

    wizard.run_wizard(_cfg(tmp_path))

    out = capsys.readouterr().out
    assert "成片不可用" in out and "时间戳" in out, out[-800:]
    assert "恢复段表后重进" not in out, "这条形态没有段表可恢复，别再让用户去找它"
    assert load_session(gen).status == session_store.STATUS_COMPLETED


def test_reentry_with_a_missing_table_but_timestamped_prompts_still_stops(
        tmp_path, monkeypatch, capsys):
    """同一条重进入口：段提示词有时间戳（段表只是丢了）→ 仍按原行为停住等恢复。

    两支必须是同一个判据（``_mechanical_table_reason``）：把这一支也判死，用户就再也
    拼不出那条**本来拼得出来**的片子。
    """
    gen, _session = _start_session(tmp_path)
    for number in (1, 2):
        shot_ledger.append_shot_output(
            gen, segment=number, start_s=(number - 1) * 4, end_s=number * 4,
            video_path=_fake_video(tmp_path, f"seg{number}.mp4"))
    _stub_flow(monkeypatch, tmp_path)
    _answers(monkeypatch, [""])

    wizard.run_wizard(_cfg(tmp_path))

    out = capsys.readouterr().out
    assert "恢复段表后重进" in out, out[-800:]
    assert "成片不可用" not in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


# ---------------------------------------------------------------------------
# 3. 收尾拒绝面：不产出成片，也不把会话标成完成（留着能续拼）
# ---------------------------------------------------------------------------

def _finish_only(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_run_segmented_flow", lambda *a, **k: True)


def test_finish_refuses_and_stays_running_when_a_segment_is_missing(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, session = _start_session(tmp_path)
    _write_plans(gen, 2)
    shot_ledger.append_shot_output(
        gen, segment=1, start_s=0, end_s=4,
        video_path=_video(tmp_video, "only1.mp4"))
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists(), "缺段的成片是伪产物，比没有更坏"
    assert "第 2 段" in out, "必须点名缺哪一段"
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING, \
        "拼接没成不得标完成——标了下次重跑会另开新 GEN，已通过的段全白做"


def test_finish_refuses_when_a_recorded_video_changed(tmp_path, monkeypatch,
                                                      tmp_video, capsys):
    """同名换版：账上记的 SHA256 就是「通过验收的那一份」的身份。"""
    gen, session = _start_session(tmp_path)
    _write_plans(gen, 2)
    first = _video(tmp_video, "seg1.mp4")
    second = _video(tmp_video, "seg2.mp4")
    shot_ledger.append_shot_output(gen, segment=1, start_s=0, end_s=4, video_path=first)
    shot_ledger.append_shot_output(gen, segment=2, start_s=4, end_s=8, video_path=second)
    second.write_bytes(b"a different cut of segment 2")
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists()
    assert "第 2 段" in out and "SHA256" in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


def test_finish_refuses_a_ledger_that_skips_a_segment(tmp_path, monkeypatch,
                                                      tmp_video, capsys):
    """账上跳号＝这不是同一套分割的进度，不能拿来拼成片。"""
    gen, session = _start_session(tmp_path, duration=12.0)
    _write_plans(gen, 3)
    for number in (1, 3):
        shot_ledger.append_shot_output(
            gen, segment=number, start_s=(number - 1) * 4, end_s=number * 4,
            video_path=_video(tmp_video, f"seg{number}.mp4"))
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists()
    assert "跳号" in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


def test_finish_refuses_when_the_table_stops_short_of_the_total(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, session = _start_session(tmp_path, duration=12.0)
    _write_plans(gen, 2)  # 只覆盖到 8s，欠起步总时长 12s
    first = _video(tmp_video, "seg1.mp4")
    second = _video(tmp_video, "seg2.mp4")
    shot_ledger.append_shot_output(gen, segment=1, start_s=0, end_s=4, video_path=first)
    shot_ledger.append_shot_output(gen, segment=2, start_s=4, end_s=8, video_path=second)
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert not (gen / "final.mp4").exists()
    assert "12s" in out and "段表" in out
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


# ---------------------------------------------------------------------------
# 4. 旧会话：拼接不可用，但照旧标完成（那不是失败）
# ---------------------------------------------------------------------------

def test_legacy_session_says_the_film_is_unavailable_and_still_completes(
        tmp_path, monkeypatch, capsys):
    gen, session = _start_session(tmp_path)
    (gen / "segments").mkdir(exist_ok=True)
    (gen / "segments" / "progress.json").write_text(
        json.dumps({"done": 2, "total": 2}), encoding="utf-8")
    # 旧会话即便碰巧有一份 plan.json，也不许拼：账上没有段视频路径。
    _write_plans(gen, 2)
    _finish_only(monkeypatch)

    result = wizard._run_long_form(session.brief, session, "prompt", None,
                                   session.stage_state, gen)

    out = capsys.readouterr().out
    assert result == {}
    assert "旧会话没记段视频，拼接不可用" in out
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_COMPLETED, \
        "旧会话拼不出成片是已知边界，不是失败——停在 running 会让它永远续不出来"


# ---------------------------------------------------------------------------
# 5. ≤10 秒不分段：没有可拼的东西
# ---------------------------------------------------------------------------

def test_short_video_never_produces_a_final_film(tmp_path, monkeypatch):
    """短视频（≤10s）不走长片收尾，**不**产出 final.mp4。

    ``_phase2_collect_and_finish`` 是唯一的路由点（``brief.duration > 10``），
    成片、段表、账在这里都不该出现——短视频本来就是一次生成的结果。
    """
    brief = _brief(6.0)
    gen = tmp_path / "GEN001"
    save_session(gen, brief, {"shot_table": "[Shot 1] 深夜空店内……"},
                 status=session_store.STATUS_AWAITING_FRAMES)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(wizard, "run_stage2", lambda *a, **k: ({}, "整条提示词"))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *a, **k: None)

    assert wizard._phase2_collect_and_finish(
        SimpleNamespace(vision_model=SimpleNamespace(model="stub-vision")),
        session_store.load_session(gen)) == 0

    assert not (gen / "final.mp4").exists()
    assert not (gen / "segments" / "shot-ledger.jsonl").exists()
    assert load_session(gen).status == session_store.STATUS_COMPLETED
