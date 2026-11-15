"""D2-4（#218）：事件流的写入契约——序号单调、类型闭合、payload 按类型校验。

这一层是 #219/#220 的写入面（待办决议、产物登记都走同一个 ``EventLog``），所以
「谁分配到几号」「哪些类型能写」必须在存储层钉死，而不是散在路由与执行器里各判一次。
用例只碰事件文件与 ``EventLog`` 的公开方法，不碰私有解析。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimax_h3_prompt.workbench_store import (
    EVENTS_FILENAME,
    EventDraft,
    EventLog,
    InvalidEventPayload,
    UnknownEventType,
)


def _directory(tmp_path: Path) -> Path:
    directory = tmp_path / "主题" / "GEN001"
    directory.mkdir(parents=True)
    return directory


def _log(directory: Path, session_id: str = "主题/GEN001") -> EventLog:
    return EventLog(directory, session_id)


def _lines(directory: Path) -> list[dict]:
    text = (directory / EVENTS_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 序号（D2-AC11）
# ---------------------------------------------------------------------------

def test_seq_starts_at_one_and_is_strictly_increasing(tmp_path):
    directory = _directory(tmp_path)
    log = _log(directory)

    first = log.append([EventDraft("session.created", {"status": "created"})])
    second = log.append([
        EventDraft("job.started", {"kind": "advance"}, job_id="j1"),
        EventDraft("job.progress",
                   {"stage": "stage1", "node": "producer", "status": "started"},
                   job_id="j1"),
    ])

    assert [event.seq for event in first] == [1]
    assert [event.seq for event in second] == [2, 3]
    assert log.last_seq() == 3
    assert [line["seq"] for line in _lines(directory)] == [1, 2, 3]


def test_seq_continues_from_the_file_not_from_memory(tmp_path):
    """新进程（新 ``EventLog`` 实例）接着文件尾号分配，不从头再来。"""
    directory = _directory(tmp_path)
    _log(directory).append([EventDraft("session.created", {"status": "created"})])

    appended = _log(directory).append([EventDraft("job.interrupted", {})])

    assert appended[0].seq == 2


def test_envelope_carries_the_documented_fields(tmp_path):
    directory = _directory(tmp_path)

    event = _log(directory).append(
        [EventDraft("job.failed", {"code": "MODEL_OUTPUT", "detail": "节点报错"},
                    job_id="j1")])[0]

    line = _lines(directory)[0]
    assert line == {
        "seq": 1,
        "type": "job.failed",
        "at": event.at,
        "session_id": "主题/GEN001",
        "job_id": "j1",
        "payload": {"code": "MODEL_OUTPUT", "detail": "节点报错"},
    }
    assert line["at"].endswith("+00:00")


def test_events_without_a_job_omit_job_id(tmp_path):
    directory = _directory(tmp_path)

    _log(directory).append([EventDraft("session.created", {"status": "created"})])

    assert "job_id" not in _lines(directory)[0]


def test_read_returns_only_events_after_the_cursor(tmp_path):
    directory = _directory(tmp_path)
    log = _log(directory)
    log.append([EventDraft("session.created", {"status": "created"})])
    log.append([EventDraft("job.started", {"kind": "advance"}, job_id="j1")])
    log.append([EventDraft("job.interrupted", {}, job_id="j1")])

    assert [event.seq for event in log.read(after_seq=1)] == [2, 3]
    assert log.read(after_seq=3) == []


def test_latest_of_type_is_none_when_the_cursor_has_no_such_event(tmp_path):
    """游标之前没有这种事件：调用方（SSE 补发）不补任何东西，不是编一条空的。"""
    directory = _directory(tmp_path)
    log = _log(directory)
    log.append([EventDraft("session.created", {"status": "created"})])

    assert log.latest_of_type("job.progress", up_to_seq=1) is None


def test_latest_of_type_picks_the_last_match_at_or_before_the_cursor(tmp_path):
    """刷新后要恢复的是**游标那一刻**的节点，不是更早的、也不是游标之后的。"""
    directory = _directory(tmp_path)
    log = _log(directory)
    log.append([
        EventDraft("job.progress",
                   {"stage": "stage1", "node": "producer", "status": "started"}),
        EventDraft("job.progress",
                   {"stage": "stage1", "node": "storyboard", "status": "started"}),
        EventDraft("action_item.created", {"item_id": "i1", "kind": "stage1_review"}),
    ])

    assert log.latest_of_type("job.progress", up_to_seq=3).seq == 2
    assert log.latest_of_type("job.progress", up_to_seq=2).seq == 2
    assert log.latest_of_type("job.progress", up_to_seq=1).seq == 1
    assert log.latest_of_type("job.progress", up_to_seq=0) is None


# ---------------------------------------------------------------------------
# 闭合类型（spec §8）
# ---------------------------------------------------------------------------

def test_unknown_event_type_is_rejected(tmp_path):
    directory = _directory(tmp_path)
    log = _log(directory)

    with pytest.raises(UnknownEventType):
        log.append([EventDraft("job.exploded", {})])

    assert log.last_seq() == 0
    assert not (directory / EVENTS_FILENAME).exists()


@pytest.mark.parametrize("event_type,payload", [
    ("session.created", {"status": "created"}),
    ("job.started", {"kind": "advance"}),
    ("job.progress", {"stage": "stage1", "node": "producer", "status": "started"}),
    ("job.progress", {"stage": "stage2", "node": "prompt_engineer", "status": "done",
                      "elapsed_ms": 12}),
    # D6 的分段阶段：仍是 ``job.progress``，只多一个派生自步骤机的 stage 取值。
    ("job.progress", {"stage": "segmenting", "node": "segment_plan", "status": "done"}),
    ("job.succeeded", {"next": "awaiting_human"}),
    # 长片阶段 2 成功即接分段规划任务：会话不落 completed，``next`` 是 running。
    ("job.succeeded", {"next": "running"}),
    ("job.failed", {"code": "MODEL_OUTPUT", "detail": "坏 JSON"}),
    ("job.cancelled", {"at_node": "producer"}),
    ("job.interrupted", {}),
    ("action_item.created", {"item_id": "i1", "kind": "stage1_review"}),
    # 待办载荷（D7-1 / spec §14）：段号、素材件号、被拒原因随事件一起写。
    ("action_item.created", {"item_id": "i1", "kind": "segment_video",
                             "segment_index": 2, "reason": "尾帧未达到本段末态"}),
    ("action_item.created", {"item_id": "i2", "kind": "asset_image",
                             "asset_index": 3}),
    ("action_item.resolved", {"item_id": "i1", "decision": "approve"}),
    ("artifact.created",
     {"artifact_id": "主题/GEN001/video-prompt.md", "kind": "video_prompt",
      "name": "video-prompt.md"}),
])
def test_all_ten_types_accept_their_documented_payload(tmp_path, event_type, payload):
    directory = _directory(tmp_path)

    appended = _log(directory).append([EventDraft(event_type, payload, job_id="j1")])

    assert appended[0].type == event_type
    assert _lines(directory)[0]["payload"] == payload


@pytest.mark.parametrize("event_type,payload", [
    ("job.progress", {"stage": "stage1", "status": "started"}),
    ("job.progress", {"stage": "stage3", "node": "x", "status": "started"}),
    ("job.progress", {"stage": "stage1", "node": "x", "status": "halfway"}),
    ("job.succeeded", {"next": "created"}),
    ("job.failed", {"code": "MODEL_OUTPUT"}),
    ("action_item.resolved", {"item_id": "i1", "decision": "maybe"}),
    ("artifact.created", {"artifact_id": "a", "kind": "video_prompt"}),
])
def test_payload_missing_or_wrong_values_are_rejected(tmp_path, event_type, payload):
    log = _log(_directory(tmp_path))

    with pytest.raises(InvalidEventPayload):
        log.append([EventDraft(event_type, payload)])


@pytest.mark.parametrize("payload", [
    # 段号与素材件号是「第几段 / 第几件」的唯一依据，写错了重放就指向别的产物；
    # 0、bool、字符串与小数都不是合法编号（``True == 1`` 是 Python 的陷阱）。
    {"item_id": "i1", "kind": "segment_video", "segment_index": 0},
    {"item_id": "i1", "kind": "segment_video", "segment_index": True},
    {"item_id": "i1", "kind": "segment_video", "segment_index": "2"},
    {"item_id": "i1", "kind": "segment_video", "segment_index": 1.5},
    {"item_id": "i1", "kind": "asset_image", "asset_index": -1},
    {"item_id": "i1", "kind": "asset_image", "asset_index": "3"},
    # 校对差异/被拒原因是给人看的文本，不是编号。
    {"item_id": "i1", "kind": "segment_video", "reason": 3},
    {"item_id": "i1", "kind": "segment_video", "reason": ["尾帧未达到本段末态"]},
])
def test_action_item_payload_fields_must_be_well_typed(tmp_path, payload):
    """待办载荷出现时必须类型正确——不能靠「重放时自然变空」静默兜住（AC2）。"""
    log = _log(_directory(tmp_path))

    with pytest.raises(InvalidEventPayload):
        log.append([EventDraft("action_item.created", payload)])


@pytest.mark.parametrize("proofread", [
    # 结论不是对象：改词作业按它驱动，形状错了会被读成「没有偏差」。
    "deviations",
    ["deviations"],
    # 两张清单必须都是列表：写成字符串/对象会变成逐字符或逐键迭代。
    {"deviations": "x", "omissions": []},
    {"deviations": [], "omissions": {}},
    # 条目必须是对象、字段必须逐一是字符串：缺字段＝改词作业读到空串（#288）。
    {"deviations": ["尾巴"], "omissions": []},
    {"deviations": [{"aspect": "尾巴", "prompt_quote": "尾巴绕住前爪"}], "omissions": []},
    {"deviations": [], "omissions": [{"aspect": "项圈"}]},
    {"deviations": [], "omissions": [{"aspect": "项圈", "fact": 3}]},
])
def test_proofread_conclusion_must_be_well_shaped(tmp_path, proofread):
    """结构化校对结论的形状是改词作业的输入契约，坏形状必须在写事件时就拒掉（#288）。"""
    log = _log(_directory(tmp_path))

    with pytest.raises(InvalidEventPayload):
        log.append([EventDraft("action_item.created", {
            "item_id": "i1", "kind": "asset_image", "asset_index": 1,
            "proofread": proofread})])


