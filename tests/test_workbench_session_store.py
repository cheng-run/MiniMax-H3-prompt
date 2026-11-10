"""D2-1（#215）：会话文件 schema 升级、原子写与 revision。

会话文件是崩溃恢复的唯一真源：写到一半被杀不许留半截文件（D2-AC2），
每次快照写入 `revision` 必须 +1，且旧的 ``wizard_session.v1`` 必须照旧读得回来。
"""
from __future__ import annotations

import json
import os

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import (
    LEGACY_SCHEMA_VERSION,
    SESSION_FILENAME,
    WORKBENCH_SCHEMA_VERSION,
    SessionState,
    load_session,
    save_session,
)


def _brief(plot: str = "雨夜旧信") -> Brief:
    return Brief(mode="base", variant="T2VA", duration=4.0, plot=plot)


def _raw(directory) -> dict:
    return json.loads((directory / SESSION_FILENAME).read_text(encoding="utf-8"))


def test_save_writes_workbench_schema_with_new_keys(tmp_path):
    """新写入的会话文件是 workbench.v1，并带齐 D2 的新键（D2-AC2）。"""
    generation_dir = tmp_path / "GEN001"

    save_session(generation_dir, _brief(), {"shot_table": "表"})

    raw = _raw(generation_dir)
    assert raw["schema_version"] == WORKBENCH_SCHEMA_VERSION
    assert raw["status"]
    assert raw["revision"] == 1
    assert raw["last_event_seq"] == 0
    assert raw["jobs"] == []
    assert raw["action_items"] == []
    # session_id 由工作台层负责填；向导直接保存时允许为空（没有工作台身份）。
    assert raw["session_id"] == ""


def test_revision_increments_on_every_snapshot_write(tmp_path):
    """每次快照写入 revision +1——即使调用方每次新建一个 SessionState。"""
    generation_dir = tmp_path / "GEN001"

    save_session(generation_dir, _brief(), {"shot_table": "一"})
    save_session(generation_dir, _brief(), {"shot_table": "二"})
    save_session(generation_dir, _brief(), {"shot_table": "三"})

    assert _raw(generation_dir)["revision"] == 3


def test_session_id_and_status_round_trip(tmp_path):
    """工作台身份与会话状态必须能落盘、能读回。"""
    generation_dir = tmp_path / "主题" / "GEN001"
    session = SessionState(
        directory=generation_dir,
        brief=_brief(),
        stage_state={},
        status="created",
        session_id="主题/GEN001",
    )
    session.save()

    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.schema_version == WORKBENCH_SCHEMA_VERSION
    assert loaded.session_id == "主题/GEN001"
    assert loaded.status == "created"
    assert loaded.revision == 1


def test_legacy_session_file_still_loads(tmp_path):
    """现存 wizard_session.v1 会话照旧读得回来（D2-AC12 的读入口）。"""
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    legacy = {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "status": "completed",
        "brief": {"mode": "base", "variant": "T2VA", "duration": 4.0, "plot": "旧会话"},
        "stage_state": {"script": "旧剧本"},
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:10:00+00:00",
    }
    (generation_dir / SESSION_FILENAME).write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = load_session(generation_dir)

    assert loaded is not None
    assert loaded.schema_version == LEGACY_SCHEMA_VERSION
    assert loaded.status == "completed"
    assert loaded.revision == 0
    assert loaded.session_id == ""
    assert loaded.stage_state["script"] == "旧剧本"


def test_interrupted_write_keeps_the_previous_file_intact(tmp_path, monkeypatch):
    """中途失败不许产生半截文件：旧文件保持完整，且不留下临时文件。

    这条是 D2-AC2 的行为判据——裸 ``write_text`` 在 replace 之前就已截断旧文件，
    所以只要把最后一步替换打断，旧实现会当场红。
    """
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "旧内容"})
    before = (generation_dir / SESSION_FILENAME).read_text(encoding="utf-8")

    def _boom(_src, _dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        save_session(generation_dir, _brief(), {"shot_table": "新内容"})

    assert (generation_dir / SESSION_FILENAME).read_text(encoding="utf-8") == before
    leftovers = [p.name for p in generation_dir.iterdir()
                 if p.name != SESSION_FILENAME]
    assert leftovers == [], f"写入失败后留下了临时文件：{leftovers}"


def test_atomic_write_is_used_for_the_real_save(tmp_path, monkeypatch):
    """正常保存也必须走 os.replace——不是只有失败路径才检查。"""
    generation_dir = tmp_path / "GEN001"
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def _record(src, dst):
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _record)

    save_session(generation_dir, _brief(), {"shot_table": "表"})

    assert replaced, "保存没有走 os.replace（仍是裸 write_text）"
    src, dst = replaced[-1]
    assert dst.endswith(SESSION_FILENAME)
    assert src != dst


def test_update_session_file_bumps_revision_and_is_atomic(tmp_path, monkeypatch):
    """不经过 SessionState 的小改写也必须原子落盘并计入 revision。"""
    from minimax_h3_prompt.session_store import update_session_file

    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "表"})

    update_session_file(generation_dir, variant_downgraded="I2VA")

    raw = _raw(generation_dir)
    assert raw["variant_downgraded"] == "I2VA"
    assert raw["revision"] == 2, "补写没有计入 revision（快照版本号会漏掉一次写入）"

    before = (generation_dir / SESSION_FILENAME).read_text(encoding="utf-8")

    def _boom(_src, _dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(os, "replace", _boom)
    assert update_session_file(generation_dir, variant_downgraded="FL2VA") is None
    assert (generation_dir / SESSION_FILENAME).read_text(encoding="utf-8") == before
