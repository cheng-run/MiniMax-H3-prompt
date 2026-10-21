"""向导·多参逐段陪跑的续跑（issue #58）：已通过的段不重出、不重跑、不重付。

覆盖票面 AC：
- AC1 每段通过即落盘进度（段号 + 判定 + 视频路径）——进度就是 #55 的段产出账通过行；
- AC2 中断后重跑，从**最后一个续跑点**继续；已通过的段不重出提示词、不重跑、不重付；
- AC3 进度落盘物缺失/错形时**响错**，不静默从零开始（裁定见下）；
- AC4 与**阶段 2 断点**、**续跑点**的边界（见 CONTEXT.md 词条与代码注释）；
- AC5 主缝测试驱动**真实调用链**（``run_wizard`` 的多参阶段 2），只打桩 LLM 与交互输入；
- AC6 **变异检验**：把「续跑跳过已通过段」摘掉，本文件必须变红（实测记录见文件尾）。

**AC3 的口径（2026-10-21 用户裁定：分档）**：
- 段产出账**文件不在** ＝ 一段都没通过（追加账在零通过时本来就不存在，#55 的
  ``test_empty_handback_stops_the_run`` 钉着这一点）——当面打印「账上没有已通过的段」，
  从第 1 段起，**这不是错**，也不是静默（打印本身就是响）；
- **段表 plan.json 缺失/读不出、段文件缺失、账行错形、账行与段表对不上**（段号越界或
  跳号）＝ 响错停住：这些形态下「从零开始」意味着把已通过的段当没做过。

**不重付的判据**：续跑全程零 LLM 调用——``run_stage1``/``run_stage2`` 一旦被调就
直接炸（见 ``_resume_run`` 的布防），段文本从 ``segments/segment-NN.md`` 读回，
已通过段连语义层的读图（视觉计费）都不再跑。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, session_store
from minimax_h3_prompt.ref_segments import (
    SEGMENT_EVENT_OUTPUT,
    read_segment_ledger,
)
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# 20s 两镜（切点 0/10）→ 机械打包恰得两段各 10s，段正文互不相同：
# 段 1 只有 Shot 1（花园小径），段 2 只有 Shot 2（钥匙特写）。两句话是「当前展示的是
# 哪一段」的判据——续跑「不重出已通过段的提示词」正是靠它断言的。
SEG1_BODY = "A medium shot frames the girl walking through the garden gate."
SEG2_BODY = "the shot cuts to a close-up of the bronze key in her hand"

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


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    # 读图是网络调用（真花钱），一律打桩——同「会真建 LLM 客户端的用例一律打桩」。
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda *a, **k: "桩：本段首/末帧的实际画面")


def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        try:
            return next(prompts)
        except StopIteration:
            raise AssertionError(f"输入表用完了；已问：{asked}") from None

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _confirm_continue_only(text: str, default: bool = True) -> bool:
    """确认桩：语义层「达到」答是；其余只有含「继续」的答是（素材追加、续接连问等）。"""
    if "达到" in text:
        return True
    return "继续" in text


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


def _new_run_tail(duration: str) -> list[str]:
    """首跑的输入表：题面 → 总时长 → 画布 → 素材循环四张全放弃。"""
    return [TOPIC, duration, "", "4", "", "4", "", "4", "", "4", "4"]


def _run(tmp_path, monkeypatch, handbacks: list[str],
         *, duration: str = "20") -> tuple[list[str], int]:
    """首跑全程：阶段 1 → 素材循环 → 阶段 2 → 逐段陪跑（真调用链，只打桩 LLM 与输入）。

    ``duration``：20 ＝ >15s 拆两段；12 ＝ ≤15s 直出单段（走同一套收尾，issue #57 AC5）。
    """
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
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(wizard, "_confirm", _confirm_continue_only)
    asked = _fake_inputs(monkeypatch, [*_new_run_tail(duration), *handbacks])
    return asked, wizard.run_wizard(_cfg(tmp_path))


def _resume_run(tmp_path, monkeypatch, handbacks: list[str]) -> tuple[list[str], int]:
    """重跑向导（续接路径）：只答「选序号（回车=1）」，然后接逐段陪跑的交回口。

    阶段 1/2 一旦被调就直接炸——续跑的全部意义就是**不重做最贵的那几步**。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})

    def boom(*a, **k):
        raise AssertionError("续跑重跑了阶段 1/2——这正是 #58 要消灭的代价")

    monkeypatch.setattr(wizard, "run_stage1", boom)
    monkeypatch.setattr(wizard, "run_stage2", boom)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(wizard, "_confirm", _confirm_continue_only)
    asked = _fake_inputs(monkeypatch, ["", *handbacks])
    return asked, wizard.run_wizard(_cfg(tmp_path))


