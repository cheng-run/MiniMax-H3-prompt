"""拼接失败可续（issue #97，父 spec #92）：重进向导**只**续拼。

钉住的是**外部可见**的事实：重进之后屏幕上没有视频提问、剥帧/闸门/写段一次都没走、
``final.mp4`` 出来、会话转 ``completed``；以及短路发生在**任何 LLM 调用之前**——
那是「段已全过、却因为一个与成片无关的失败（模型构造失败）永远拼不出成片」的形态，
也是本仓「规则在、接线不在」最容易栽的那一格。

为什么这一次不能用「跑一遍完整流程恰好也拼出来」当反证：``_run_segmented_flow`` 的
``cached and done`` 分支会**先构造模型**（哪怕一段都不用写），FL2VA/L2VA 下还会无条件
走末段核验把末段视频再要一遍。段已全过时这些都该整段跳过，所以下面把每一样都打桩成
「被调用就炸」的炸弹，而不是只断言「最后出了成片」。

另有一条**停**的分支：账满而段表缺席时也不进那条路径（它会先重跑一次分段规划才得出
同一个结论），就地停住并沿用 #96 的话术（#96 交接的现场）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import (
    model_factory,
    segment_planner,
    segment_prompts,
    session_store,
    shot_ledger,
)
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.main import build_parser
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.session_store import load_session, save_session
from minimax_h3_prompt.tools import frame_auditor
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.ui import wizard

# 承诺了尾帧的 state：FL2VA 下旧路径一定会去剥末帧核验——恰恰是 #97 要跳过的那一步。
PROMISED_TAIL_STATE = {
    "shot_table": "[Shot 1] 深夜空店内……",
    "fl2va_frame_descriptions": [
        {"picture": 2, "role": "last", "path": "last.png", "description": "承诺的尾帧画面"}],
}


def _brief(duration: float = 8.0) -> Brief:
    return Brief(mode="base", variant="FL2VA", duration=duration, style="写实",
                 plot="深夜便利店一只橘猫推门跳上柜台蹭店员的手")


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(sessions_root=str(tmp_path / "sessions"),
                           vision_model=SimpleNamespace(model="stub-vision"))


def _plans(total: int = 2) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=i, start_s=i * 4, end_s=(i + 1) * 4, shots_in_segment=(i + 1,),
                    summary=f"第 {i + 1} 段中文摘要", end_hook=f"第 {i + 1} 段末态")
        for i in range(total)
    ]


def _segmented_session(tmp_path, *, total: int = 2) -> tuple[Path, session_store.SessionState]:
    """造一个「分段陪跑没跑完」的会话，让 ``run_wizard`` 落到续接入口。

    状态必须是 ``segmented_running``：拼接失败之后会话就停在这里（#96 的失败语义），
    于是它同时还是「出现在可续接列表里」那一条 AC 的现场。
    """
    gen = tmp_path / "sessions" / "某主题" / "GEN001"
    gen.mkdir(parents=True)
    save_session(gen, _brief(), dict(PROMISED_TAIL_STATE),
                 status=session_store.STATUS_SEGMENTED_RUNNING)
    (gen / "video-prompt.md").write_text(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n", encoding="utf-8")
    (gen / "segments").mkdir(parents=True, exist_ok=True)
    (gen / "segments" / "plan.json").write_text(
        json.dumps([plan.to_dict() for plan in _plans(total)], ensure_ascii=False, indent=2),
        encoding="utf-8")
    return gen, load_session(gen)


def _passed_segments(gen: Path, tmp_video, total: int = 2) -> list[Path]:
    """账上把 1..total 段都记成通过，并交回每段 4s / 24fps 的真视频。"""
    videos: list[Path] = []
    for number in range(1, total + 1):
        video = tmp_video(f"seg{number}.mp4", frames=96, fps=24, width=128, height=72)
        shot_ledger.append_shot_output(
            gen, segment=number, start_s=(number - 1) * 4, end_s=number * 4,
            video_path=video)
        videos.append(video)
    return videos


def _interaction(monkeypatch, sequence: list[str]) -> list[str]:
    """打桩交互；返回被问过的问题（按顺序），供「不许再问视频路径」的断言用。"""
    iterator = iter(sequence)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(iterator, "")

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return asked


def _forbid_repeat_steps(monkeypatch) -> dict[str, int]:
    """把「续拼不该再碰到的东西」全部打桩成会记账的炸弹，返回命中次数。

    被拒绝的那几处里有几个（``plan_segments`` / ``build_chat_model``）被上游的
    ``except Exception`` 兜住——所以不能只靠异常炸穿用例，必须同时读命中的次数。
    """
    hits: dict[str, int] = {}

    def boom(name: str):
        def _inner(*_a, **_k):
            hits[name] = hits.get(name, 0) + 1
            raise AssertionError(f"续拼时走到了 {name}：这正是 #97 要消灭的重复步骤")

        return _inner

    for name in ("_capture_bridge_frame", "_acquire_bridge_frame", "_verify_last_frame",
                 "_gate_segment_handoff", "_gate_segment_text"):
        monkeypatch.setattr(wizard, name, boom(name))
    monkeypatch.setattr(model_factory, "build_chat_model", boom("build_chat_model"))
    monkeypatch.setattr(frame_auditor, "extract_last_frame", boom("extract_last_frame"))
    monkeypatch.setattr(segment_prompts, "write_segment_v2", boom("write_segment_v2"))
    monkeypatch.setattr(segment_prompts, "rewrite_segment_prompt",
                        boom("rewrite_segment_prompt"))
    monkeypatch.setattr(segment_planner, "plan_segments", boom("plan_segments"))
    return hits


# ---------------------------------------------------------------------------
# 1. 端到端：拼接失败 → 会话停住 → 重进向导只续拼
# ---------------------------------------------------------------------------

def test_reentering_after_a_failed_concat_only_reattaches_the_film(
        tmp_path, monkeypatch, tmp_video, capsys):
    gen, _session = _segmented_session(tmp_path)
    videos = _passed_segments(gen, tmp_video)
    intact = videos[1].read_bytes()

    # 第一次：末段文件被同名换版 → 拼接被拒（#96 的失败语义，本票不改）。
    videos[1].write_bytes(b"another cut of segment 2")
    hits = _forbid_repeat_steps(monkeypatch)
    asked = _interaction(monkeypatch, [""])

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    first_out = capsys.readouterr().out
    assert not (gen / "final.mp4").exists(), first_out
    assert "SHA256" in first_out, "被拒的原因要点名到段，而不是只留一句「失败」"
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING, \
        "拼接没成不得标完成——标了下次重跑会另开新 GEN，已通过的段全白做"
    # 它还必须在**可续接列表**里，否则用户根本进不到这次续拼。
    assert gen in [item.directory for item in
                   session_store.find_awaiting_sessions(tmp_path / "sessions")]

    # 第二次：把那份视频换回验收时的字节，重进向导——只差成片。
    videos[1].write_bytes(intact)
    asked.clear()

    assert wizard.run_wizard(_cfg(tmp_path)) == 0
    out = capsys.readouterr().out
    final = gen / "final.mp4"
    assert final.is_file(), out
    assert video_meta(final)["frames"] == 192, "两段各 96 帧，stream-copy 直接接起来"
    assert load_session(gen).status == session_store.STATUS_COMPLETED
    assert hits == {}, f"续拼时重跑了不该重跑的步骤：{hits}"
    assert asked, "向导没被真正驱动起来（续接菜单都没问）"
    assert not [text for text in asked if "视频" in text], \
        f"续拼还在索要段视频路径：{asked}"


# ---------------------------------------------------------------------------
# 2. 只过了一部分：短路不许误判（否则剩下的段永远没人做）
# ---------------------------------------------------------------------------

def test_a_partially_passed_ledger_continues_the_remaining_segment(
        tmp_path, monkeypatch, tmp_video, capsys):
    """账上只过了第 1 段 → 必须接着做第 2 段，**不是**直接去拼接。

    短路误判的后果比它要修的病更坏：剩下的段没人做，而拼接每次都被「第 2 段尚未通过」
    拒绝——会话卡在 running 上，用户重进一次就被挡一次。所以判据是「每段都有通过行」，
    不是「账上有点东西」。
    """
    gen, _session = _segmented_session(tmp_path)
    _passed_segments(gen, tmp_video, total=1)   # 只有第 1 段
    prompt = _interaction(monkeypatch, ["", ""])   # 续接菜单 → 第 2 段「已生成好」
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr(wizard, "_capture_bridge_frame",
                        lambda *a, **k: ([], "桥接帧画面", tmp_path / "frame.png"))
    monkeypatch.setattr(wizard, "_gate_segment_handoff", lambda *a, **k: [])
    monkeypatch.setattr(segment_prompts, "write_segment_v2", lambda *a, **k: "第 2 段提示词")

    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    out = capsys.readouterr().out
    assert "第 2/2 段" in out, out
    assert "全部段已通过验收" not in out, "只过了一段就走了续拼短路"
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING
    assert prompt and "视频" in prompt[-1], "第 2 段没做完就该停在要末段视频这一步"


# ---------------------------------------------------------------------------
# 3. 全段已过 + 成片已在：重进要收敛到完成，不是卡在 running
# ---------------------------------------------------------------------------

def test_reentering_with_the_film_already_written_still_converges(
        tmp_path, monkeypatch, tmp_video, capsys):
    """「写完成片、还没标完成就崩了」的重进形态。

    重跑一次拼接是允许的（同名成片被覆盖）：被覆盖掉的是一份**已经由这套段接出来**
    的文件，换来的是「重进就收敛」——不重跑它，这条会话会永远停在 running。
    """
    gen, _session = _segmented_session(tmp_path)
    _passed_segments(gen, tmp_video)
    (gen / "final.mp4").write_bytes(b"stale film from the crashed run")
    hits = _forbid_repeat_steps(monkeypatch)
    _interaction(monkeypatch, [""])

    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    out = capsys.readouterr().out
    assert video_meta(gen / "final.mp4")["frames"] == 192, out
    assert load_session(gen).status == session_store.STATUS_COMPLETED
    assert hits == {}


# ---------------------------------------------------------------------------
# 4. 账读不回来：响错停住，不静默从第 1 段重来
# ---------------------------------------------------------------------------

def _legacy_shot_files(gen: Path, total: int) -> dict[Path, str]:
    """给每段先落一份可辨认的 ``shot-NN.md``：重写会让这份哨兵变样。"""
    written: dict[Path, str] = {}
    for number in range(1, total + 1):
        path = gen / "segments" / f"shot-{number:02d}.md"
        path.write_text(f"哨兵：第 {number} 段的提示词原文", encoding="utf-8")
        written[path] = path.read_text(encoding="utf-8")
    return written


def _damage_the_ledger(gen: Path, damage: str) -> None:
    path = shot_ledger.shot_ledger_path(gen)
    if damage == "corrupt":
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ 这不是合法 JSON\n")
        return
    # "gap"：把第 2 段那一行抹掉，账上留下 {1, 3}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    kept = [row for row in rows if row["segment"] != 2]
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept),
                    encoding="utf-8")


@pytest.mark.parametrize(("damage", "expected"), [
    ("corrupt", "段产出账读不回来"),
    ("gap", "跳号"),
])
def test_a_broken_ledger_stops_the_resume_instead_of_redoing_segment_one(
        tmp_path, monkeypatch, tmp_video, capsys, damage, expected):
    gen, _session = _segmented_session(tmp_path, total=3)
    _passed_segments(gen, tmp_video, total=3)
    sentinels = _legacy_shot_files(gen, 3)
    _damage_the_ledger(gen, damage)
    hits = _forbid_repeat_steps(monkeypatch)
    # 账错形/跳号要走到段产出账的既有读口才判得出来，而它前面那道「续接要一个 llm
    # 句柄」的分支会先要模型——这里放行句柄构造，只禁写段本身（见 hits）。
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    hits.pop("build_chat_model", None)
    _interaction(monkeypatch, [""])

    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    out = capsys.readouterr().out
    assert expected in out, out
    assert "[错误]" in out
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING
    assert hits == {}, f"账读不回来时仍然重写了段/跑了下游：{hits}"
    for path, text in sentinels.items():
        assert path.read_text(encoding="utf-8") == text, f"第 1 段被重写了：{path}"


# ---------------------------------------------------------------------------
# 5. 账满但段表缺席：停住，**不**重跑规划
# ---------------------------------------------------------------------------

def test_a_full_ledger_without_a_segment_table_stops_before_any_llm_call(
        tmp_path, monkeypatch, tmp_video, capsys):
    """账满 1..N、但 ``segments/plan.json`` 缺席 → 不打 LLM、不进闸门，直接停住。

    现场（#96 交接）：机械回退路径的段表只在**全部段通过之后**补写，段提示词没有可用
    时间戳时它压根不写。今天的重进会先重跑一次 ``plan_segments``（LLM 调用），再因为
    「账已满 + 这是另一套分段」报错停下——那次调用纯属浪费，而「拼不了」这件事盘上早就
    看得出来。
    """
    gen, _session = _segmented_session(tmp_path)
    (gen / "segments" / "plan.json").unlink()      # 段表缺席
    _passed_segments(gen, tmp_video)
    sentinels = _legacy_shot_files(gen, 2)
    hits = _forbid_repeat_steps(monkeypatch)
    _interaction(monkeypatch, [""])

    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    out = capsys.readouterr().out
    assert "找不到分段规划 segments/plan.json——段序无从确定" in out, out
    # 第二句两种形态都得为真：这条分支也可能是半途的机械回退账，那时恢复段表后重进是
    # 「接着往下走」。把 #96 收尾处那句「从拼接这一步继续」搬回来就会在那种形态上撒谎。
    assert "接着往下走" in out, out
    assert "从拼接这一步继续" not in out, \
        "把 #96 收尾处那句话搬回来了：半途的机械回退账会被它误导"
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING, \
        "停住要停在可续接状态，别顺手标完成"
    assert hits == {}, f"段表缺席时仍然重跑了流程：{hits}"
    for path, text in sentinels.items():
        assert path.read_text(encoding="utf-8") == text, f"段被重写了：{path}"


def test_a_partial_ledger_without_a_segment_table_also_stops(
        tmp_path, monkeypatch, tmp_video, capsys):
    """已知边界：没有段表就看不出总段数，「账满 1..N」也可能是**半途**的机械回退账。

    两种都停在这里（同一条话术）：后者的出路与今天一样——补段表，或删账从第 1 段重做
    ——但少掉一次白跑的规划调用。钉住它是为了让这条边界是**决定过的**，不是漏出来的。
    """
    gen, _session = _segmented_session(tmp_path)
    (gen / "segments" / "plan.json").unlink()
    _passed_segments(gen, tmp_video, total=1)      # 只过了第 1 段，账是连续的
    hits = _forbid_repeat_steps(monkeypatch)
    _interaction(monkeypatch, [""])

    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    out = capsys.readouterr().out
    assert "找不到分段规划 segments/plan.json——段序无从确定" in out, out
    assert "接着往下走" in out, "半途账补回段表后是接着往下走，不是从拼接继续"
    assert not (gen / "final.mp4").exists()
    assert load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING
    assert hits == {}


# ---------------------------------------------------------------------------
# 6. 命令面：base 没有换段入口
# ---------------------------------------------------------------------------

def test_the_base_route_exposes_no_segment_replace_entry():
    """spec #92：首尾帧链**不提供**换段拼接命令。

    中间第 N 段换掉，会让第 N+1 段的首帧锚不再是它的尾帧——等于亲手制造一个新的接缝
    断裂。多参那条 ``ref-concat --replace`` 的结构不同，不能照搬。这条守卫钉的是
    **命令面**：``--replace`` 只能属于多参拼接那一个子命令。
    """
    parser = build_parser()
    assert all("--replace" not in action.option_strings for action in parser._actions), \
        "顶层多了一个换段入口"

    subcommands: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subcommands.update(action.choices)
    assert "ref-concat" in subcommands, "多参的换段命令被一起删掉了——那是另一条路线的事"

    owners = sorted(
        name for name, sub in subcommands.items()
        if any("--replace" in action.option_strings for action in sub._actions))
    assert owners == ["ref-concat"], f"除多参外还有人提供换段入口：{owners}"
