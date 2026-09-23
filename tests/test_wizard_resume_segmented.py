"""分段陪跑中断后的续接（issue #17）。

**物证（三处合起来才看得出）**：

1. 阶段 2 结束就把会话标成 ``completed``（``_phase2_collect_and_finish``），而分段陪跑
   是在这之后才启动的——长视频照样被标完成。
2. 分段陪跑全程**从不**改会话状态（``_run_segmented_flow*`` 里没有任何 ``save_session``）。
3. 会话目录按状态选：``status != completed`` 才复用，否则 ``GEN{len+1}``。

合起来：分段陪跑中途退出 → 重跑向导 → 最新 GEN 是 ``completed`` → **另开新 GEN** →
阶段 1/2 重做（实测可超 10 分钟）＋ 已完成的所有段重来，而 ``segments/progress.json``
在向导路径上**永远读不到**。2026-09-23 的 GEN005 实际就是这么绕过来的：段 1 被错误闸门
常量硬阻断后，靠仓库里一个未跟踪的 ``resume_gen005.py`` 直进 ``_run_segmented_flow_v2``。

还有一处不改就不成立的配套：v2 每次进入都会重跑 ``plan_segments``（LLM，非确定），
而 ``progress.json`` 里的 ``done=N`` 只对**产出它的那套分段**有意义。续接要真的可用，
就必须复用已落盘的 ``segments/plan.json``。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import model_factory, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.ui import wizard

FIXTURE = Path(__file__).parent / "fixtures" / "official_shot01_i2va.md"


def _brief(duration: float = 18.0) -> Brief:
    return Brief(mode="base", variant="I2VA", duration=duration, style="写实",
                 plot="深夜便利店一只橘猫推门跳上柜台蹭店员的手")


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(sessions_root=str(tmp_path))


def _plans(total: int = 4) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=i, start_s=i * 4, end_s=(i + 1) * 4, shots_in_segment=(i + 1,),
                    summary=f"第 {i + 1} 段中文摘要", end_hook=f"第 {i + 1} 段末态")
        for i in range(total)
    ]


def _interrupted_gen(slug_dir: Path, *, done: int = 2, total: int = 4) -> Path:
    """造一个「分段陪跑跑到一半被中断」的 GEN001（磁盘状态与真产物同形）。"""
    gen = slug_dir / "GEN001"
    (gen / "segments").mkdir(parents=True)
    session_store.save_session(gen, _brief(), {"shot_table": "[Shot 1] 深夜空店内……"},
                               status=session_store.STATUS_SEGMENTED_RUNNING)
    (gen / "video-prompt.md").write_text(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n", encoding="utf-8")
    (gen / "segments" / "plan.json").write_text(
        json.dumps([p.to_dict() for p in _plans(total)], ensure_ascii=False, indent=2),
        encoding="utf-8")
    for i in range(done):
        (gen / "segments" / f"shot-{i + 1:02d}.md").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (gen / "segments" / "progress.json").write_text(
        json.dumps({"done": done, "total": total}), encoding="utf-8")
    return gen


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    # 分段流程要一个 llm 句柄（规划被缓存短路，但写段仍要）——测试里不碰真模型
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())


# --- 状态与目录 ---------------------------------------------------------

def test_segmented_running_counts_as_resumable(tmp_path):
    """分段进行中的会话必须出现在可续接列表里——否则向导根本不会把它端出来。"""
    gen = _interrupted_gen(tmp_path / "某主题")
    assert gen in [s.directory for s in session_store.find_awaiting_sessions(tmp_path)]


def test_session_dir_reuses_gen_while_segmented_running(tmp_path):
    """分段进行中 → 续接同一个 GEN，不另开新号。"""
    cfg = _cfg(tmp_path)
    first = wizard._session_dir(cfg, "某主题")
    session_store.save_session(first, _brief(), {}, status=session_store.STATUS_SEGMENTED_RUNNING)
    assert wizard._session_dir(cfg, "某主题") == first


def test_segmented_running_then_completed_opens_next_gen(tmp_path):
    """跑完后标回 completed，同主题再跑才开新 GEN（续接语义不能被永久劫持）。"""
    cfg = _cfg(tmp_path)
    gen = wizard._session_dir(cfg, "某主题")
    session_store.save_session(gen, _brief(), {}, status=session_store.STATUS_COMPLETED)
    assert wizard._session_dir(cfg, "某主题").name == "GEN002"


# --- 长视频的进行中状态 -------------------------------------------------

def test_long_form_marks_running_while_flow_runs(tmp_path, monkeypatch):
    """分段陪跑**进行中**会话必须是未完成——这是中断后能落回同一 GEN 的全部依据。"""
    gen = _interrupted_gen(tmp_path / "某主题")
    session = session_store.load_session(gen)
    seen: list[str] = []

    def spy(*a, **k) -> bool:
        seen.append(session_store.load_session(gen).status)
        return True

    monkeypatch.setattr(wizard, "_run_segmented_flow", spy)
    wizard._run_long_form(session.brief, session, "prompt", None, session.stage_state, gen)

    assert seen == [session_store.STATUS_SEGMENTED_RUNNING], "跑的过程中会话被标成了完成"
    assert session_store.load_session(gen).status == session_store.STATUS_COMPLETED


def test_long_form_keeps_running_status_when_flow_stops_early(tmp_path, monkeypatch):
    """被硬阻断（#13）或 Ctrl-C 的半途而废不得被标成完成——那正是 #17 的原始形态。"""
    gen = _interrupted_gen(tmp_path / "某主题")
    session = session_store.load_session(gen)
    monkeypatch.setattr(wizard, "_run_segmented_flow", lambda *a, **k: False)

    wizard._run_long_form(session.brief, session, "prompt", None, session.stage_state, gen)

    assert session_store.load_session(gen).status == session_store.STATUS_SEGMENTED_RUNNING


