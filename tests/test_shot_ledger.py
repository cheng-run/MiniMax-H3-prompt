"""首尾帧链段产出账（issue #94）：``segments/shot-ledger.jsonl`` 的纯件缝。

为什么要有这本账：``progress.json`` 只记 ``done=N``，**不记段视频路径**——
成片拼接（#96）拼不出来，重跑本段换了新视频后也无从取最新那份。本账一段一行
（段号／起止秒／视频绝对路径／SHA256／写入时间），并成为「第几段过了」的唯一真源。

这一层只测**账本身的外部行为**：写进去读得出来、同段多行取最后一行、错形与跳号响错。
流程侧（两条产出路径各在哪个点位落账、旧会话回退）在 ``test_wizard_shot_ledger.py``。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from minimax_h3_prompt import shot_ledger


def _video(path: Path, payload: bytes = b"fake-mp4-bytes") -> Path:
    path.write_bytes(payload)
    return path


def _write_rows(directory: Path, rows: list[dict]) -> Path:
    path = shot_ledger.shot_ledger_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")
    return path


# --- 写入与读回 ---------------------------------------------------------------

def test_append_writes_one_row_with_all_fields(tmp_path):
    """一段一行，五样字段齐全；路径是**绝对路径**，摘要按文件内容现算。"""
    video = _video(tmp_path / "seg1.mp4", b"the-actual-bytes")
    expected_sha = hashlib.sha256(b"the-actual-bytes").hexdigest()

    shot_ledger.append_shot_output(
        tmp_path, segment=1, start_s=0, end_s=4, video_path=video)

    rows = shot_ledger.read_shot_ledger(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["segment"] == 1
    assert row["start_s"] == 0 and row["end_s"] == 4
    assert row["video"] == str(video.resolve()), "账上要记绝对路径，相对路径在别的 cwd 下指错文件"
    assert row["sha256"] == expected_sha
    assert row["at"], "写入时间是事后区分「哪一轮通过」的唯一凭据"


def test_append_is_append_only(tmp_path):
    """追加式：第二行不会改写第一行（重跑本段的旧记录必须留着可查）。"""
    first = _video(tmp_path / "a.mp4", b"a")
    second = _video(tmp_path / "b.mp4", b"b")

    shot_ledger.append_shot_output(tmp_path, segment=1, start_s=0, end_s=4, video_path=first)
    shot_ledger.append_shot_output(tmp_path, segment=2, start_s=4, end_s=8, video_path=second)

    rows = shot_ledger.read_shot_ledger(tmp_path)
    assert [row["segment"] for row in rows] == [1, 2]
    assert rows[0]["video"] == str(first.resolve())


def test_same_segment_takes_the_last_row(tmp_path):
    """同段多行取最后一行：重跑本段换了新视频，读者要拿到**新**的那一份。"""
    old = _video(tmp_path / "old.mp4", b"old")
    new = _video(tmp_path / "new.mp4", b"new")

    shot_ledger.append_shot_output(tmp_path, segment=1, start_s=0, end_s=4, video_path=old)
    shot_ledger.append_shot_output(tmp_path, segment=1, start_s=0, end_s=4, video_path=new)

    passed = shot_ledger.passed_shot_rows(tmp_path)
    assert list(passed) == [1]
    assert passed[1]["video"] == str(new.resolve())


def test_absent_ledger_is_an_empty_account_not_an_error(tmp_path):
    """账文件不在＝一段都没通过（追加账在零通过时本来就不存在），不是错。"""
    assert shot_ledger.read_shot_ledger(tmp_path) == []
    assert shot_ledger.passed_shot_rows(tmp_path) == {}
    assert shot_ledger.read_shot_progress(tmp_path, total_segments=3) == {}


# --- 错形与跳号：响错停住 -----------------------------------------------------

@pytest.mark.parametrize("broken, needle", [
    ("video", "video"),
    ("sha256", "sha256"),
    ("at", "at"),
    ("segment", "段号"),
])
def test_malformed_rows_are_loud(tmp_path, broken, needle):
    """缺字段／段号不是整数 → 抛错。行在＝这段过了，这条判据建立在**行写全了**之上。"""
    row = {"segment": 1, "start_s": 0, "end_s": 4,
           "video": str((tmp_path / "a.mp4").resolve()), "sha256": "x", "at": "t"}
    row[broken] = None if broken == "segment" else ""
    _write_rows(tmp_path, [row])

    with pytest.raises(ValueError) as exc:
        shot_ledger.passed_shot_rows(tmp_path)
    assert needle in str(exc.value)


def test_broken_json_is_loud(tmp_path):
    path = shot_ledger.shot_ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ 这不是 JSON\n", encoding="utf-8")

    with pytest.raises(ValueError):
        shot_ledger.passed_shot_rows(tmp_path)


def test_relative_video_path_is_loud(tmp_path):
    """字段定义是「绝对路径」：相对路径换个 cwd 就指到别的文件，拼片时会悄悄拼错。"""
    _write_rows(tmp_path, [{"segment": 1, "start_s": 0, "end_s": 4,
                            "video": "seg1.mp4", "sha256": "x", "at": "t"}])

    with pytest.raises(ValueError) as exc:
        shot_ledger.passed_shot_rows(tmp_path)
    assert "绝对路径" in str(exc.value)


def test_segment_out_of_range_is_loud(tmp_path):
    """账上的段号落在段表之外 → 账与段表不是同一次拆段的产物。"""
    _write_rows(tmp_path, [
        {"segment": 1, "start_s": 0, "end_s": 4,
         "video": str((tmp_path / "a.mp4").resolve()), "sha256": "x", "at": "t"},
        {"segment": 3, "start_s": 8, "end_s": 12,
         "video": str((tmp_path / "c.mp4").resolve()), "sha256": "y", "at": "t"},
    ])

    with pytest.raises(ValueError) as exc:
        shot_ledger.read_shot_progress(tmp_path, total_segments=2)
    assert "段表之外" in str(exc.value)


def test_skipped_segment_number_is_loud(tmp_path):
    """跳号（1、3，没有 2）→ 响错。按它续跑会跳过从没做过的段。"""
    _write_rows(tmp_path, [
        {"segment": 1, "start_s": 0, "end_s": 4,
         "video": str((tmp_path / "a.mp4").resolve()), "sha256": "x", "at": "t"},
        {"segment": 3, "start_s": 8, "end_s": 12,
         "video": str((tmp_path / "c.mp4").resolve()), "sha256": "y", "at": "t"},
    ])

    with pytest.raises(ValueError) as exc:
        shot_ledger.read_shot_progress(tmp_path, total_segments=4)
    assert "跳号" in str(exc.value)


# --- 新交付轮换旧账 -----------------------------------------------------------

def test_rotate_moves_the_ledger_and_the_legacy_progress_aside(tmp_path):
    """复用 GEN 重走一遍流程时，旧账与旧进度都必须挪开（追加账不挪会冒充本轮进度）。"""
    _write_rows(tmp_path, [{"segment": 1, "start_s": 0, "end_s": 4,
                            "video": str((tmp_path / "a.mp4").resolve()),
                            "sha256": "x", "at": "t"}])
    legacy = shot_ledger.legacy_progress_path(tmp_path)
    legacy.write_text('{"done": 1, "total": 4}', encoding="utf-8")

    moved = shot_ledger.rotate_shot_progress(tmp_path)

    assert {path.name for path in moved} == {
        shot_ledger.SHOT_LEDGER_FILENAME + ".bak", "progress.json.bak"}
    assert not shot_ledger.shot_ledger_path(tmp_path).is_file()
    assert not legacy.is_file(), "旧进度留着，新一轮会被永久当成旧会话（新账永远不写）"
    assert all(path.is_file() for path in moved), "挪而不删：旧账是那一次交付的凭据"


def test_rotate_is_a_noop_without_any_progress_file(tmp_path):
    assert shot_ledger.rotate_shot_progress(tmp_path) == []
