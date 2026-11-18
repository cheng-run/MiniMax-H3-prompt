"""#306：导演台写段请求导出脚本的秒级用例（段过滤 ＋ sha256 稳定）。

为什么用合成会话：真实会话在 ``output/``（gitignore），干净检出上没有；脚本与真实
写段的同源性由「它调的就是 ``build_segment_request``」保证，这里钉的是 **CLI 行为**：
``--segment`` 只出那一段、同一输入两跑 sha256 相同、缺产物时非零退出且不静默空输出。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from minimax_h3_prompt.director.frames import (
    DirectorPlan,
    DirectorSegment,
    declared_frames_for_export,
    give_images_for,
)
from minimax_h3_prompt.director.frame_meta import sha256_file
from minimax_h3_prompt.director.opening_still import OPENING_STILL_KIND
from minimax_h3_prompt.director.planning import plan_path, read_plan_json, write_plan_json
from scripts.director_request_dump import main

# 镜头块解析认 ``**[Shot N]**`` 与行内 ``At 00:SS.mmm``（``firstframe._shot_blocks``）。
SHOT_TABLE = """# 分镜表

**[Shot 1]** SHOT-ONE-PROSE At 00:00.000，远镜：海堤与空着的尽头。

**[Shot 2]** SHOT-TWO-PROSE At 00:01.500，中景：人物取手机。

