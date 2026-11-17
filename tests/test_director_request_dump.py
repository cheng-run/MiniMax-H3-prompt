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
from minimax_h3_prompt.director.planning import plan_path, write_plan_json
from scripts.director_request_dump import main

# 镜头块解析认 ``**[Shot N]**`` 与行内 ``At 00:SS.mmm``（``firstframe._shot_blocks``）。
SHOT_TABLE = """# 分镜表

**[Shot 1]** SHOT-ONE-PROSE At 00:00.000，远镜：海堤与空着的尽头。

**[Shot 2]** SHOT-TWO-PROSE At 00:01.500，中景：人物取手机。

**[Shot 3]** SHOT-THREE-PROSE At 00:02.400，近景：人物转身。
"""


def _plan() -> DirectorPlan:
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
            give_images=give_images_for("opening", "fl2v"),
        ),
        DirectorSegment(
            index=2, type="continue", guided=True,
            start_frame=opening_export, end_frame=opening_export + continue_export,
            declared_frames=declared_frames_for_export(continue_export, guided=True),
            export_frames=continue_export,
            summary="续接", end_hook="人物转身面向海",
            give_images=give_images_for("continue", "fl2v"),
        ),
    )
    return DirectorPlan(mode="fl2v", total_frames=opening_export + continue_export,
                        segments=segments)


def _write_session(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_plan_json(plan_path(root), _plan())
    (root / "shot-table.md").write_text(SHOT_TABLE, encoding="utf-8")
    (root / "session-state.json").write_text(json.dumps({
        "schema_version": "wizard_session.v1",
        "brief": {"mode": "director", "director_mode": "fl2v", "duration": 4.5},
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