def _generation_dir(tmp_path) -> Path:
    """本次生成的 GEN 目录（段产出账收的是它，不是 segments/ 那层）。"""
    return next((tmp_path / "sessions").rglob("session-state.json")).parent


def _output_rows(tmp_path) -> list[dict]:
    return read_segment_ledger(_generation_dir(tmp_path), event=SEGMENT_EVENT_OUTPUT)


# ---------------------------------------------------------------------------
# AC2 / AC4：中断后会话必须停在「多参分段进行中」，否则续接根本走不到
# ---------------------------------------------------------------------------

def test_interrupted_handoff_keeps_the_session_resumable(tmp_path, monkeypatch,
                                                         h3_tier_video):
    """段 1 通过后停住：会话状态必须是 ``ref_segments_running``，且出现在可续接列表里。

    这与首尾帧链 #17 是同一个形态：陪跑在 ``save_session(completed)`` **之后**跑，于是
    「停在段 3」的会话在盘上是完成的——重跑向导会另开新 GEN，阶段 1/2 重做，段产出账
    永远读不到（#55 在 ``_phase2_ref_finish`` 里留了注释点名本票）。
    """
    first = h3_tier_video(10.0, "seg1.avi")
    _run(tmp_path, monkeypatch, [str(first), ""])

    gen = _generation_dir(tmp_path)
    session = session_store.load_session(gen)
    assert session is not None
    assert session.status == session_store.STATUS_REF_SEGMENTS_RUNNING
    resumable = [s.directory for s in
                 session_store.find_awaiting_sessions(tmp_path / "sessions")]
    assert gen in resumable, "中断的多参陪跑会话没进可续接列表——向导不会把它端出来"


# ---------------------------------------------------------------------------
# AC2 / AC5：续跑从最后一个续跑点继续，已通过的段不重出、不重跑、不重付
# ---------------------------------------------------------------------------

def test_resume_skips_the_passed_segment_and_continues_from_the_next(
        tmp_path, monkeypatch, capsys, h3_tier_video):
    """段 1 通过后停住 → 重跑向导：段 1 不再出词、不再问交回；段 2 接着走完并拼出成片。

    「不重付」由两道布防钉住：``_resume_run`` 把阶段 1/2 换成一旦被调就炸的桩，而
    已通过段的语义层（读图＝视觉计费）也不再进入——段 1 的帧图路径不会在本轮重算。
    """
    first = h3_tier_video(10.0, "seg1.avi")
    second = h3_tier_video(10.0, "seg2.avi")
    _run(tmp_path, monkeypatch, [str(first), ""])
    capsys.readouterr()  # 首跑的输出不参与本轮断言

    # 第二个交回值是**变异检验的余量**：摘掉跳段逻辑时段 1 会重新要交回口，本用例必须
    # 走到自己的断言上红，而不是以「输入表耗尽」这种与 AC 无关的方式红。
    asked, code = _resume_run(tmp_path, monkeypatch, [str(second), str(second)])
    out = capsys.readouterr().out

    assert code == 0
    # 不重问已通过段的交回口（这是变异检验的靶子：摘掉跳过逻辑就会出现这条问句）
    assert not any("第 1/2 段输出视频路径" in text for text in asked), \
        f"续跑重问了段 1 的交回口：{asked}"
    assert any("第 2/2 段输出视频路径" in text for text in asked), \
        f"没有从段 2 接着走：{asked}"

    # 不重出已通过段的提示词与语义核验块
    assert SEG1_BODY not in out, "续跑把段 1 的提示词又打了一遍（等于让用户再跑一次）"
    assert "第 1/2 段已通过" in out, f"没有当面说明跳过了哪一段：{out[-600:]}"
    assert SEG2_BODY in out, "段 2 的提示词没出——续跑停在半路"

    # 账上段 1 仍只有原来那一行，段 2 在续跑里补上
    rows = _output_rows(tmp_path)
    assert [r["segment"] for r in rows] == [1, 2]
    assert rows[0]["video"] == str(first.resolve()), "段 1 的通过行被续跑改写了"

    # 收尾照走：成片接出来、会话转 completed（否则下次重跑还会端出这个会话）
    assert (next((tmp_path / "sessions").rglob("final.mp4"))).is_file()
    gen = _generation_dir(tmp_path)
    assert session_store.load_session(gen).status == session_store.STATUS_COMPLETED
    assert gen not in [s.directory for s in
                       session_store.find_awaiting_sessions(tmp_path / "sessions")], \
        "跑完的会话还挂在可续接列表里——续接语义被永久劫持（同 #17 的收尾）"