**[Shot 3]** SHOT-THREE-PROSE At 00:02.400，近景：人物转身。
"""


def _plan(mode: str = "fl2v") -> DirectorPlan:
    """两段 fl2v：开场（无引导）＋续接（有引导），帧数满足读回的全部硬契约。"""
    opening_export = 39          # 17×2+5：无引导段声明帧 == 导出帧
    continue_export = 68         # 17×4：有引导段声明帧 = 导出帧 − 12
    segments = (
        DirectorSegment(
            index=1, type="opening", guided=False,
            start_frame=0, end_frame=opening_export,
            declared_frames=declared_frames_for_export(opening_export, guided=False),
            export_frames=opening_export,
            summary="开场", end_hook="人物站定在堤上",
            give_images=give_images_for("opening", mode),
        ),
        DirectorSegment(
            index=2, type="continue", guided=True,
            start_frame=opening_export, end_frame=opening_export + continue_export,
            declared_frames=declared_frames_for_export(continue_export, guided=True),
            export_frames=continue_export,
            summary="续接", end_hook="人物转身面向海",
            give_images=give_images_for("continue", mode),
        ),
    )
    return DirectorPlan(mode=mode, total_frames=opening_export + continue_export,
                        segments=segments)


def _write_session(root: Path, mode: str = "fl2v") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_plan_json(plan_path(root), _plan(mode))
    (root / "shot-table.md").write_text(SHOT_TABLE, encoding="utf-8")
    (root / "session-state.json").write_text(json.dumps({
        "schema_version": "wizard_session.v1",
        "brief": {"mode": "director", "director_mode": mode, "duration": 4.5},
        "stage_state": {
            "shot_table": SHOT_TABLE,
            # 两个上下文块的来源值：画面决策块由后两键渲染，修订块没有待落地条目。
            "shot_review_lock": "LOCK-SENTINEL",
            "visual_design": "VISUAL-SENTINEL",
        },
        "status": "director_segments_running",
    }, ensure_ascii=False), encoding="utf-8")
    return root


def _index(out: Path) -> dict:
    return json.loads((out / "index.json").read_text(encoding="utf-8"))


def test_segment_filter_exports_only_the_requested_segment(tmp_path):
    """AC4「段过滤」：``--segment 2`` 只落第 2 段；窗口镜头号按整张段表念。"""
    session = _write_session(tmp_path / "GEN001")
    out = tmp_path / "dump-2"

    assert main([str(session), "--segment", "2", "--out", str(out)]) == 0

    assert (out / "segment-02.request.txt").is_file()
    assert not (out / "segment-01.request.txt").exists(), "段过滤没生效"
    rows = _index(out)["segments"]
    assert [row["index"] for row in rows] == [2], rows
    # 段 2 窗口 1.625–4.458s：Shot 2（1.5s 起）与 Shot 3（2.4s 起）在窗内，Shot 1 不在。
    assert rows[0]["window_shots"] == [2, 3], rows[0]["window_shots"]
    request = (out / "segment-02.request.txt").read_text(encoding="utf-8")
    # 分镜表按本段窗口切片（#301 的同一份解析）：窗内的 Shot 2/3 进了请求，窗外的 Shot 1 没进。
    assert "SHOT-TWO-PROSE" in request and "SHOT-THREE-PROSE" in request
    assert "SHOT-ONE-PROSE" not in request, "窗口外的镜头原文串进了请求"
    assert "LOCK-SENTINEL" in request and "VISUAL-SENTINEL" in request, \
        "画面决策块的来源值没有进请求（上下文块接错了）"


def test_same_input_gives_the_same_sha256(tmp_path):
    """AC4「同一输入 sha256 稳定」：同一会话两跑，文件字节与索引 sha 全等。"""
    session = _write_session(tmp_path / "GEN001")
    first = tmp_path / "run-1"
    second = tmp_path / "run-2"

    assert main([str(session), "--out", str(first)]) == 0
    assert main([str(session), "--out", str(second)]) == 0

    for row in _index(first)["segments"]:
        left = (first / row["file"]).read_bytes()
        right = (second / row["file"]).read_bytes()
        assert left == right, f"第 {row['index']} 段请求两次导出不一致"
        assert hashlib.sha256(left).hexdigest() == row["sha256"], \
            "索引里的 sha256 与请求文件字节不是同一份"
    assert _index(first)["segments"] == _index(second)["segments"]


def test_missing_artifacts_fail_loudly_instead_of_empty_output(tmp_path):
    """AC1：缺段表/缺会话状态时非零退出并给出可读错误，不静默导出空结果。"""
    session = _write_session(tmp_path / "GEN001")
    (session / "director" / "plan.json").unlink()

    assert main([str(session), "--out", str(tmp_path / "dump")]) == 1
    assert not (tmp_path / "dump").exists(), "失败时不该留下半份产物"
    assert not (tmp_path / "does-not-exist").exists()
    assert main([str(tmp_path / "does-not-exist")]) == 1


def test_corrupt_state_is_not_silently_ignored(tmp_path, capsys):
    """状态文件存在但读不出时要响错：别拿一份「两个块都空」的假请求去对照。"""
    session = _write_session(tmp_path / "GEN001")
    (session / "session-state.json").write_text("{坏的 json", encoding="utf-8")

    assert main([str(session)]) == 1
    assert "读不出" in capsys.readouterr().err


def test_session_without_state_still_exports_but_warns(tmp_path, capsys):
    """AC1 的最低要求：只有 plan.json ＋ 分镜表也能导出；缺状态时明说两个块为空。"""
    session = _write_session(tmp_path / "GEN001")
    (session / "session-state.json").unlink()
    out = tmp_path / "dump"

    assert main([str(session), "--out", str(out)]) == 0
    assert (out / "segment-01.request.txt").is_file()
    assert "没有 session-state.json" in capsys.readouterr().out
    row = _index(out)["segments"][0]
    assert row["revision_context_chars"] == 0 and row["visual_decision_chars"] == 0


def test_out_dir_inside_the_session_is_refused(tmp_path):
    """只读契约：``--out`` 落进会话目录时拒绝（脚本对会话目录没有写权）。"""
    session = _write_session(tmp_path / "GEN001")
    inside = session / "dump"

    assert main([str(session), "--out", str(inside)]) == 1
    assert not inside.exists()


@pytest.mark.parametrize("segment", [0, 3])
def test_segment_out_of_range_is_named(tmp_path, capsys, segment):
    session = _write_session(tmp_path / "GEN001")

    assert main([str(session), "--segment", str(segment)]) == 1
    assert "不在段表里" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# #325 开场首帧图：导出要复现真实写段的「本段起点画面」块（契约与全片尾帧一致：只读）
# ---------------------------------------------------------------------------

_OPENING_DESCRIPTION = "窗在人物身后侧，桌上有一只白瓷杯，男人背对镜头。"


def _opening_still(root: Path, description: str = _OPENING_DESCRIPTION) -> Path:
    """按打包契约落一张开场首帧图 ＋ 它的读图 sidecar（用途标记＝``opening_still``）。

    sidecar 是**真实读图过的会话**盘上会有的东西（向导收图后立刻读一次）；这里直接
    落好，模拟「这条会话读过了、现在只做离线导出」。
    """
    image = root / "director" / "assets" / "01" / "start.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"opening-still-bytes")
    image.with_name(image.stem + ".meta.json").write_text(json.dumps({
        "kind": OPENING_STILL_KIND,
        "description": description,
        "vision_model": "test-vision-model",
        "image_sha256": sha256_file(image),
        "at": "2026-11-18T00:00:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")
    return image


def _forbid_vision(monkeypatch) -> list:
    """打桩唯一的视觉调用口：导出过程中任何一次读图都当场失败并记账。

    桩打在 ``reference_auditor.describe_image`` 上——那是读图的唯一入口（共享读图
    实现延迟 import 它），所以「导出脚本绕过 sidecar 现场读图」会在这里暴露，而不是
    悄悄真的发一次付费请求。
    """
    from minimax_h3_prompt.tools import reference_auditor

    calls: list = []

    def fail(path, instruction=None):
        calls.append(path)
        raise AssertionError(f"离线导出过程中读了图：{path}")

    monkeypatch.setattr(reference_auditor, "describe_image", fail)
    return calls


_WRITE_BODY = """{note}
integrated_multimodal_description:
[Shot 1] A woman stands by the seawall, wind in her hair.