def test_phase2_long_form_never_writes_completed_before_segmenting(tmp_path, monkeypatch):
    """阶段 2 自己那一处写入也必须选对状态。

    「常量在、helper 在、但真正的写入点还写着 completed」是本项目反复栽过的形态
    （规则在、接线不在）。这一段写入覆盖的是**更窄但真实**的窗口：阶段 2 与分段陪跑
    之间还夹着中文摘要生成（要调 LLM，几秒到几十秒），在那期间 Ctrl-C，会话若已是
    completed，下次重跑就另开新 GEN 了。
    """
    gen = tmp_path / "GEN001"
    gen.mkdir()
    brief = _brief(18.0)
    session = session_store.SessionState(
        directory=gen, brief=brief, stage_state={},
        status=session_store.STATUS_AWAITING_FRAMES,
    )
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: ("", ""))
    monkeypatch.setattr(wizard, "run_stage2", lambda *a, **k: ({}, "整条提示词"))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *a, **k: None)
    seen: list[str] = []
    monkeypatch.setattr(wizard, "_run_long_form",
                        lambda *a, **k: seen.append(session_store.load_session(gen).status))

    assert wizard._phase2_collect_and_finish(_cfg(tmp_path), session) == 0

    assert seen == [session_store.STATUS_SEGMENTED_RUNNING], \
        f"进分段陪跑时会话状态是 {seen}，长视频不该在此之前被标成完成"


# --- 向导入口端到端 -----------------------------------------------------