def test_resume_works_for_the_single_segment_route_too(tmp_path, monkeypatch, capsys,
                                                       h3_tier_video):
    """≤15 秒直出（段数=1）走**同一套**续跑，不另开一条路（issue #57 AC5 的延续）。

    单段片子同样会跨越人工生成窗口：停在段 1 的会话必须能续，续上就是「全部通过 →
    拼接退化为拷贝改名」。这条同时钉住续跑装载的 ``annotated`` 判据跟的是**交付路径**
    （总时长 > 15s）而不是「段数 > 1」——段数=1 的直出片不带注记。
    """
    only = h3_tier_video(12.0, "only.avi")
    _run(tmp_path, monkeypatch, [str(only)], duration="12")
    capsys.readouterr()
    gen = _generation_dir(tmp_path)
    # 单段片没有「通过后停住」的自然形态（通过即收尾），所以按**真实的中断窗口**造盘上
    # 状态：段 1 的通过行已落、成片也接出来了，但进程死在收尾那次 save_session 之前——
    # 会话于是仍是「陪跑进行中」。这就是 Ctrl-C/关机落在这个窗口时磁盘的样子。
    session = session_store.load_session(gen)
    session_store.save_session(gen, session.brief, session.stage_state,
                               status=session_store.STATUS_REF_SEGMENTS_RUNNING)
    (gen / "final.mp4").unlink()

    # 余量同上一例：变异检验要红在本用例自己的断言上。
    asked, code = _resume_run(tmp_path, monkeypatch, [str(only)])
    out = capsys.readouterr().out

    assert code == 0
    assert not any("输出视频路径" in text for text in asked), \
        f"续跑重问了已通过段的交回口：{asked}"
    assert "第 1/1 段已通过" in out, f"没有跳过已通过的那一段：{out[-600:]}"
    film = next((tmp_path / "sessions").rglob("final.mp4"))
    assert film.is_file(), "单段片续跑通过后没接出成片（拼接应退化为拷贝改名）"
    assert session_store.load_session(_generation_dir(tmp_path)).status \
        == session_store.STATUS_COMPLETED


# --- 续跑装载的纯件（判据不依赖终端节奏） -----------------------------------

def test_resume_loader_takes_the_annotation_flag_from_the_delivery_route(tmp_path):
    """``annotated`` 跟**交付路径**（总时长 > 15s），不跟段数：>15s 也可能只剩 1 段。

    这是 ``validate_segment_text`` 选校验口径的依据：直出片不带注记，拿分段校验器去判它，
    两条注记判据是结构性必挂的假错。判据若写成「段数 > 1」，>15s 的单段片续跑后会被
    按错误口径判。
    """
    from minimax_h3_prompt.ref_segments import load_ref_segments_for_resume
    from minimax_h3_prompt.segment_planner import SegmentPlan

    gen = tmp_path / "GEN001"
    (gen / "segments").mkdir(parents=True)
    plan = SegmentPlan(index=0, start_s=0, end_s=16, shots_in_segment=(1,),
                       summary="整片", end_hook="末态", start_state="开场")
    (gen / "segments" / "plan.json").write_text(
        json.dumps([plan.to_dict()], ensure_ascii=False), encoding="utf-8")
    (gen / "segments" / "segment-01.md").write_text("整片正文", encoding="utf-8")

    assert load_ref_segments_for_resume(gen, total_s=16.0).annotated is True, \
        ">15s 的单段片是拆段路径的产物，带注记"
    assert load_ref_segments_for_resume(gen, total_s=12.0).annotated is False, \
        "≤15s 是直出路径的产物，不带注记"


# ---------------------------------------------------------------------------
# AC3：落盘物缺失/错形 → 响错停住，不静默从零开始
# ---------------------------------------------------------------------------

def _interrupted(tmp_path, monkeypatch, h3_tier_video) -> tuple[Path, Path]:
    """跑到「段 1 通过、段 2 未交回」然后停住；返回 (GEN 目录, 段 1 的视频)。"""
    first = h3_tier_video(10.0, "seg1.avi")
    _run(tmp_path, monkeypatch, [str(first), ""])
    return _generation_dir(tmp_path), first