overall_soundscape:
Waves against concrete.

non_diegetic_music:
N/A
"""


class _RecordingLLM:
    """记录出站请求、回一版合格正文的桩模型（``_direct_write`` 拿到什么就记什么）。"""

    def __init__(self, plan: DirectorPlan) -> None:
        self.plan = plan
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        import re

        from minimax_h3_prompt.director.prompts import render_timeline_note

        text = str(request)
        self.requests.append(text)
        index = int(re.search(r"Segment (\d+)/", text).group(1))
        return _WRITE_BODY.format(
            note=render_timeline_note(self.plan, self.plan.segments[index - 1]))


def test_export_matches_the_request_the_real_write_sends(tmp_path):
    """AC1：有图且有 sidecar 时，导出的开场段请求与真实写段发出的请求逐字节相同。

    「真实写段」照调用方的做法跑一遍：经读图口取盘上描述（缓存命中、不读图），把
    事实交给 ``write_director_segments``，桩模型抓的是**模型眼前**的那份请求。两边
    不同就说明导出脚本与真实组装分叉了——那让整条 diff 对照失效。
    """
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_BLOCK_HEAD,
        get_opening_still_description,
    )
    from minimax_h3_prompt.director.prompts import write_director_segments
    from minimax_h3_prompt.session_store import load_session
    from minimax_h3_prompt.ui.wizard_director import write_context_blocks

    session = _write_session(tmp_path / "GEN001")
    _opening_still(session)
    out = tmp_path / "dump"

    assert main([str(session), "--out", str(out)]) == 0
    exported = (out / "segment-01.request.txt").read_text(encoding="utf-8")
    assert OPENING_STILL_BLOCK_HEAD in exported, "导出的开场段请求没带起点画面块"
    assert _OPENING_DESCRIPTION in exported, "sidecar 里的描述没进导出的请求"

    plan = read_plan_json(plan_path(session))
    description = get_opening_still_description(session)
    assert description == _OPENING_DESCRIPTION, "sidecar 命中不该重读图"
    state = load_session(session).stage_state
    blocks = write_context_blocks(state, state["shot_table"])
    llm = _RecordingLLM(plan)
    write_director_segments(
        plan, llm=llm, session_dir=session,
        shot_table=state["shot_table"],
        start_frame=True, start_frame_description=description,
        revision_context=blocks["revision_context"],
        visual_decision=blocks["visual_decision"])

    assert llm.requests, "桩模型没收到写段请求"
    assert llm.requests[0] == exported, \
        "导出的开场段请求与真实写段发出的那份不是同一份字节"


@pytest.mark.parametrize("stale", ["缺失", "过期"])
def test_missing_or_stale_opening_sidecar_fails_with_the_image_path(
        tmp_path, capsys, monkeypatch, stale):
    """AC2：图在盘上、sidecar 缺失或已过期 → 报错，点明图路径与「真实写段会重读」。

    静默略过会让「改前/改后」对照少一块而没人发现；现场读图又会把一条只读命令变成
    付费且改会话目录的操作——两边都不行，只能报错说清病因。
    """
    session = _write_session(tmp_path / "GEN001")
    image = _opening_still(session)
    sidecar = image.with_name(image.stem + ".meta.json")
    if stale == "缺失":
        sidecar.unlink()
    else:
        # 过期＝图换了但 sidecar 还是旧图的：指纹对不上，命中判据三条缺一条。
        sidecar.write_text(json.dumps({
            "kind": OPENING_STILL_KIND,
            "description": "旧图的画面：窗在人物身前。",
            "image_sha256": "0" * 64,
        }, ensure_ascii=False), encoding="utf-8")
    calls = _forbid_vision(monkeypatch)
    out = tmp_path / "dump"

    assert main([str(session), "--out", str(out)]) == 1

    err = capsys.readouterr().err
    assert str(image) in err, "错误里要点名是哪张图"
    assert "重读" in err, "要说清真实写段会当场重读这张图"
    assert calls == [], "缓存缺失不许现场读图（付费且会改会话目录）"
    assert not out.exists(), "失败时不该留下半份产物"


def test_export_never_calls_the_vision_model(tmp_path, monkeypatch):
    """AC3：导出过程中视觉调用零次——缓存命中就只读 sidecar。"""
    session = _write_session(tmp_path / "GEN001")
    _opening_still(session)
    calls = _forbid_vision(monkeypatch)

    assert main([str(session), "--out", str(tmp_path / "dump")]) == 0

    assert calls == [], f"导出过程中调了视觉模型：{calls}"


def test_without_the_image_the_export_is_the_pre_change_request(tmp_path):
    """AC4：盘上无图时导出照常，请求与改动前的组装逐字节相同。

    对照物是 ``build_segment_request`` 的**默认路径**（不传这次新增的两个实参）——
    那正是改动前导出的组装方式；多一个字节都说明「没有图」也悄悄改了请求。
    """
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD
    from minimax_h3_prompt.director.prompts import (
        _resolve_visual_decision,
        build_segment_request,
    )
    from minimax_h3_prompt.session_store import load_session
    from minimax_h3_prompt.ui.wizard_director import write_context_blocks

    session = _write_session(tmp_path / "GEN001")
    out = tmp_path / "dump"

    assert main([str(session), "--out", str(out)]) == 0
    exported = (out / "segment-01.request.txt").read_text(encoding="utf-8")
    assert OPENING_STILL_BLOCK_HEAD not in exported, "没有图却带了起点画面块"

    plan = read_plan_json(plan_path(session))
    state = load_session(session).stage_state
    blocks = write_context_blocks(state, state["shot_table"])
    baseline = build_segment_request(
        plan, plan.segments[0], shot_table=state["shot_table"],
        revision_context=blocks["revision_context"],
        visual_decision=_resolve_visual_decision(
            blocks["visual_decision"], plan.segments[0]))
    assert exported == baseline, "没有图时导出的请求与改动前的组装不是同一份字节"


def test_report_shows_which_segment_carried_the_opening_still_block(
        tmp_path, capsys):
    """AC5：报告能看出本段是否带了起点块——会话摘要与 index.json 两处都要看得到。

    只有开场段带；续接段的字段是「没有」而不是缺键（缺键会被读成「脚本不认识」）。
    """
    session = _write_session(tmp_path / "GEN001")
    image = _opening_still(session)
    out = tmp_path / "dump"

    assert main([str(session), "--out", str(out)]) == 0
    stdout = capsys.readouterr().out
    rows = _index(out)["segments"]

    first, second = rows
    assert first["opening_still_block_chars"] > 0, "开场段带了起点块却没报出来"
    assert second["opening_still_block_chars"] == 0, "续接段不该带起点块"
    assert first["opening_still_block_sha256"] and not second["opening_still_block_sha256"]
    assert _index(out)["opening_still_image"] == str(image), \
        "索引要能指出这份导出用的是哪张开场首帧图"
    # 人读摘要里也看得出第 1 段带、第 2 段不带。
    report = stdout
    assert report.count("起点画面块") == 2, "每段都要有一行「带没带起点块」"
    assert "（不带）" in report


def test_modes_without_a_first_frame_slot_ignore_a_stray_image(tmp_path):
    """无首帧槽的模式（t2v）不因盘上残留一张 ``assets/01/start`` 就要 sidecar。

    判据问给图契约（``needs_first_frame_image(FIRST_SEGMENT_TYPE, mode)``），不写模式
    字面量：真实写段在 t2v 下也不读这张图，导出同样不该为它报错或注入。
    """
    session = _write_session(tmp_path / "GEN001", mode="t2v")
    stray = session / "director" / "assets" / "01" / "start.png"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray-image-without-a-sidecar")
    out = tmp_path / "dump"

    assert main([str(session), "--out", str(out)]) == 0, \
        "t2v 没有首帧槽，不该因盘上残留的图要求读图 sidecar"
    assert _index(out)["opening_still_image"] == ""
    for row in _index(out)["segments"]:
        assert row["opening_still_block_chars"] == 0, "t2v 不该带起点画面块"

