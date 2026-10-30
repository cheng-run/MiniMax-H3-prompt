"""导演台段产出账（issue #105 / #106 / #108，设计 §3.6）。

**沿用** ``segments/segment-ledger.jsonl``（``ref_segments.segment_ledger_path``），
不新开第三本账：那本账的行原本就是「段号＋判定＋产物路径」，导演台要记的是同一类事
（段号＋实际帧数＋缓存路径）。再开一本只会长出第二本会漂的真相。

导演台只写两类行：

- ``event=output``：一段通过可证层（字段见 ``log_director_output``）；
- ``event=invalidate``：作废某段的通过行（追加式，绝不改写旧行）。

**不写 ``encounter``**：那是多参语义层（首末帧读图 ＋ 人判）的遭遇记录，导演台的
语义验收在导演台 UI 里完成，项目侧只做可证层。

``passed`` 的判据是本模块最容易被抄错的一处：必须**按段号取最后一条
output/invalidate 行**，是 output 才算通过。``ref_segments.passed_segment_rows``
只按 event 过滤 output、**完全忽略 invalidate**，直接拿它判「现在是否通过」会把已作废
的段当成通过（#106 级联一上线就咬人）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..asset_ledger import sha256_file
from ..ref_segments import (
    SEGMENT_EVENT_OUTPUT,
    _append_row,
    read_segment_ledger,
    rotate_segment_ledger,
    segment_ledger_path,
)

# 导演台写的那一类「作废」行。``output`` 复用多参的常量，不再造一份同义值。
DIRECTOR_EVENT_INVALIDATE = "invalidate"

# 导演台**允许写**的事件集合（#105 复审口径：本清单就是白名单，加行先改这里）。
DIRECTOR_EVENTS = (SEGMENT_EVENT_OUTPUT, DIRECTOR_EVENT_INVALIDATE)

# 通过行里必须有的字段：账本自己说清「这段的产物是什么、几个帧」。#58 复审实测过
# 「行在但字段被剥掉、续跑照跳」的形态——判据建立在行写全了之上。
_REQUIRED_OUTPUT_FIELDS = (
    "segment", "start_frame", "end_frame", "declared_frames", "export_frames",
    "actual_frames", "cache", "sha256", "at",
)


def utc_now_iso() -> str:
    """导演台产物时间戳的唯一实现（复审 H7）：账本与交付记录共用一份。

    此前 ``ledger._now`` 与 ``delivery._now`` 各写一遍 ``datetime.now(timezone.utc)
    .isoformat()``——两处各写一次时，改一处（比如换成带毫秒的格式）另一处不跟着变，
    同一本会话里的时间戳就会长出两种口径。时间戳是「这是哪一次运行」的唯一线索，
    不多此一举地做成两份。
    """
    return datetime.now(timezone.utc).isoformat()


def log_director_output(
    directory: str | Path,
    *,
    segment: int,
    start_frame: int,
    end_frame: int,
    declared_frames: int,
    export_frames: int,
    actual_frames: int,
    cache_path: str | Path,
) -> Path:
    """追加一行通过行：段号、成片坐标、两种计划帧、实际帧数、缓存路径与 SHA256。

    记 **SHA256** 与多参账同一条理由（文件名可变引用）：缓存文件会被下一轮重跑覆盖，
    只记路径的话旧行会静默指向新内容。摘要**写入时现算**，不收参数——收参数就多一个
    「传错了也没人知道」的口子。

    ``cache`` 存**绝对路径**：账本会被拿到别的工作目录去读（续跑、#108 归档）。
    """
    path = segment_ledger_path(directory)
    record = {
        "event": SEGMENT_EVENT_OUTPUT,
        "segment": int(segment),
        "start_frame": int(start_frame),
        "end_frame": int(end_frame),
        "declared_frames": int(declared_frames),
        "export_frames": int(export_frames),
        "actual_frames": int(actual_frames),
        "cache": str(Path(cache_path).resolve()),
        "sha256": sha256_file(Path(cache_path)),
        "at": utc_now_iso(),
    }
    _append_row(path, record)
    return path


def log_director_invalidate(directory: str | Path, *, segment: int, reason: str) -> Path:
    """追加一行作废行（#106 级联用）：不改写旧行，靠「最后一条」的次序表达作废。

    追加而不删除的理由与所有追加账一致：旧行是当时那次通过的凭据，抹掉它就再也回答
    不了「这一段当初为什么过、后来为什么又不算了」。
    """
    path = segment_ledger_path(directory)
    record = {
        "event": DIRECTOR_EVENT_INVALIDATE,
        "segment": int(segment),
        "reason": str(reason),
        "at": utc_now_iso(),
    }
    _append_row(path, record)
    return path


def read_director_rows(directory: str | Path) -> list[dict]:
    """导演台相关的行（output ＋ invalidate），按写入顺序；坏行由底层读口抛出。"""
    return [
        row for row in read_segment_ledger(directory, event=None)
        if row.get("event") in DIRECTOR_EVENTS
    ]


def passed_director_rows(directory: str | Path) -> dict[int, dict]:
    """``{段号: 通过行}``——同一段取**最后一条** output/invalidate 行，是 output 才收。

    这是导演台「这一段现在算不算过」的**唯一**判据；别用 ``passed_segment_rows``
    （它忽略 invalidate）。
    """
    rows: dict[int, dict] = {}
    for row in read_director_rows(directory):
        rows[int(row.get("segment", 0))] = row
    return {segment: row for segment, row in rows.items()
            if row.get("event") == SEGMENT_EVENT_OUTPUT}


def read_director_progress(directory: str | Path, *, total_segments: int) -> dict[int, dict]:
    """续跑要的「已通过段」；账与段表对不上就抛，绝不静默从零开始。

    两条硬核对：

    1. 段号必须在 ``1..total_segments`` 内——越界行说明这本账不属于这张段表；
    2. 通过行必须写全 ``_REQUIRED_OUTPUT_FIELDS``——「行在＝这段过了」建立在这份
       证据完整之上。
    """
    passed = passed_director_rows(directory)
    for segment, row in sorted(passed.items()):
        if not 1 <= segment <= total_segments:
            raise ValueError(
                f"段产出账里的段号 {segment} 超出段表范围 1..{total_segments}："
                f"{segment_ledger_path(directory)} 与当前 plan.json 不是同一份")
        missing = [name for name in _REQUIRED_OUTPUT_FIELDS if name not in row]
        if missing:
            raise ValueError(
                f"段产出账第 {segment} 段的通过行缺字段：{'、'.join(missing)}"
                f"（{segment_ledger_path(directory)}）——行写不全就不算「这段过了」，"
                "续跑拿它跳段等于放行一段没人生成过的段")
    return passed


def rotate_director_ledger(directory: str | Path) -> Path | None:
    """新一次交付开头把上一轮的账挪成 ``.bak``（复用多参的轮转口，不另写一份）。

    为什么必须做：``ui.wizard._session_dir`` 只要会话不是 completed 就复用同一个
    GEN，而用户可能**拒绝续接**、重走一遍。那时 ``director/plan.json`` 与段文件都被
    新一轮整份覆盖，唯独这本账是追加式的——不挪开，新一次陪跑会读到上一轮的通过行，
    把「第 1 段已通过」当成本轮进度照跳（#58 实测的镜像形态）。
    """
    return rotate_segment_ledger(directory)