def test_a_well_shaped_proofread_conclusion_is_accepted(tmp_path):
    """合法形状放行：空清单与齐备条目都收，且原样落盘（校验不许收窄合法载荷）。"""
    directory = _directory(tmp_path)
    proofread = {
        "deviations": [{"aspect": "尾巴", "prompt_quote": "尾巴绕住前爪",
                        "observed": "尾巴垂在窗台边缘"}],
        "omissions": [{"aspect": "项圈", "fact": "脖子上有一圈红色项圈"}],
    }
    log = _log(directory)

    log.append([EventDraft("action_item.created", {
        "item_id": "i1", "kind": "asset_image", "asset_index": 1,
        "proofread": proofread})])
    log.append([EventDraft("action_item.created", {
        "item_id": "i2", "kind": "asset_image", "asset_index": 2,
        "proofread": {"deviations": [], "omissions": []}})])

    assert _lines(directory)[0]["payload"]["proofread"] == proofread


def test_payload_may_carry_extra_fields(tmp_path):
    """加字段可以：向前兼容的读者不该因为新字段而拒绝一条合法事件。"""
    directory = _directory(tmp_path)

    _log(directory).append(
        [EventDraft("job.interrupted", {"reason": "pid 已不存在"}, job_id="j1")])

    assert _lines(directory)[0]["payload"]["reason"] == "pid 已不存在"


