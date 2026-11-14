"""D2-1（#215）：工作台会话存储——建会话、幂等、列表、详情。

存储层是 API 的唯一业务入口；这些用例钉住 D2-AC1 / AC4（建会话部分）/ AC12 的
数据面，HTTP 形状由 ``tests/test_api_sessions.py`` 单独钉。
"""
from __future__ import annotations

import json
import threading
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


# ---------------------------------------------------------------------------
# D6-1：段列表进快照与详情（spec §10 / §11）
# ---------------------------------------------------------------------------

def _segment_rows() -> list[dict]:
    return [
        {"index": 1, "start_s": 0, "end_s": 6, "shots_in_segment": [1, 2],
         "summary": "开场：雨夜街口", "end_hook": "人物停在旧楼门口",
         "status": "pending", "prompt_artifact_id": "", "video_asset_id": ""},
        {"index": 2, "start_s": 6, "end_s": 12, "shots_in_segment": [3],
         "summary": "收束：抬头看向楼内", "end_hook": "灯亮起",
         "status": "pending", "prompt_artifact_id": "", "video_asset_id": ""},
    ]


def test_segments_default_to_empty_and_round_trip_through_the_snapshot(tmp_path):
    """段状态是**快照字段**：写进去要落盘，别的 mutate 不能把它重置回空。"""
    store = _store(tmp_path)
    detail, _ = store.create(_brief(), fingerprint="body-1")
    assert detail.segments == [], "≤10 秒的会话不该带段列表"

    rows = _segment_rows()
    store.mutate(detail.session_id, lambda _detail: {"segments": rows})

    again = store.get_session(detail.session_id)
    assert [segment.index for segment in again.segments] == [1, 2]
    assert again.segments[1].end_hook == "灯亮起"
    raw = json.loads(
        (tmp_path / detail.session_id / SESSION_FILENAME).read_text(encoding="utf-8"))
    assert raw["segments"] == rows

    # 其他字段的写入以现有快照为底，段列表原样保留。
    store.mutate(detail.session_id, lambda _detail: {"status": "running"})
    assert [segment.index for segment in store.get_session(detail.session_id).segments] == [1, 2]


def test_cached_detail_without_segments_is_still_valid(tmp_path):
    """旧幂等缓存里没有 segments：``model_validate`` 必须靠默认值放行（spec R2）。"""
    from minimax_h3_prompt.workbench_store import SessionDetail

    store = _store(tmp_path)
    detail, _ = store.create(_brief(), fingerprint="body-1")

    cached = detail.model_dump(mode="json")
    cached.pop("segments", None)

    assert SessionDetail.model_validate(cached).segments == []


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



def test_concurrent_updates_do_not_lose_each_others_fields(tmp_path):
    """执行器的进度写与取消请求来自不同线程：读-改-写必须整段互斥（#217 复审）。

    两个线程各写 100 次、只碰各自的字段。没有会话锁时，两边会读到同一份 revision
    再各自 +1，200 次成功写入只抬不到 200（丢更新）；同时先读的一方会把对方刚写的
    字段覆盖回旧值。这里同时钉住「字段都在」与「revision 等于成功写入次数」。
    """
    store = _store(tmp_path)
    detail, _ = store.create(_brief(), fingerprint="body-1")
    session_id = detail.session_id
    initial_revision = detail.revision
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write_jobs() -> None:
        try:
            barrier.wait()
            for index in range(100):
                store.update(session_id, jobs=[{
                    "job_id": f"job-{index}", "kind": "advance",
                    "status": "running", "started_at": "",
                    "finished_at": "", "error_code": None,
                    "cancelled_at_node": None, "cancel_requested": False,
                }])
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
            errors.append(exc)

    def write_stage_state() -> None:
        try:
            barrier.wait()
            for index in range(100):
                # 只用白名单里的键（``script``）：会话保存会按白名单过滤，
                # 自造键写不进文件，那样测的就不是锁而是过滤器。
                store.update(session_id, stage_state={"script": f"第{index}稿"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write_jobs),
               threading.Thread(target=write_stage_state)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors, errors

    final = store.get_session(session_id)
    assert final.jobs[-1].job_id == "job-99"
    raw = json.loads(
        (tmp_path / session_id / SESSION_FILENAME).read_text(encoding="utf-8"))
    assert raw["stage_state"]["script"] == "第99稿"
    assert final.revision == initial_revision + 200


# ---------------------------------------------------------------------------
# #253 复审：素材归属校验与待办载荷校验的共用抽法
# ---------------------------------------------------------------------------

def test_resolve_owned_media_keeps_each_entrys_code_and_message(tmp_path):
    """段视频与交付包共用归属/类型校验，但错误码与文案必须各是各的（#253 复审）。

    两个入口只差 ``error`` 与媒体类型；抽共用函数后最容易「顺手统一」文案——这条
    用例逐字钉住两边的码与四句消息。
    """
    import io

    from minimax_h3_prompt.workbench_store import ArchiveInvalid, SegmentVideoInvalid

    store = _store(tmp_path)
    session, _ = store.create(_brief(), fingerprint="body-1")
    other, _ = store.create(_brief("另一个主题"), fingerprint="body-2")

    # 非归属：前缀不是 URL 里那个会话，两边各自回自己的码。
    for resolver, error in (
        (store.resolve_video_asset, SegmentVideoInvalid),
        (store.resolve_pack_asset, ArchiveInvalid),
    ):
        with pytest.raises(error) as excinfo:
            resolver(session.session_id, f"{other.session_id}/{0:016d}")
        assert excinfo.value.code == error.code
        assert "不属于本会话" in str(excinfo.value)

    # 类型不符：同一件 png，两边各自说「必须是 … 容器」。
    png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 256)
    asset, _ = store.store_asset(session.session_id, png, filename="a.png")
    with pytest.raises(SegmentVideoInvalid) as video_exc:
        store.resolve_video_asset(session.session_id, asset.asset_id)
    assert video_exc.value.code == "segment_video_invalid"
    assert "本段视频必须是 mp4 容器" in str(video_exc.value)
    with pytest.raises(ArchiveInvalid) as pack_exc:
        store.resolve_pack_asset(session.session_id, asset.asset_id)
    assert pack_exc.value.code == "archive_invalid"
    assert "最终包必须是 zip 容器" in str(pack_exc.value)


def test_action_item_event_payload_rejects_non_string_slots():
    """四个素材槽（图槽 + 交付槽）在两种事件上都必须是字符串（D8 §7.1 / §12）。

    两条校验合并成一个按**键名列表**参数化的函数后，键表仍是模块级常量；这条用例
    逐个键钉住，漏一个键（或漏调一次）就会变红。
    """
    from minimax_h3_prompt.workbench_store import (
        InvalidEventPayload,
        _validate_action_item_created,
        _validate_action_item_resolved,
    )

    keys = ("start_asset_id", "end_asset_id", "video_asset_id", "pack_asset_id")
    for key in keys:
        with pytest.raises(InvalidEventPayload):
            _validate_action_item_created(
                {"item_id": "i1", "kind": "segment_video", key: 123})
        with pytest.raises(InvalidEventPayload):
            _validate_action_item_resolved(
                {"item_id": "i1", "decision": "approve", key: 123})
        # 字符串值本身合法：共用函数不许把正常载荷也拒掉。
        _validate_action_item_created(
            {"item_id": "i1", "kind": "segment_video", key: "s/a"})
        _validate_action_item_resolved(
            {"item_id": "i1", "decision": "approve", key: "s/a"})