def _assert_refused_loudly(tmp_path, asked, code, out) -> None:
    """公用判据：代码退出码 1、当面打出 [错误]、不重问任何交回口、会话仍可续接。"""
    assert code == 1, "读不回来却当没事发生继续跑"
    assert "[错误]" in out, f"没有响：{out[-600:]}"
    assert not any("输出视频路径" in text for text in asked), \
        f"响错之后仍然开始重问交回口（＝从零开始）：{asked}"
    session = session_store.load_session(_generation_dir(tmp_path))
    assert session.status == session_store.STATUS_REF_SEGMENTS_RUNNING, \
        "响错后会话被标成别的状态——用户修好落盘物也没法再续"
    assert [r["segment"] for r in _output_rows(tmp_path)] == [1], "响错路径改了段产出账"


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_unusable_plan_table_is_refused_not_restarted(tmp_path, monkeypatch, capsys,
                                                      h3_tier_video, damage):
    """段表读不出（坏 JSON / 整个没了）→ 响错停住。

    段表没了，``passed`` 里的「第 1 段已通过」指向哪几段就无从谈起；而重跑阶段 2
    重新拆段会得到**另一套**边界——拿旧进度索引新分段会跳过从没做过的段（base 路线
    ``_cached_plans`` 同一条纪律）。
    """
    gen, _first = _interrupted(tmp_path, monkeypatch, h3_tier_video)
    capsys.readouterr()
    plan = gen / "segments" / "plan.json"
    if damage == "corrupt":
        plan.write_text("{ 这不是合法 JSON", encoding="utf-8")
    else:
        plan.unlink()

    asked, code = _resume_run(tmp_path, monkeypatch, [""])
    out = capsys.readouterr().out

    _assert_refused_loudly(tmp_path, asked, code, out)
    assert "分段规划" in out, f"没说清是段表读不回来：{out[-600:]}"


def test_missing_segment_file_is_refused(tmp_path, monkeypatch, capsys, h3_tier_video):
    """段文件没了 → 响错：续跑不许「重出」已交付的段（那要重付 LLM 且会与已通过的脱节）。"""
    gen, _first = _interrupted(tmp_path, monkeypatch, h3_tier_video)
    capsys.readouterr()
    (gen / "segments" / "segment-02.md").unlink()

    asked, code = _resume_run(tmp_path, monkeypatch, [""])
    out = capsys.readouterr().out

    _assert_refused_loudly(tmp_path, asked, code, out)
    assert "第 2 段的提示词文件不见了" in out, f"没点名缺的是哪一段：{out[-600:]}"


def test_malformed_ledger_line_is_refused(tmp_path, monkeypatch, capsys, h3_tier_video):
    """段产出账有坏行 → 响错（追加账是「这段到底交回过什么」的凭据，坏行不许跳过）。"""
    gen, _first = _interrupted(tmp_path, monkeypatch, h3_tier_video)
    capsys.readouterr()
    ledger = gen / "segments" / "segment-ledger.jsonl"
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write("{{ 这不是合法 JSON\n")

    asked, code = _resume_run(tmp_path, monkeypatch, [""])
    out = capsys.readouterr().out

    assert code == 1 and "[错误]" in out
    assert "段产出账" in out and "不是合法 JSON" in out, f"没说清账坏在哪：{out[-600:]}"
    assert not any("输出视频路径" in text for text in asked)


def test_ledger_disagreeing_with_the_plan_is_refused(tmp_path, monkeypatch, capsys,
                                                     h3_tier_video):
    """账行与段表对不上（跳号 / 段号越界）→ 响错。

    陪跑是严格顺序的：通过行必须恰好是 1..k。中间缺段说明账被删过/改过，按它续跑
    会**跳过从没做过的段**——那正是本票要消灭的形态。
    """
    gen, _first = _interrupted(tmp_path, monkeypatch, h3_tier_video)
    capsys.readouterr()
    ledger = gen / "segments" / "segment-ledger.jsonl"
    # 必须挑 event=output 那一行：同一本账里 encounter（语义层遭遇）行写在它之前，
    # 取第一行会抄到「判为达到」的遭遇行——那正是「不判别 event 就误读」的活例子。
    row = next(r for r in map(json.loads, ledger.read_text(encoding="utf-8").splitlines())
               if r.get("event") == SEGMENT_EVENT_OUTPUT)
    # 把「第 1 段通过」改写成「第 99 段通过」：段表只有 2 段 → 越界；同时只剩一行 → 跳号
    ledger.write_text(json.dumps({**row, "segment": 99}, ensure_ascii=False) + "\n",
                      encoding="utf-8")

    asked, code = _resume_run(tmp_path, monkeypatch, [""])
    out = capsys.readouterr().out

    assert code == 1, f"code={code} asked={asked} out={out[-1500:]}"
    assert "[错误]" in out
    assert "段表之外的段号" in out, f"没点名账与段表对不上：{out[-600:]}"
    assert not any("输出视频路径" in text for text in asked)
