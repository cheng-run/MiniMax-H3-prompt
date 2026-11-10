"""D2-1（#215）：工作台会话存储——建会话、幂等、列表、详情。

存储层是 API 的唯一业务入口；这些用例钉住 D2-AC1 / AC4（建会话部分）/ AC12 的
数据面，HTTP 形状由 ``tests/test_api_sessions.py`` 单独钉。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import SESSION_FILENAME, LEGACY_SCHEMA_VERSION
from minimax_h3_prompt.workbench_store import (
    IdempotencyConflict,
    IdempotencyRegistry,
    SessionLegacy,
    SessionNotFound,
    WorkbenchSessionStore,
)


def _brief(plot: str = "雨夜旧信") -> Brief:
    return Brief(mode="base", variant="T2VA", duration=4.0, plot=plot)


def _store(tmp_path: Path) -> WorkbenchSessionStore:
    return WorkbenchSessionStore(tmp_path)


def test_create_writes_a_workbench_session(tmp_path):
    store = _store(tmp_path)

    detail, replayed = store.create(_brief(), fingerprint="body-1")

    assert replayed is False
    assert detail.session_id.endswith("/GEN001")
    assert detail.status == "created"
    assert detail.legacy is False
    assert detail.revision == 1
    assert detail.brief.plot == "雨夜旧信"
    raw = json.loads((tmp_path / detail.session_id / SESSION_FILENAME).read_text(encoding="utf-8"))
    assert raw["schema_version"] == "workbench.v1"
    assert raw["session_id"] == detail.session_id
    assert raw["status"] == "created"
    assert raw["revision"] == 1


def test_create_twice_allocates_the_next_generation(tmp_path):
    store = _store(tmp_path)

    first, _ = store.create(_brief(), fingerprint="body-1")
    second, _ = store.create(_brief(), fingerprint="body-2")

    assert first.session_id != second.session_id
    assert first.session_id.endswith("/GEN001")
    assert second.session_id.endswith("/GEN002")


def test_same_idempotency_key_replays_without_creating_a_directory(tmp_path):
    store = _store(tmp_path)

    first, replayed_first = store.create(
        _brief(), idempotency_key="key-1", fingerprint="body-1")
    second, replayed_second = store.create(
        _brief(), idempotency_key="key-1", fingerprint="body-1")

    assert replayed_first is False
    assert replayed_second is True
    assert second.session_id == first.session_id
    assert second.revision == first.revision
    generations = sorted(p.name for p in (tmp_path / Path(first.session_id).parent).iterdir()
                         if p.is_dir())
    assert generations == ["GEN001"], "幂等重发又建了一个会话目录"


def test_same_key_with_a_different_body_is_a_conflict(tmp_path):
    store = _store(tmp_path)
    store.create(_brief(), idempotency_key="key-1", fingerprint="body-1")

    with pytest.raises(IdempotencyConflict):
        store.create(_brief(), idempotency_key="key-1", fingerprint="body-2")


def test_idempotency_registry_keeps_only_the_last_50_keys(tmp_path):
    registry = IdempotencyRegistry(tmp_path / "idempotency.json")

    for index in range(55):
        registry.record(f"key-{index}", fingerprint=f"body-{index}", response={"n": index})

    assert registry.replay("key-0", fingerprint="body-0") is None, "最老的键没有被淘汰"
    assert registry.replay("key-5", fingerprint="body-5") == {"n": 5}
    assert registry.replay("key-54", fingerprint="body-54") == {"n": 54}


def _write_raw(root: Path, session_id: str, *, updated_at: str,
               schema: str = "workbench.v1", status: str = "created") -> Path:
    directory = root / Path(session_id)
    directory.mkdir(parents=True)
    (directory / SESSION_FILENAME).write_text(json.dumps({
        "schema_version": schema,
        "session_id": session_id if schema == "workbench.v1" else "",
        "status": status,
        "revision": 1 if schema == "workbench.v1" else 0,
        "last_event_seq": 0,
        "jobs": [],
        "action_items": [],
        "brief": {"mode": "base", "variant": "T2VA", "duration": 4.0,
                  "plot": session_id.split("/")[0]},
        "stage_state": {},
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": updated_at,
    }, ensure_ascii=False), encoding="utf-8")
    return directory


def test_list_is_newest_first_and_paginates(tmp_path):
    store = _store(tmp_path)
    _write_raw(tmp_path, "a/GEN001", updated_at="2026-11-01T00:00:03+00:00")
    _write_raw(tmp_path, "b/GEN001", updated_at="2026-11-01T00:00:02+00:00")
    _write_raw(tmp_path, "c/GEN001", updated_at="2026-11-01T00:00:01+00:00")

    first = store.list_sessions(limit=2)

    assert [item.session_id for item in first.items] == ["a/GEN001", "b/GEN001"]
    assert first.next_cursor
    second = store.list_sessions(limit=2, cursor=first.next_cursor)
    assert [item.session_id for item in second.items] == ["c/GEN001"]
    assert second.next_cursor is None


def test_list_includes_legacy_sessions_with_the_flag(tmp_path):
    store = _store(tmp_path)
    _write_raw(tmp_path, "旧主题/GEN001", updated_at="2026-10-01T00:00:00+00:00",
               schema=LEGACY_SCHEMA_VERSION, status="completed")

    page = store.list_sessions()

    assert len(page.items) == 1
    item = page.items[0]
    assert item.legacy is True
    assert item.session_id == "旧主题/GEN001"
    assert item.status == "completed"


def test_detail_lists_known_artifacts_with_size(tmp_path):
    store = _store(tmp_path)
    detail, _ = store.create(_brief(), fingerprint="body-1")
    directory = tmp_path / detail.session_id
    (directory / "video-prompt.md").write_text("提示词正文", encoding="utf-8")
    (directory / "summary-zh.md").write_text("摘要", encoding="utf-8")

    loaded = store.get_session(detail.session_id)

    by_name = {item.name: item for item in loaded.artifacts}
    assert set(by_name) == {"video-prompt.md", "summary-zh.md"}
    assert by_name["video-prompt.md"].bytes == len("提示词正文".encode("utf-8"))
    assert by_name["video-prompt.md"].kind == "video_prompt"
    assert by_name["video-prompt.md"].artifact_id == (
        f"{detail.session_id}/video-prompt.md")


def test_legacy_detail_is_readable_but_cannot_advance(tmp_path):
    store = _store(tmp_path)
    directory = _write_raw(
        tmp_path, "旧主题/GEN001", updated_at="2026-10-01T00:00:00+00:00",
        schema=LEGACY_SCHEMA_VERSION, status="completed")
    (directory / "video-prompt.md").write_text("旧提示词", encoding="utf-8")

    detail = store.get_session("旧主题/GEN001")

    assert detail.legacy is True
    assert detail.status == "completed"
    assert detail.revision == 0
    assert [item.name for item in detail.artifacts] == ["video-prompt.md"]
    with pytest.raises(SessionLegacy):
        store.ensure_can_advance(detail)


def test_unknown_and_traversal_ids_are_not_found(tmp_path):
    store = _store(tmp_path)

    for session_id in ("missing/GEN001", "../secret", "a/../../secret", "", "/abs"):
        with pytest.raises(SessionNotFound):
            store.get_session(session_id)


def test_session_id_mismatch_is_loud(tmp_path):
    """冗余的 session_id 是自校验：文件被搬过/改过必须响，不许按目录静默继续。"""
    store = _store(tmp_path)
    directory = _write_raw(
        tmp_path, "a/GEN001", updated_at="2026-11-01T00:00:00+00:00")
    raw = json.loads((directory / SESSION_FILENAME).read_text(encoding="utf-8"))
    raw["session_id"] = "other/GEN001"
    (directory / SESSION_FILENAME).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="session_id"):
        store.get_session("a/GEN001")