def test_wizard_entry_resumes_same_gen_without_rerunning_stage2(tmp_path, monkeypatch, capsys):
    """**走向导入口**：中断后重跑向导必须落回同一 GEN，且不重做阶段 1/2。"""
    slug_dir = tmp_path / "深夜便利店一只橘猫推门跳上柜台蹭店员的手-a09fb116"
    gen = _interrupted_gen(slug_dir)
    _silence(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("阶段 2 被重跑了——这正是 #17 要消灭的代价")

    monkeypatch.setattr(wizard, "run_stage2", boom)
    monkeypatch.setattr(wizard, "run_stage1", boom)
    routed: list[Path] = []
    monkeypatch.setattr(wizard, "_run_segmented_flow",
                        lambda brief, session, *a, **k: routed.append(session.directory) or True)

    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    assert routed == [gen], "向导没有把这次运行路由到中断的那个 GEN"
    assert sorted(p.name for p in slug_dir.glob("GEN*")) == ["GEN001"], "另开了新 GEN"
    assert "续接" in capsys.readouterr().out


def test_resume_uses_the_prompt_already_on_disk(tmp_path, monkeypatch, capsys):
    """续接要用已落盘的整条提示词，不能要求重跑阶段 2 才有 prompt 可拆。"""
    slug_dir = tmp_path / "某主题"
    gen = _interrupted_gen(slug_dir)
    _silence(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(wizard, "_run_segmented_flow",
                        lambda brief, session, prompt, *a, **k: seen.append(prompt) or True)

    wizard.run_wizard(_cfg(tmp_path))

    assert seen and "<Picture 1> (from [Shot 1]) is fully referenced." in seen[0]


# --- 分段流程自身的续接 -------------------------------------------------

def test_v2_flow_continues_from_progress_and_skips_written_segments(tmp_path, monkeypatch, capsys):
    """续接必须从第 done+1 段继续，且不重写已落盘的段。"""
    slug_dir = tmp_path / "某主题"
    gen = _interrupted_gen(slug_dir, done=2, total=4)
    session = session_store.load_session(gen)
    _silence(monkeypatch)
    written: list[int] = []

    def fake_write(plan, *a, **k):
        written.append(plan.index)
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(wizard, "_acquire_bridge_frame", lambda *a, **k: True)
    monkeypatch.setattr("minimax_h3_prompt.segment_prompts.write_segment_v2", fake_write)

    wizard._run_segmented_flow(session.brief, session, "prompt", None, session.stage_state)

    out = capsys.readouterr().out
    assert "检测到已完成 2/4 段" in out
    assert written == [2, 3], f"没有从第 3 段继续：{written}"


def test_corrupt_plan_with_progress_refuses_to_continue(tmp_path, monkeypatch, capsys):
    """plan.json 读不回来 + progress 说做过段 → **停下并报错**，不许静默重规划。

    静默重规划的后果：新分段边界配旧 ``done`` → 跳过从没写过的段，而且全程不报错
    （正是「续接必须复用 plan.json」那条注释要防的事）。也不能覆盖那份可能还能救回来的
    plan.json。
    """
    from minimax_h3_prompt import segment_planner

    gen = _interrupted_gen(tmp_path / "某主题", done=2, total=4)
    broken = "{ 这不是合法 JSON"
    (gen / "segments" / "plan.json").write_text(broken, encoding="utf-8")
    session = session_store.load_session(gen)
    _silence(monkeypatch)
    replanned: list[int] = []
    monkeypatch.setattr(segment_planner, "plan_segments",
                        lambda *a, **k: replanned.append(1) or _plans())

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is False, "落盘物对不上却继续跑了"
    assert not replanned, "静默重规划了——旧 done 会去索引新分段"
    assert "[错误]" in out and "对不上" in out
    assert (gen / "segments" / "plan.json").read_text(encoding="utf-8") == broken, \
        "把可能还能救回来的 plan.json 覆盖掉了"


def test_progress_without_cached_plan_refuses_to_continue(tmp_path, monkeypatch, capsys):
    """plan.json **整个丢了** + progress > 0 → 重规划成功也进不去。

    这条不变量钉在 v2 里（``plans_from_cache``）：只要 ``done > 0``，就只允许用从盘上
    读回来的那一份分段继续。
    """
    from minimax_h3_prompt import segment_planner

    gen = _interrupted_gen(tmp_path / "某主题", done=2, total=4)
    (gen / "segments" / "plan.json").unlink()
    session = session_store.load_session(gen)
    _silence(monkeypatch)
    monkeypatch.setattr(segment_planner, "plan_segments", lambda *a, **k: _plans())

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is False
    assert "[错误]" in out and "跳过没写过的段" in out


def test_resume_stops_loudly_when_the_model_cannot_be_built(tmp_path, monkeypatch, capsys):
    """续接时模型构造失败要和兄弟分支同一套口径：显式报错停下，不是崩出去。"""
    gen = _interrupted_gen(tmp_path / "某主题", done=2, total=4)
    session = session_store.load_session(gen)
    _silence(monkeypatch)

    def boom():
        raise RuntimeError("缺 API key")

    monkeypatch.setattr(model_factory, "build_chat_model", boom)

    finished = wizard._run_segmented_flow(session.brief, session, "prompt", None,
                                          session.stage_state)

    out = capsys.readouterr().out
    assert finished is False
    assert "续接时构造模型失败" in out and "[错误]" in out
    assert "回到同一条续接路径" in out, "必须告诉用户重跑是安全的、不必重做任何段"


def test_corrupt_progress_is_reported_not_silently_zero(tmp_path, capsys):
    """progress.json 读不出来要显式告警——静默返回 0 与「一段都没做过」无法区分。"""
    gen = tmp_path / "GEN001"
    (gen / "segments").mkdir(parents=True)
    (gen / "segments" / "progress.json").write_text("{{ 坏的", encoding="utf-8")

    assert wizard._segments_done(gen) == 0
    assert "[警告]" in capsys.readouterr().out


def test_v2_flow_reuses_cached_plan_so_done_stays_meaningful(tmp_path, monkeypatch, capsys):
    """续接时不得重跑规划：plan_segments 是 LLM 调用（非确定），换了分段 done=N 就失去意义。"""
    from minimax_h3_prompt import segment_planner

    slug_dir = tmp_path / "某主题"
    gen = _interrupted_gen(slug_dir, done=2, total=4)
    session = session_store.load_session(gen)
    _silence(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("续接时重跑了分段规划——done=N 会指向另一套分段")

    monkeypatch.setattr(segment_planner, "plan_segments", boom)
    monkeypatch.setattr(wizard, "_acquire_bridge_frame", lambda *a, **k: True)
    monkeypatch.setattr("minimax_h3_prompt.segment_prompts.write_segment_v2",
                        lambda *a, **k: FIXTURE.read_text(encoding="utf-8"))

    wizard._run_segmented_flow(session.brief, session, "prompt", None, session.stage_state)

    assert "检测到已完成 2/4 段" in capsys.readouterr().out
