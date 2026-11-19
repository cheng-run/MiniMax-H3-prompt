"""#346：``scripts/acceptance_report.py`` 的秒级守卫。

取数脚本的外部契约只有两条：给一个会话目录，把五类事实打到屏幕上；给一个不存在
的目录，给可读错误并非零退出。用例不碰真实 ``output/sessions``——那里的会话会随
本机跑过的生成漂移，钉在上面等于让守卫随产物过期。这里用 ``tmp_path`` 造最小会话
目录，只断言「屏幕上出现了什么」与退出码，不断言内部函数怎么取数。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "acceptance_report.py"
SPEC = importlib.util.spec_from_file_location("acceptance_report", SCRIPT)
assert SPEC and SPEC.loader
acceptance_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acceptance_report
SPEC.loader.exec_module(acceptance_report)

# 段词里只有 non_diegetic_music 一节是断言材料；其余字段刻意留空，
# 让「取配乐原文」这件事不与正文措辞耦合。
SEGMENT_PROMPT = """integrated_multimodal_description: 她坐在堤沿看落日。

overall_soundscape: 海风与浪声。

non_diegetic_music: The same slow, sparse cue continues: a low sustained
string drone beneath a single repeated piano note.
"""


def _write_session(root: Path) -> Path:
    """造一个最小 base 长片会话目录（首尾帧链：segments/shot-ledger.jsonl）。"""
    root.mkdir()
    (root / "session-state.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "brief": {
                    "mode": "base",
                    "variant": "FL2VA",
                    "duration": 18.0,
                    "plot": "她坐在堤沿看落日。",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seg = root / "segments"
    seg.mkdir()
    rows = [
        {
            "segment": 1,
            "start_s": 0,
            "end_s": 8,
            "video": str(seg / "missing-seg1.mp4"),
            "sha256": "a" * 64,
        },
        {
            "segment": 2,
            "start_s": 8,
            "end_s": 14,
            "video": str(seg / "seg2.mp4"),
            "sha256": "b" * 64,
        },
    ]
    (seg / "shot-ledger.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    # 一个 0 字节的「视频」：文件在、但读不出参数——「在/缺」两支都要能看到。
    (seg / "seg2.mp4").write_bytes(b"")
    (seg / "shot-01.md").write_text(SEGMENT_PROMPT, encoding="utf-8")
    bridges = seg / "bridge_frames"
    bridges.mkdir()
    (bridges / "shot-01-start.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_reports_the_five_fact_groups(tmp_path, capsys):
    """正常会话目录：会话参数、成片、段账、配乐原文、桥接帧五类都要出现。"""
    root = _write_session(tmp_path / "GEN001")

    code = acceptance_report.main(["acceptance_report.py", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    # 1. 会话参数
    assert "mode = 'base'" in out
    assert "variant = 'FL2VA'" in out
    assert "duration = 18.0" in out
    assert "status = 'completed'" in out
    # 2. 成片（这里没有 final.mp4：要如实打「缺失」，不是静默跳过这一段）
    assert "[成片]" in out
    assert "缺失" in out
    # 3. 段账逐段：视频「缺」与「在」两支、配乐原文
    assert "shot-ledger.jsonl" in out
    assert "段 1" in out and "视频=缺" in out
    assert "段 2" in out and "视频=在" in out
    # 4. 逐段 non_diegetic_music 原文
    assert "non_diegetic_music" in out
    assert "low sustained" in out
    # 5. 桥接帧清单
    assert "[桥接帧]" in out
    assert "shot-01-start.png" in out


def test_frame_fields_are_printed_when_the_ledger_records_them(tmp_path, capsys):
    """段账带帧字段时（导演台形态）帧区间与导出帧数要打出来，不是只认秒。"""
    root = _write_session(tmp_path / "GEN002")
    (root / "segments" / "shot-ledger.jsonl").unlink()
    row = {
        "event": "output",
        "segment": 1,
        "start_frame": 0,
        "end_frame": 107,
        "export_frames": 107,
        "video": str(root / "segments" / "missing-seg1.mp4"),
    }
    (root / "segments" / "segment-ledger.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    code = acceptance_report.main(["acceptance_report.py", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert "segment-ledger.jsonl" in out
    assert "帧区间=0–107" in out
    assert "导出帧数=107" in out


def test_missing_directory_is_a_readable_error(tmp_path, capsys):
    """目录不存在：点名那个路径、非零退出，而不是抛 traceback。"""
    missing = tmp_path / "没有这个会话"

    code = acceptance_report.main(["acceptance_report.py", str(missing)])
    out = capsys.readouterr().out

    assert code != 0
    assert "[错误]" in out
    assert "没有这个会话" in out


def test_does_not_write_into_the_session_directory(tmp_path, capsys):
    """脚本只读：跑完前后会话目录的文件内容与清单逐字节不变。"""
    root = _write_session(tmp_path / "GEN003")
    before = _snapshot(root)

    acceptance_report.main(["acceptance_report.py", str(root)])

    assert _snapshot(root) == before
