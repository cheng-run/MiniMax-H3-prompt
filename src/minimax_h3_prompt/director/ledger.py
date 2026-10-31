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

# 通过行的**旧必填集**：#58 复审实测过「行在但字段被剥掉、续跑照跳」的形态，
# 判据建立在行写全了之上。这份集合#112 起保持不变——缺这些仍是账坏了。
_REQUIRED_OUTPUT_FIELDS = (
    "segment", "start_frame", "end_frame", "declared_frames", "export_frames",
    "actual_frames", "cache", "sha256", "at",
)

# #112 起**判定证据只有成片视频**。它们不进上面的旧必填集：旧账（升级前写的行）
# 没有这些字段是**无证据**，要重新贴视频，不是「账坏了」——直接加进必填集会让人
# 升级后连陪跑都进不去（票面明令）。新行则必须写全。
_VIDEO_EVIDENCE_FIELDS = ("video", "video_sha256", "source")


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
    video_path: str | Path,
    cache_path: str | Path | None = None,
    end_image_path: str | Path | None = None,
) -> Path:
    """追加一行通过行：#112 起证据是**成片视频**，缓存路径只是现场线索。

    记 SHA256 与多参账同一条理由（文件名可变引用）：视频与缓存都会被下一轮重跑覆盖，
    只记路径的话旧行会静默指向新内容。两个摘要**写入时现算**，不收参数——收参数就多
    一个「传错了也没人知道」的口子。

    ``cache_path`` 允许 ``None``：插件只为「分段重跑 / refine / 段间引导」写缓存，
    用户手里那条视频与缓存是否存在无关（#112 的现场）。此时 ``cache``/``sha256``
    写 ``null``，键仍在——读账的人不必分辨「没有缓存」与「旧账缺字段」。

    ``end_image_path`` 是用户生成并挂到导演台尾帧槽的**本段终点图**（可选）。
    下一段写尾帧词时优先拿它当参考图：它比「本段成片视频末帧」干净——视频末帧是
    生成结果，每段都会漂一点，拿它当参考会把漂移逐段放大（2026-10-31 实跑：
    第 3 段起戴墨镜、第 4 段长胡子）。旧账没有这个键 → 读的人回退到视频末帧。

    三条路径都存**绝对路径**：账本会被拿到别的工作目录去读（续跑、#108 归档）。
    """
    path = segment_ledger_path(directory)
    video = Path(video_path)
    record = {
        "event": SEGMENT_EVENT_OUTPUT,
        "segment": int(segment),
        "start_frame": int(start_frame),
        "end_frame": int(end_frame),
        "declared_frames": int(declared_frames),
        "export_frames": int(export_frames),
        "actual_frames": int(actual_frames),
        "video": str(video.resolve()),
        "video_sha256": sha256_file(video),
        "source": "video",
        "cache": str(Path(cache_path).resolve()) if cache_path is not None else None,
        "sha256": sha256_file(Path(cache_path)) if cache_path is not None else None,
        "end_image": (
            str(Path(end_image_path).resolve())
            if end_image_path is not None else None),
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


def _split_director_progress(
        directory: str | Path, *, total_segments: int) -> tuple[dict[int, dict], tuple[int, ...]]:
    """把「最后一条 output 行」分成两类：**有视频证据**与**旧账无证据**。

    判据只有一条：#112 起「这段过了」＝行里有 ``video``。旧行（只有缓存）不是坏账，
    是「这段需要重新贴视频」——两者的处置完全不同，混成同一个返回值会让调用方
    在第二处重做这份区分（「同一契约不许两处口径」）。

    硬核对仍保留：段号必须在本张段表内（越界＝账与段表不是同一份）；有视频的行
    必须写全新字段、旧行必须写全旧字段，否则是真坏账，照抛不误。
    """
    valid: dict[int, dict] = {}
    missing_video: list[int] = []
    for segment, row in sorted(passed_director_rows(directory).items()):
        if not 1 <= segment <= total_segments:
            raise ValueError(
                f"段产出账里的段号 {segment} 超出段表范围 1..{total_segments}："
                f"{segment_ledger_path(directory)} 与当前 plan.json 不是同一份")
        has_video = bool(row.get("video"))
        # 旧必填集对两类行都查；视频三字段只对有视频的新行查（旧账缺它们＝无证据）。
        required = _REQUIRED_OUTPUT_FIELDS + (
            _VIDEO_EVIDENCE_FIELDS if has_video else ())
        missing = [name for name in required if name not in row]
        if missing:
            raise ValueError(
                f"段产出账第 {segment} 段的通过行缺字段：{'、'.join(missing)}"
                f"（{segment_ledger_path(directory)}）——行写不全就不算「这段过了」，"
                "续跑拿它跳段等于放行一段没人生成过的段")
        if has_video:
            valid[segment] = row
        else:
            missing_video.append(segment)
    return valid, tuple(missing_video)


def read_director_progress(directory: str | Path, *, total_segments: int) -> dict[int, dict]:
    """续跑要的「**有视频证据**的已通过段」；账与段表对不上就抛，绝不静默从零开始。

    #112 起旧账行（没有 ``video``）不算通过：它们没有本票要求的证据，必须重新贴视频。
    这类段的清单由 ``read_director_missing_video`` 给出，调用方负责点名。
    """
    valid, _ = _split_director_progress(directory, total_segments=total_segments)
    return valid


def read_director_missing_video(directory: str | Path, *,
                                total_segments: int) -> tuple[int, ...]:
    """续跑时账里有通过行、但缺 ``video`` 的段号（#112：要重新贴视频）。

    **两条「通过」视图不是同一个口径，不许互相替代**（2026-10-31 复审更正）：

    - 本函数与 ``read_director_progress`` 走 ``_split_director_progress``：口径是
      「**有视频证据**的已通过段」。缺 ``video`` 的旧账行不算通过，落到本函数的返回里。
    - ``passed_director_rows`` 是另一条口径：「最后一条 output/invalidate 行是 output」
      的历史视图，**不要求 video**。级联（#106）与尾帧起点（#114）有意用它——它们要
      的是「账上曾有通过行」这条事实；换成有视频口径会把旧账上的段当成没跑过。

    单列这个读口而不是让调用方自己翻 ``passed_director_rows``：续跑出站点名的段
    与实际要重跑的段必须来自同一处判据，否则两者会各走各的。
    """
    _, missing = _split_director_progress(directory, total_segments=total_segments)
    return missing


def rotate_director_ledger(directory: str | Path) -> Path | None:
    """新一次交付开头把上一轮的账挪成 ``.bak``（复用多参的轮转口，不另写一份）。

    为什么必须做：``ui.wizard._session_dir`` 只要会话不是 completed 就复用同一个
    GEN，而用户可能**拒绝续接**、重走一遍。那时 ``director/plan.json`` 与段文件都被
    新一轮整份覆盖，唯独这本账是追加式的——不挪开，新一次陪跑会读到上一轮的通过行，
    把「第 1 段已通过」当成本轮进度照跳（#58 实测的镜像形态）。
    """
    return rotate_segment_ledger(directory)