# ---------------------------------------------------------------------------
# 崩溃安全：尾部半行
# ---------------------------------------------------------------------------

def test_a_torn_tail_line_is_ignored_and_overwritten(tmp_path):
    """崩溃可能把最后一行写了一半；它从未落进快照，重读要跳过、续写要覆盖掉。"""
    directory = _directory(tmp_path)
    log = _log(directory)
    log.append([EventDraft("session.created", {"status": "created"})])
    path = directory / EVENTS_FILENAME
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"seq": 2, "type": "job.sta')

    assert [event.seq for event in log.read()] == [1]
    assert log.last_seq() == 1

    appended = log.append([EventDraft("job.started", {"kind": "advance"}, job_id="j1")])

    assert appended[0].seq == 2
    assert [line["seq"] for line in _lines(directory)] == [1, 2]


def test_non_monotonic_seq_in_the_file_is_loud(tmp_path):
    """序号回退只可能是被人改过或两处各写一份；静默排序会把补发逻辑变成随机。"""
    directory = _directory(tmp_path)
    path = directory / EVENTS_FILENAME
    path.write_text(
        '{"seq": 1, "type": "session.created", "at": "2026-11-10T00:00:00+00:00",'
        ' "session_id": "主题/GEN001", "payload": {"status": "created"}}\n'
        '{"seq": 1, "type": "job.started", "at": "2026-11-10T00:00:01+00:00",'
        ' "session_id": "主题/GEN001", "job_id": "j1", "payload": {"kind": "advance"}}\n',
        encoding="utf-8")

    with pytest.raises(ValueError):
        _log(directory).read()
