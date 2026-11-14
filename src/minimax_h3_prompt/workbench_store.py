"""工作台会话存储：建会话、列表、详情；文件是唯一真源（D2 spec §5）。

为什么不做成数据库：一个会话是「一个目录 + 若干产物」，用户会直接看、直接拷、
直接删；文件布局本身就是交付物的一部分。事件流与快照的分工见 ADR 0032。

这一层只认 ``sessions_root`` 之下的相对路径，不接受任意本地路径——``session_id``
是登记根之下的标识（spec §5.1），越界一律按 404 处理，不泄漏根外文件是否存在。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal, Sequence, cast

from pydantic import BaseModel, Field, PrivateAttr

from . import step_machine
from .brief_parser import Brief, is_ref_mode
from .kinds import KIND_PICTURE, KINDS
from .ref_capacity import (
    ASSET_KIND_TO_REFERENCE,
    ReferenceCapacityError,
    ReferenceItem,
    check_reference_capacity,
)
from .session_store import (
    SESSION_FILENAME,
    WORKBENCH_SCHEMA_VERSION,
    SessionState,
    atomic_write_text,
    load_session,
    save_session,
)
logger = logging.getLogger(__name__)

SessionStatus = Literal[
    "created", "running", "awaiting_human", "completed", "failed", "cancelled",
    "interrupted",
]
SESSION_STATUSES: tuple[str, ...] = (
    "created", "running", "awaiting_human", "completed", "failed", "cancelled",
    "interrupted",
)

# 执行段的状态（D6 spec §10 / #246）。规划完是 ``pending``；写词完成后等用户交视频是
# ``waiting_video``；交上视频、异步校验在跑（含失败待重试）是 ``validating``；通过是
# ``passed``。被拒的段回到 ``waiting_video``（换一段视频重交），**绝不落成 passed**
# 否则恢复会跳过它（spec §8）。
SEGMENT_STATUS_PENDING = "pending"
SEGMENT_STATUS_WAITING_VIDEO = "waiting_video"
SEGMENT_STATUS_VALIDATING = "validating"
SEGMENT_STATUS_PASSED = "passed"
SEGMENT_STATUSES: tuple[str, ...] = (
    SEGMENT_STATUS_PENDING,
    SEGMENT_STATUS_WAITING_VIDEO,
    SEGMENT_STATUS_VALIDATING,
    SEGMENT_STATUS_PASSED,
)

# 旧向导状态 → 工作台状态。历史会话全部是 completed；未完成的旧会话（用户还在
# 向导里续接的那些）对 API 也是 legacy，映射只是给列表/详情一个可读的状态，
# **不承诺它们能被 advance**——legacy 标记才是那个判据。
_LEGACY_STATUS = {
    "completed": "completed",
    "awaiting_frames": "awaiting_human",
    "stage1_running": "interrupted",
    "segmented_running": "interrupted",
    "ref_segments_running": "interrupted",
    "director_segments_running": "interrupted",
}

# 产物登记范围（D2 spec §10、D3 #222 §7、D6 §7）：登记表同时决定两件事——「哪些
# 名字算产物」与「某个名字按哪种形态读」。分成两张表各写一份时，改一处漏一处就会
# 让「按什么形态读」与「算不算产物」各说各话。
# 产物名字是契约的一部分：登记表、执行器写盘与用例都从这些常量取，不各写一份字面量
# （「守卫形态要从常量派生」——字面重抄在改名时会静默指到旧名字）。
ARTIFACT_VIDEO_PROMPT = "video-prompt.md"
# 阶段 1 的中间产物（D4 spec §7）：剧本与分镜表每次阶段 1 完成都落；首/尾帧词只有
# 帧链会话（bundle 里真有那一帧）才落，两个文件同 kind ``frame_prompt``。
ARTIFACT_SCRIPT = "script.md"
ARTIFACT_SHOT_TABLE = "shot-table.md"
ARTIFACT_FIRST_FRAME_PROMPT = "first-frame-prompt.md"
ARTIFACT_LAST_FRAME_PROMPT = "last-frame-prompt.md"
# 阶段 2 的中文摘要两件：``video-prompt.md`` 的展示层提炼（向导与工作台共用同一格式）。
ARTIFACT_SUMMARY_MARKDOWN = "summary-zh.md"
ARTIFACT_SUMMARY_JSON = "summary-zh.json"
# 成片（D6 §9）：拼接成功后登记，仍是既有 ``video`` kind 与 ``video/mp4`` 媒体类型。
ARTIFACT_FINAL_FILM = "final.mp4"
# 整片注入卡（D7 §9 / #257）：进阶段 2 前的最后一件产物，只读接线清单。槽位名与
# 正本文件名只住在卡上——它是**给人照抄的**，绝不进任何一段的模型载荷。
ARTIFACT_INJECTION_CARD = "injection-card.md"
# 多参专属的两件只读清单（D7 §11 / #258）：拼接顺序卡是段序唯一依据，多参四列分镜表
# 是段内素材编号的对照表。两者都由规划阶段写出（与 CLI 同名同址），工作台只读。
ARTIFACT_CONCAT_ORDER_CARD = "segments/order-card.md"
ARTIFACT_REF_SHOT_TABLE = "segments/ref-shot-table.md"
# 导演台初始包（D8 §9 / #263）：唯一会写的二进制产物。它是**归档形态**——不能当
# ``<video>`` 播，但走同一条 content 端点、同一套单段 Range 语义（插件里也可能很大）。
# 登记成 ``application/zip`` 后自动进 ``ARTIFACT_MEDIA_TYPES``，端点不再认第二种形态。
ARTIFACT_DIRECTOR_PACK = "director/initial.mmxpack.zip"


@dataclass(frozen=True)
class ArtifactSpec:
    """一种产物的形态：``kind`` 进契约，``media_type`` 为空表示按文本读。"""

    kind: str
    media_type: str | None = None


# 固定名表：一个名字一条记录，「算不算产物」与「按什么形态读」从同一条取。
_FIXED_ARTIFACT_SPECS: dict[str, ArtifactSpec] = {
    ARTIFACT_VIDEO_PROMPT: ArtifactSpec("video_prompt"),
    ARTIFACT_SCRIPT: ArtifactSpec("script"),
    ARTIFACT_SHOT_TABLE: ArtifactSpec("shot_table"),
    ARTIFACT_FIRST_FRAME_PROMPT: ArtifactSpec("frame_prompt"),
    ARTIFACT_LAST_FRAME_PROMPT: ArtifactSpec("frame_prompt"),
    ARTIFACT_SUMMARY_MARKDOWN: ArtifactSpec("summary_markdown"),
    ARTIFACT_SUMMARY_JSON: ArtifactSpec("summary_json"),
    # 媒体产物：名字 → Content-Type。目前只有成片；列表里的 kind 仍是 ``video``。
    ARTIFACT_FINAL_FILM: ArtifactSpec("video", "video/mp4"),
    # 注入卡是纯文本接线清单（1 MiB 上限与其余文本产物同一条口径）。
    ARTIFACT_INJECTION_CARD: ArtifactSpec("injection_card"),
    # 多参两件只读清单（D7 §11 / #258）：都是规划阶段一次写定的文本，没有写作入口。
    ARTIFACT_CONCAT_ORDER_CARD: ArtifactSpec("concat_order_card"),
    ARTIFACT_REF_SHOT_TABLE: ArtifactSpec("ref_shot_table"),
    # 导演台初始包（D8 §9 / #263）：归档形态，与成片共用 Range 语义。
    ARTIFACT_DIRECTOR_PACK: ArtifactSpec("director_pack", "application/zip"),
}

# 模式产物（D6 §7 / D7 §10）：每段一份。base 写 ``segments/shot-NN.md``，ref 写
# ``segments/segment-NN.md``——**两条模式并列**（不是改名，路线决定产物名），同一种
# kind ``segment_prompt``：读者只关心「这是第 N 段的提示词」，名字里带路线名。
# ``segments/`` 下的其他文件（``plan.json``、``shot-ledger.jsonl``）不在这条模式里。
SEGMENT_PROMPT_KIND = "segment_prompt"
SEGMENTS_DIRNAME = "segments"
_SEGMENT_PROMPT_FILENAME = r"(?:shot|segment)-\d{2}\.md"
_SEGMENT_PROMPT_SPEC = ArtifactSpec(SEGMENT_PROMPT_KIND)
# 导演台段词（D8 §8.2 / #262）：逐段 ``director/segment-NN.md``，与 base/ref 的
# ``segments/`` 目录并列。它不是 ``segment_prompt``：两条路线的段词同时出现在同一个
# 会话目录里时，按 kind 就能分开（产物分区与前端能力判据都靠它）。
DIRECTOR_DIRNAME = "director"
DIRECTOR_SEGMENT_PROMPT_KIND = "director_segment_prompt"
_DIRECTOR_SEGMENT_PROMPT_FILENAME = r"segment-\d{2}\.md"
# 逐段操作简报（D8 §8.5 / #263）：可复制文本，与段词同号、同目录。它不是段词：
# 前后端要能按 kind 把「照着在导演台里点」的清单与「丢进提示词框」的正文分开。
DIRECTOR_BRIEFING_KIND = "director_briefing"
_DIRECTOR_BRIEFING_FILENAME = r"briefing-\d{2}\.md"
# 按需词（D8 §8.3 / #264）：切镜段的首帧词与运镜/收尾段的尾帧词，逐段一份、与段号
# 同号。两者 kind 分开：产物分区里「复制去生图」的用途不同（首帧 vs 终点图变换）。
DIRECTOR_FIRST_FRAME_PROMPT_KIND = "director_first_frame_prompt"
_DIRECTOR_FIRST_FRAME_PROMPT_FILENAME = r"firstframe-\d{2}\.md"
DIRECTOR_TAILFRAME_PROMPT_KIND = "director_tailframe_prompt"
_DIRECTOR_TAILFRAME_PROMPT_FILENAME = r"tailframe-\d{2}\.md"


def director_segment_prompt_name(position: int) -> str:
    """第 ``position`` 段（1 起）的导演台段词产物名（与 CLI 落点同名同形）。"""
    return f"{DIRECTOR_DIRNAME}/segment-{position:02d}.md"


def director_briefing_name(position: int) -> str:
    """第 ``position`` 段（1 起）的导演台操作简报产物名。"""
    return f"{DIRECTOR_DIRNAME}/briefing-{position:02d}.md"


def director_first_frame_prompt_name(position: int) -> str:
    """第 ``position`` 段的段首帧词产物名（与 ``firstframe.py`` 落点同名同形）。"""
    return f"{DIRECTOR_DIRNAME}/firstframe-{position:02d}.md"


def director_tailframe_prompt_name(position: int) -> str:
    """第 ``position`` 段的尾帧词产物名（与 ``tailframe.py`` 落点同名同形）。"""
    return f"{DIRECTOR_DIRNAME}/tailframe-{position:02d}.md"

# 资产图词（D7 §7）：素材循环里每件参考素材一份 ``assets/picture-NN.md``，与台账
# 图片编号同号。改词重出**覆盖**同一份文件——历史靠台账轮次与待办决议表达。
ASSET_PROMPT_KIND = "asset_prompt"
_ASSET_PROMPT_FILENAME = r"picture-\d{2}\.md"
_ASSET_PROMPT_SPEC = ArtifactSpec(ASSET_PROMPT_KIND)
# 段级注入卡（D7 §9 / #257）：每段一份 ``segments/injection-card-NN.md``，与段词同号。
# 段循环没跑到该段时没有这份文件——页面按产物存在与否显示，不做空卡。
SEGMENT_INJECTION_CARD_KIND = "segment_injection_card"
_SEGMENT_INJECTION_CARD_FILENAME = r"injection-card-\d{2}\.md"
_SEGMENT_INJECTION_CARD_SPEC = ArtifactSpec(SEGMENT_INJECTION_CARD_KIND)
# 目录名与文件名模式分开存：解析（名字 → 规格）与扫描（去哪个目录找）都从这一份
# 取；写死在两处时，改一处就会让「清单里有、读取说没有」。
#
# ``assets/`` 同时是上传素材的落点与资产图词的落点：素材正本是
# ``<16 位内容哈希>.<扩展名>``（``_resolved_asset_file`` 只认这一种），与
# ``picture-NN.md`` 不会互相认领——两边各自按自己的命名规则判定。
ASSETS_DIRNAME = "assets"
_PATTERN_ARTIFACT_SPECS: tuple[tuple[str, str, ArtifactSpec], ...] = (
    (SEGMENTS_DIRNAME, _SEGMENT_PROMPT_FILENAME, _SEGMENT_PROMPT_SPEC),
    (SEGMENTS_DIRNAME, _SEGMENT_INJECTION_CARD_FILENAME, _SEGMENT_INJECTION_CARD_SPEC),
    (ASSETS_DIRNAME, _ASSET_PROMPT_FILENAME, _ASSET_PROMPT_SPEC),
    # 导演台段词（#262）：``director/plan.json`` 不在这条模式里，所以它不是产物。
    (DIRECTOR_DIRNAME, _DIRECTOR_SEGMENT_PROMPT_FILENAME,
     ArtifactSpec(DIRECTOR_SEGMENT_PROMPT_KIND)),
    # 导演台逐段简报（#263）：同一个 ``director/`` 目录，文本形态。
    (DIRECTOR_DIRNAME, _DIRECTOR_BRIEFING_FILENAME,
     ArtifactSpec(DIRECTOR_BRIEFING_KIND)),
    # 导演台按需词（#264）：首帧词与尾帧词各一种 kind，产物分区按 kind 分开读。
    (DIRECTOR_DIRNAME, _DIRECTOR_FIRST_FRAME_PROMPT_FILENAME,
     ArtifactSpec(DIRECTOR_FIRST_FRAME_PROMPT_KIND)),
    (DIRECTOR_DIRNAME, _DIRECTOR_TAILFRAME_PROMPT_FILENAME,
     ArtifactSpec(DIRECTOR_TAILFRAME_PROMPT_KIND)),
)


def segment_injection_card_name(position: int) -> str:
    """第 ``position`` 段（1 起）的段级注入卡产物名（与 CLI 落点同名同形）。

    写入口与用例都从这一处取名：「算不算产物」的模式与「写到哪个名字」各写一遍
    时，改一处就会让产物清单里没有刚写的卡。
    """
    return f"{SEGMENTS_DIRNAME}/injection-card-{position:02d}.md"


def artifact_spec(name: str) -> ArtifactSpec | None:
    """``name`` → 产物规格；不在登记范围内返回 ``None``。

    「算不算产物」「按文本还是媒体读」「清单从哪来」三个判断都走这一个函数，
    调用点不再各抄一份白名单字面量。
    """
    fixed = _FIXED_ARTIFACT_SPECS.get(name)
    if fixed is not None:
        return fixed
    directory, _, filename = name.rpartition("/")
    for dirname, filename_pattern, spec in _PATTERN_ARTIFACT_SPECS:
        if directory == dirname and re.fullmatch(filename_pattern, filename):
            return spec
    return None


def _text_spec(name: str) -> ArtifactSpec | None:
    """文本形态的规格；媒体产物与登记范围外的名字都返回 ``None``。"""
    spec = artifact_spec(name)
    return spec if spec is not None and spec.media_type is None else None


def _media_spec(name: str) -> ArtifactSpec | None:
    """媒体形态的规格；文本产物与登记范围外的名字都返回 ``None``。"""
    spec = artifact_spec(name)
    return spec if spec is not None and spec.media_type is not None else None


# 媒体表保留给路由的契约声明（``ARTIFACT_MEDIA_TYPES.values()``）：数据仍只有上面
# 那一张表，这里只是按形态筛一遍。
ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    name: spec.media_type for name, spec in _FIXED_ARTIFACT_SPECS.items()
    if spec.media_type is not None
}

# 文本内容读取的上限（spec §10）：1 MiB，超出回 413 artifact_too_large。
ARTIFACT_MAX_BYTES = 1024 * 1024

# ---------------------------------------------------------------------------
# 上传素材（D3-2，#222 §5）
# ---------------------------------------------------------------------------
# ``ASSETS_DIRNAME`` 在产物登记表那一节声明（它是资产图词的目录，也是素材落点）。
# 临时文件的固定前缀：崩溃残留靠它认出来（启动清理只删这一种）。
ASSET_TMP_PREFIX = ".tmp-"
# 上传的读块大小。它同时是「边写边判」的粒度：一次最多多读一块就能发现超限，
# 不会出现「先把 512 MiB 读完再判」。
ASSET_CHUNK_BYTES = 64 * 1024
# 嗅探窗口。spec §5.2 写的是「前 16 字节」——PNG / JPEG / WebP / WAV / MP3 / MP4 的
# 魔数在这个窗口里就够。EBML 不够：WebM 与 Matroska 共用同一个 4 字节头，判据是后面的
# DocType（``webm`` / ``matroska``，通常落在第 24–40 字节）。所以窗口取 64，
# 前 16 字节就能定的格式行为不变。
ASSET_SNIFF_BYTES = 64
ASSET_NAME_MAX_CHARS = 120

# 支持的类型与各自上限（#222 §5.2）。按类型分开是因为视频本来就比图片大两个数量级：
# 一个统一上限要么拦不住图，要么把短视频也卡死。
ASSET_KIND_LIMITS: dict[str, int] = {
    "image": 20 * 1024 * 1024,
    "video": 512 * 1024 * 1024,
    "audio": 64 * 1024 * 1024,
    # 导演包（D8 §9.2 / #263）：包里装的是媒体，上限与视频同档。
    "zip": 512 * 1024 * 1024,
}

# 落盘扩展名 → (kind, 规范化格式)。嗅探与目录扫描共用这一张表：两处各写一份
# 「哪些扩展名算图片/视频/音频」时，改一处就会让列表与上传各说各话。
# ``jpg``/``jpeg`` 是同一个格式的两个写法，落盘一律取 ``jpg``。
_ASSET_FORMATS: dict[str, tuple[str, str]] = {
    "png": ("image", "png"),
    "jpg": ("image", "jpeg"),
    "jpeg": ("image", "jpeg"),
    "webp": ("image", "webp"),
    "mp4": ("video", "mp4"),
    "webm": ("video", "webm"),
    "wav": ("audio", "wav"),
    "mp3": ("audio", "mp3"),
    # 导演包（D8 §9.2）：上传侧只用来收「最终包」，本票不真正解包业务包。
    "zip": ("zip", "zip"),
}

# 规范化格式 → Content-Type。媒体读取端点与它自己的契约共用这一张表：端点按磁盘上
# 的扩展名给 Content-Type，契约里列出的可选类型也从这里派生，两处不会各说各话。
_FORMAT_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "zip": "application/zip",
}
# 扩展名 → Content-Type：从 ``_ASSET_FORMATS`` **现算**，不给新扩展名留一条静默漏判
# 的路（加了扩展名却忘了媒体类型，这里当场 KeyError）。
ASSET_MEDIA_TYPES: dict[str, str] = {
    ext: _FORMAT_MEDIA_TYPES[fmt] for ext, (_kind, fmt) in _ASSET_FORMATS.items()
}

# ISO 基础媒体文件（MP4）的 major brand 白名单（``ftyp`` 盒第 8–12 字节）。
# 只认 ``ftyp`` 盒子不够：QuickTime（``qt  ``）、HEIC（``heic``/``mif1``）与 MP4 用
# 同一个盒子结构，前者不在 §5.2 白名单里、后者根本是图片——只判盒子会让它们以
# ``kind=video`` 落成 ``.mp4``（复审实测）。名单取常见封装器的输出。
_MP4_BRANDS = frozenset({
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso8", b"iso9",
    b"mp41", b"mp42", b"M4V ", b"M4A ", b"avc1", b"dash", b"MSNV", b"f4v ",
})

# 素材标识里的内容哈希前缀长度（#222 §5.4）。两条正则与 ``store_asset`` 截前缀都从
# 它派生——字面重抄三份的话，改宽度会静默漏改一处。
ASSET_HASH_CHARS = 16

# 素材文件名形如 ``<sha256 前 16 位>.<已知扩展名>``。``.tmp-*`` 不匹配这个形状；
# 边车 ``<hash>.json`` 形状上匹配，但扩展名不在 ``_ASSET_FORMATS`` 里，同样落选——
# 「残片/边车不出现在列表里」由这两条判据共同保证，不需要额外的排除清单。
_ASSET_NAME_RE = re.compile(
    rf"^(?P<hash>[0-9a-f]{{{ASSET_HASH_CHARS}}})\.(?P<ext>[a-z0-9]+)$")
# 素材标识的第二段：只有内容哈希前缀、没有扩展名（#222 §5.4）。扩展名是落盘形态，
# 不是标识的一部分——客户端拿列表里的 ``asset_id`` 就能拼出媒体 URL。
_ASSET_ID_RE = re.compile(rf"^[0-9a-f]{{{ASSET_HASH_CHARS}}}$")

_GEN_RE = re.compile(r"^GEN(\d+)$")
_IDEMPOTENCY_CAPACITY = 50


class WorkbenchError(Exception):
    """工作台领域错误；HTTP 层据此构造 problem+json。"""

    status_code = 500
    code = "workbench_error"


class SessionNotFound(WorkbenchError):
    status_code = 404
    code = "session_not_found"


class SessionLegacy(WorkbenchError):
    status_code = 409
    code = "session_legacy"


class SessionBusy(WorkbenchError):
    status_code = 409
    code = "session_busy"


class SessionCompleted(WorkbenchError):
    status_code = 409
    code = "session_completed"


class ActionItemPending(WorkbenchError):
    status_code = 409
    code = "action_item_pending"


class ActionItemNotFound(WorkbenchError):
    """待办不存在，或不属于 URL 里那个会话（两种情况不回不同码，避免泄漏他会话的 id）。"""

    status_code = 404
    code = "action_item_not_found"


class ActionItemResolved(WorkbenchError):
    status_code = 409
    code = "action_item_resolved"


class IdempotencyConflict(WorkbenchError):
    status_code = 409
    code = "idempotency_conflict"


class JobNotFound(WorkbenchError):
    status_code = 404
    code = "job_not_found"


class JobNotRunning(WorkbenchError):
    status_code = 409
    code = "job_not_running"


class ArtifactNotFound(WorkbenchError):
    """产物不存在、标识越界，或名字不在登记范围内。

    这几种情况不回不同码：越界与不存在若能区分，就等于泄漏登记根之外是否真有文件。
    """

    status_code = 404
    code = "artifact_not_found"


class ArtifactTooLarge(WorkbenchError):
    status_code = 413
    code = "artifact_too_large"


class UploadInvalid(WorkbenchError):
    """multipart 结构不对（不是表单、没有 ``file`` 字段、边界坏掉）。

    与 FastAPI 的 422 分开：422 说的是「字段值不合法」，这里说的是「请求根本不是
    一份能读的上传」——前端对这两种的处理不同（补字段 vs 改发送方式）。
    """

    status_code = 400
    code = "upload_invalid"


class UnsupportedMediaType(WorkbenchError):
    """魔数不在白名单（含「声明的扩展名与字节不符」）。

    判定只认字节（#222 §5.2）：用户文件名只作显示名。声明的扩展名若是**已知格式**却
    与嗅探结果不符，同样按 415 拒绝——文件名与内容互相矛盾时不猜哪一个是真的。
    """

    status_code = 415
    code = "unsupported_media_type"


class AssetTooLarge(WorkbenchError):
    status_code = 413
    code = "asset_too_large"


class AssetNotFound(WorkbenchError):
    """素材不存在、标识越界，或不是素材命名形态（#222 §8 / §10）。

    三种情况同码：能区分「越界」与「不存在」就等于泄漏登记根之外是否真有文件。
    """

    status_code = 404
    code = "asset_not_found"


class AssetLocked(WorkbenchError):
    """ref 会话起跑后不允许再增删参考素材（D7 spec §4.3 / §13）。

    比 §4.2 的「素材循环开始前」更严，按**起跑**（会话离开 ``created``）锁：素材
    循环一开始，后面每一步的资产图词都基于当时那份台账写，事后加删会让已写好的词
    与图对不上。base 会话不受这条约束——帧提交上传走同一个端点（D4）。
    """

    status_code = 409
    code = "asset_locked"


class AssetRequired(WorkbenchError):
    """ref 会话零 Picture 起跑（D7 spec §4.3 / §13）：停在 created 等人交素材。"""

    status_code = 409
    code = "asset_required"


class AssetNameInvalid(WorkbenchError):
    """ref 登记的语义名超长、会话内重复，或 base 会话带了语义名（D7 §4.2）。

    缺失**不**是触发条件：带 ``name`` 才算登记，不带就是普通素材（素材图与段视频
    回交走这条）。「必填」是前端上传框的界面规则，不是 API 规则。
    spec §13 的错误码表没给「语义名」单独一码（票面把这一条留给实现定），这里用
    ``asset_name_invalid``：它只描述「这个名字不能用」，与容量、类型、不存在分开。
    """

    status_code = 422
    code = "asset_name_invalid"


class AssetDuplicate(WorkbenchError):
    """同一份内容已经登记在台账里（D7 §4.2）。

    ref 不沿用 base 的「同内容返回同一份」：台账按语义名逐件计数，一份内容对应两条
    登记会让容量、编号与素材循环的件号同时说谎。
    """

    status_code = 422
    code = "asset_duplicate"


class RefCapacityExceeded(WorkbenchError):
    """参考素材超出官方 9/3/3/12 上限（D7 spec §8 / §13）。

    422 是同步闸门：上传时立刻拒，不留到跑起来才炸。超过哪一档由
    ``ref_capacity.check_reference_capacity`` 的文案点名，这里只换错误码——数字与
    文案都不在本模块再抄一份。
    """

    status_code = 422
    code = "ref_capacity_exceeded"


class RouteMismatch(WorkbenchError):
    """用一条路线的专属动作操作另一条路线的会话（D7 spec §13）。"""

    status_code = 409
    code = "route_mismatch"


class SegmentResubmitUnsupported(WorkbenchError):
    """base 会话请求「换一段」（D7 spec §5.5 / #258）。

    base 中途换段会断掉第 N+1 段的帧锚（ADR 0023:34-38），是结构性禁止而非产品选择，
    所以不开这条边——不是「暂不支持」，是这条路线上它永远不该发生。
    """

    status_code = 409
    code = "segment_resubmit_unsupported"


class SegmentNotFound(WorkbenchError):
    """会话里没有这个段号的执行段（#258 换段端点）。"""

    status_code = 404
    code = "segment_not_found"


class SegmentNotPassed(WorkbenchError):
    """只有 ``passed`` 的段能换一段（#258）：其它状态本来就在等重交或在校验中。"""

    status_code = 409
    code = "segment_not_passed"


class CacheOutOfRoot(WorkbenchError):
    """级联要删的路径解析后跑出了配置根（#265）。

    ``cascade_rerun`` 的越界判据（符号链接把段文件指到别处、缓存目录本身是链接）
    在接线层翻成这个稳定码：**一个文件都没删**是这条错误的语义，不能只当一句文案。
    """

    status_code = 400
    code = "cache_out_of_root"


class FrameInvalid(WorkbenchError):
    """帧槽引用无效（D4 spec §9 / §12）。

    与素材读取的 404 分开：这里回 422 且**不区分**「不存在」与「非本会话」——
    帧提交是写操作，回 404 会让人拿错误码去探测别人会话里有哪些素材。D7 §13 的
    ``asset_not_found`` 只覆盖 ref 素材循环（§6.3），不改这条已验收的路线契约。
    """

    status_code = 422
    code = "frame_invalid"


class SegmentVideoInvalid(WorkbenchError):
    """段视频回交无效（D6 spec §8 / §12）：段号不符、素材非本会话、非 mp4、技术档不合格。

    与 ``FrameInvalid`` 分开是一个**稳定错误码**（``segment_video_invalid``）：前端要把
    「本段视频不合格，重交一段」与「帧槽填错」这两种出路区分开。同码覆盖多种原因，
    具体原因进 ``detail`` 文案。
    """

    status_code = 422
    code = "segment_video_invalid"


class VideoUnreadable(WorkbenchError):
    """段视频读不出帧数（D8 §5.6 / #264）：容器坏了、不是能解码的视频。

    与 ``frame_mismatch`` 分开是**可证层的要求**：读不出是设备/文件问题，不是
    「贴错了段」。判 0 帧会把两者混成同一句话（``cache.actual_frames`` 的同一取舍）。
    """

    status_code = 422
    code = "video_unreadable"


class FrameMismatch(WorkbenchError):
    """帧数判不过（D8 §10 / #264）：这不是本段成片，或差得超出容差。

    判据只有 ``companion.judge_video_evidence`` 一份；这里只把结论翻成稳定错误码，
    文案原样带走（它已经点名该怎么做）。
    """

    status_code = 422
    code = "frame_mismatch"


class AssetImageInvalid(WorkbenchError):
    """素材图回交无效（D7 spec §6.3）：件号不符、素材非本会话、非图片、载荷错配。

    本会话里**不存在**的那一件不归这个码，按 §13 回 404 ``asset_not_found``。
    错误码 ``asset_image_invalid`` 是票面留给实现定的一格（spec §13 没单列）。它与
    ``frame_invalid`` 分开，是因为前端的出路不同：帧槽填错要重选槽位，素材图不合格
    要重新生成一张再交。
    """

    status_code = 422
    code = "asset_image_invalid"


class AspectMismatchUnconfirmed(WorkbenchError):
    """开场首帧比例不符且用户没有显式确认（D8 spec §7.2 / §15）。

    与上传时的 422 分开：用户可能就是要有意用这个比例（CLI 里也是「回车继续」），
    所以这里**收下**上传，只在决议这一步要求一个明确的 ``confirm_aspect_mismatch``。
    """

    status_code = 409
    code = "aspect_mismatch_unconfirmed"


class StartImageRequired(WorkbenchError):
    """导演台 i2v/fl2v 没有开场首帧就起跑（D8 spec §7.3 / §15）。

    i2v 的每一段都必须有源图（插件 ``gen_timeline.py:304-310``），缺了会在导入后
    直接抛 ``Segment #N has no source image``——所以闸门放在起跑之前，不是打包期。
    """

    status_code = 409
    code = "start_image_required"


class StorageUnavailable(WorkbenchError):
    """素材落盘失败（磁盘满、只读目录）。请求本身没错，是这台机器的存储不行。"""

    status_code = 507
    code = "storage_unavailable"


class ArchiveInvalid(WorkbenchError):
    """导演包（或任何 zip）不可信：缺件、包内路径越界、符号链接、不是合法 ZIP。

    D8 spec §9.2 把「绝对路径 / ``..`` / 符号链接」列为**一律拒绝**：包来自用户
    或外部工具，解包路径绝不能让包内文字决定。与「上传类型不对」分开一码，是因为
    前端的出路不同（换一份包 vs 换一种文件）。
    """

    status_code = 422
    code = "archive_invalid"


# ---------------------------------------------------------------------------
# 事件流（D2 spec §5.3 / §8，ADR 0032）
# ---------------------------------------------------------------------------
EVENTS_FILENAME = "events.jsonl"

# 十种闭合类型（spec §8）。**闭合**指写入侧：不在表里的类型一律拒绝，加字段可以、
# 改语义不行。新增类型要连 spec 与前端生成客户端一起改，不能在某条调用路径上偷偷加。
EVENT_TYPES: tuple[str, ...] = (
    "session.created",
    "job.started",
    "job.progress",
    "job.succeeded",
    "job.failed",
    "job.cancelled",
    "job.interrupted",
    "action_item.created",
    "action_item.resolved",
    "artifact.created",
)

# 进度事件的取值从步骤机常量派生：阶段与状态各有一份声明，这里重抄一遍就会在
# 步骤机改名时静默放过错值（本仓「守卫形态要从常量派生」）。
_PROGRESS_STAGES = frozenset(step_machine.STEPS)
_PROGRESS_STATUSES = frozenset({"started", "done"})
_SUCCEEDED_NEXT = frozenset(
    # ``running`` 自 D6 起合法：长片阶段 2 成功不落 completed，服务端立刻接上分段
    # 规划任务，会话仍是 running（事件类型不变，只是同一事件多一个合法去向）。
    {step_machine.STATUS_AWAITING_HUMAN, step_machine.STATUS_COMPLETED,
     step_machine.STATUS_RUNNING})
_DECISIONS = frozenset({
    step_machine.DECISION_APPROVE,
    step_machine.DECISION_REVISE,
    # 帧提交决议（D4 §9）：事件类型不变，只是同一事件多一个合法的 decision 取值。
    step_machine.DECISION_SUBMIT_FRAMES,
    # 段视频回交决议（D6 §8）：同上，只多一个取值。
    step_machine.DECISION_SUBMIT_VIDEO,
    # 素材图交回决议（D7 §6.3）：同上。
    step_machine.DECISION_SUBMIT_IMAGE,
    # 导演台开场首帧决议（D8 §5.6）：唯一显式声明的 decision 取值。
    step_machine.DECISION_SUBMIT_START_IMAGE,
    # 切镜段段首图决议（D8 §5.6 / #264）：逐段的图槽，同样显式声明。
    step_machine.DECISION_SUBMIT_SEGMENT_IMAGE,
})


class UnknownEventType(ValueError):
    """写入了一个不在闭合集合里的事件类型（编程错误，不是用户输入错误）。"""


class InvalidEventPayload(ValueError):
    """事件 payload 缺少该类型的必需字段，或取值不在声明的范围内。"""


def _require_str(payload: dict[str, Any], key: str) -> None:
    if not isinstance(payload.get(key), str):
        raise InvalidEventPayload(f"事件 payload 缺字符串字段 {key!r}：{payload!r}")


def _require_choice(payload: dict[str, Any], key: str, allowed: frozenset[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise InvalidEventPayload(
            f"事件 payload 的 {key!r} 必须是 {sorted(allowed)} 之一：{value!r}")


def _validate_progress(payload: dict[str, Any]) -> None:
    _require_choice(payload, "stage", _PROGRESS_STAGES)
    _require_str(payload, "node")
    _require_choice(payload, "status", _PROGRESS_STATUSES)
    elapsed = payload.get("elapsed_ms")
    if elapsed is not None and (type(elapsed) is not int or elapsed < 0):
        raise InvalidEventPayload(f"事件的 elapsed_ms 必须是非负整数：{elapsed!r}")


def _no_required_fields(_payload: dict[str, Any]) -> None:
    return None


def _validate_failed(payload: dict[str, Any]) -> None:
    _require_str(payload, "code")
    _require_str(payload, "detail")


# 待办载荷里「在等第几段 / 第几件 / 为什么被拒」的键（D7-1 / spec §14）：校验与重放
# 共用这一份。各抄一份字面量时，将来加键漏一处不会报错——重放读不到就等于没写，
# 恢复后的待办静默少一项（「守卫形态要从常量派生」）。
_ACTION_ITEM_INDEX_KEYS: tuple[str, ...] = ("segment_index", "asset_index")
_ACTION_ITEM_REASON_KEY = "reason"
# 一个待办收两件素材的槽位（D8 §7.1）：键与 ``ActionItemOut`` 同形，创建与决议两条
# 事件的校验、重放都从这一份常量派生——抄两份时加第三个槽位就会静默丢。
_ACTION_ITEM_ASSET_KEYS: tuple[str, ...] = ("start_asset_id", "end_asset_id")
_ACTION_ITEM_PAYLOAD_KEYS: tuple[str, ...] = (
    *_ACTION_ITEM_INDEX_KEYS, _ACTION_ITEM_REASON_KEY, *_ACTION_ITEM_ASSET_KEYS)


def _validate_action_item_created(payload: dict[str, Any]) -> None:
    _require_str(payload, "item_id")
    _require_str(payload, "kind")
    # 待办载荷（D7-1 / spec §14）：段号与素材件号是「在等第几段 / 第几件」的唯一
    # 依据，出现时必须是 1 起的整数（``type is int`` 挡掉 ``True == 1``）。不出现
    # 是允许的——旧会话的事件只有 item_id + kind，仍要能重放（AC4）。
    for key in _ACTION_ITEM_INDEX_KEYS:
        value = payload.get(key)
        if value is not None and (type(value) is not int or value < 1):
            raise InvalidEventPayload(
                f"事件 payload 的 {key!r} 必须是 ≥1 的整数：{value!r}")
    reason = payload.get(_ACTION_ITEM_REASON_KEY)
    if reason is not None and not isinstance(reason, str):
        raise InvalidEventPayload(
            f"事件 payload 的 {_ACTION_ITEM_REASON_KEY!r} 必须是字符串：{reason!r}")
    _validate_action_item_asset_keys(payload)


def _validate_action_item_asset_keys(payload: dict[str, Any]) -> None:
    """图槽标识出现时必须是字符串（D8 §7.1）：半截值会在读详情时炸。"""
    for key in _ACTION_ITEM_ASSET_KEYS:
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise InvalidEventPayload(
                f"事件 payload 的 {key!r} 必须是字符串：{value!r}")


def _validate_action_item_resolved(payload: dict[str, Any]) -> None:
    _require_str(payload, "item_id")
    _require_choice(payload, "decision", _DECISIONS)
    # 决议事件也可能带图槽（导演台开场首帧）：与创建事件同一份校验。
    _validate_action_item_asset_keys(payload)
    # 修订载荷是可选的加字段（D4 spec §7）：写了就必须是 [{layer, text}, …]，
    # 否则重放时 ``ActionItemOut`` 会拿半截字典去建模型并在**读详情**时报错。
    revisions = payload.get("revisions")
    if revisions is None:
        return
    if not isinstance(revisions, list):
        raise InvalidEventPayload(f"事件的 revisions 必须是列表：{revisions!r}")
    for entry in revisions:
        if not isinstance(entry, dict):
            raise InvalidEventPayload(f"修订条目必须是字典：{entry!r}")
        _require_str(entry, "layer")
        _require_str(entry, "text")


def _validate_artifact_created(payload: dict[str, Any]) -> None:
    _require_str(payload, "artifact_id")
    _require_str(payload, "kind")
    _require_str(payload, "name")


_PAYLOAD_RULES: dict[str, Callable[[dict[str, Any]], None]] = {
    "session.created": lambda payload: _require_str(payload, "status"),
    "job.started": lambda payload: _require_str(payload, "kind"),
    "job.progress": _validate_progress,
    "job.succeeded": lambda payload: _require_choice(
        payload, "next", _SUCCEEDED_NEXT),
    "job.failed": _validate_failed,
    "job.cancelled": lambda payload: _require_str(payload, "at_node"),
    "job.interrupted": _no_required_fields,
    "action_item.created": _validate_action_item_created,
    "action_item.resolved": _validate_action_item_resolved,
    "artifact.created": _validate_artifact_created,
}


@dataclass(frozen=True)
class EventDraft:
    """待写入的一条事件；``seq`` 由 :class:`EventLog` 在会话锁内分配。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None


@dataclass(frozen=True)
class Event:
    """已落盘的一条事件（包封见 spec §8）。"""

    seq: int
    type: str
    at: str
    session_id: str
    payload: dict[str, Any]
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "seq": self.seq,
            "type": self.type,
            "at": self.at,
            "session_id": self.session_id,
        }
        if self.job_id is not None:
            body["job_id"] = self.job_id
        body["payload"] = self.payload
        return body

    def sse_frame(self) -> str:
        """SSE 帧：``id`` / ``event`` / ``data`` 三行加一个空行（spec §8）。"""
        return (f"id: {self.seq}\nevent: {self.type}\n"
                f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n")


class EventLog:
    """``<会话目录>/events.jsonl`` 的只追加读写面。

    **序号分配必须在会话锁内**——``last_seq`` 与随后的写入之间若别人插进来，
    两条事件会拿到同一个号（D2-AC11）。本类自己不加锁：调用方是
    ``WorkbenchSessionStore``，它持有每会话一把 ``threading.Lock``。
    """

    def __init__(self, directory: str | Path, session_id: str = "") -> None:
        self.directory = Path(directory)
        self.session_id = session_id

    @property
    def path(self) -> Path:
        return self.directory / EVENTS_FILENAME

    def append(self, drafts: Sequence[EventDraft]) -> list[Event]:
        """按顺序追加一组事件并返回带 seq 的落盘结果；空列表不触碰文件。"""
        if not drafts:
            return []
        for draft in drafts:
            if draft.type not in EVENT_TYPES:
                raise UnknownEventType(
                    f"不是闭合集合里的事件类型：{draft.type!r}；"
                    f"可用类型：{', '.join(EVENT_TYPES)}")
            _PAYLOAD_RULES[draft.type](draft.payload)
        seq = self.last_seq()
        # 崩溃可能把最后一行写了一半：它从未落进快照，续写前先截掉，否则新事件会
        # 粘在残行后面，读回时整行作废（崩在这条事件上的序号被复用是正确的）。
        self._drop_torn_tail()
        at = datetime.now(timezone.utc).isoformat()
        events = [
            Event(seq=seq + offset, type=draft.type, at=at,
                  session_id=self.session_id, payload=dict(draft.payload),
                  job_id=draft.job_id)
            for offset, draft in enumerate(drafts, start=1)
        ]
        text = "".join(
            json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        return events

    def read(self, *, after_seq: int = 0) -> list[Event]:
        return [event for event in self._read_all() if event.seq > after_seq]

    def last_seq(self) -> int:
        events = self._read_all()
        return events[-1].seq if events else 0

    def _read_all(self) -> list[Event]:
        if not self.path.is_file():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("事件文件读不出来，按空流处理：%s（%s）", self.path, exc)
            return []
        events: list[Event] = []
        previous = 0
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                seq = item["seq"]
                if type(seq) is not int:
                    raise TypeError(f"seq 不是整数：{seq!r}")
                event = Event(
                    seq=seq,
                    type=str(item["type"]),
                    at=str(item["at"]),
                    session_id=str(item["session_id"]),
                    payload=dict(item.get("payload") or {}),
                    job_id=item.get("job_id"),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                # 半截行只可能来自「写事件」那一刻被强杀；它没有落进任何快照，
                # 跳过即可。真正的损坏（序号回退）在下面大声报错。
                logger.warning("事件文件第 %d 行无法解析，已跳过：%s（%s）",
                               number, self.path, exc)
                continue
            if event.seq <= previous:
                raise ValueError(
                    f"事件序号不单调：{self.path} 第 {number} 行 seq={event.seq}，"
                    f"上一行是 {previous}")
            previous = event.seq
            events.append(event)
        return events

    def _drop_torn_tail(self) -> None:
        if not self.path.is_file():
            return
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        with self.path.open("r+b") as handle:
            handle.truncate(data.rfind(b"\n") + 1)


def _replay_event_job(jobs: list[dict[str, Any]], event: Event,
                      updates: dict[str, Any]) -> None:
    """把一条 job 事件的结论并回快照的 job 摘要（按 ``job_id`` 覆盖）。"""
    if event.job_id is None:
        return
    for item in jobs:
        if item.get("job_id") == event.job_id:
            item.update(updates)
            return
    jobs.append({
        "job_id": event.job_id, "kind": "advance", "status": "",
        "started_at": "", "finished_at": "", "error_code": None,
        "cancelled_at_node": None, "cancel_requested": False, **updates,
    })


def _replay_interrupted(jobs: list[dict[str, Any]], event: Event) -> None:
    """``job.interrupted`` 的 payload 是空的：命中的任务由事件上的 ``job_id`` 指名，
    没有 ``job_id``（启动对账不认识具体任务）时按「所有 running」收。"""
    for item in jobs:
        if event.job_id is not None and item.get("job_id") != event.job_id:
            continue
        if item.get("status") == "running":
            item.update({"status": step_machine.STATUS_INTERRUPTED,
                         "finished_at": event.at})


def _replay_snapshot(
    session: SessionState, events: Sequence[Event],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """由事件流推出快照里被它们覆盖的字段（spec §5.3 的「重放补齐」）。

    只重放**快照真有对应字段**的事件：``job.progress`` 只代表「还在跑」，不写任何
    摘要；``artifact.created`` 是空操作——产物清单由 ``_artifacts`` 现扫目录得到，
    多记一份就是同一件事两处口径。

    幂等靠两点：待办按 ``item_id`` 去重、其余字段一律**绝对赋值**而非累加。同一批
    事件重放两次与一次的结果相同，所以「重复加载」不会长出第二条待办。
    """
    status = session.status
    jobs = [dict(item) for item in session.jobs]
    action_items = [dict(item) for item in session.action_items]
    for event in events:
        payload = event.payload
        if event.type == "session.created":
            status = str(payload["status"])
        elif event.type == "job.started":
            status = step_machine.STATUS_RUNNING
            # kind 也要跟着事件走：``pack`` 与 ``segment`` 混在默认值 ``advance`` 里，
            # 快照丢失后重放出来的任务列表就分不清「跑了三分钟那条是什么」（#263）。
            started: dict[str, Any] = {"status": step_machine.STATUS_RUNNING,
                                       "started_at": event.at}
            if isinstance(payload.get("kind"), str):
                started["kind"] = payload["kind"]
            _replay_event_job(jobs, event, started)
        elif event.type == "job.progress":
            status = step_machine.STATUS_RUNNING
        elif event.type == "job.succeeded":
            status = str(payload["next"])
            _replay_event_job(jobs, event, {"status": "succeeded",
                                            "finished_at": event.at})
        elif event.type == "job.failed":
            status = step_machine.STATUS_FAILED
            _replay_event_job(jobs, event, {"status": "failed",
                                            "finished_at": event.at,
                                            "error_code": payload["code"]})
        elif event.type == "job.cancelled":
            status = step_machine.STATUS_CANCELLED
            _replay_event_job(jobs, event, {
                "status": "cancelled", "finished_at": event.at,
                "cancelled_at_node": payload["at_node"] or None,
                "cancel_requested": True})
        elif event.type == "job.interrupted":
            status = step_machine.STATUS_INTERRUPTED
            _replay_interrupted(jobs, event)
        elif event.type == "action_item.created":
            if not any(item.get("item_id") == payload["item_id"]
                       for item in action_items):
                created: dict[str, Any] = {
                    "item_id": payload["item_id"], "kind": payload["kind"],
                    "status": "pending", "created_at": event.at,
                    "resolved_at": "", "decision": None,
                }
                # 决议所需的载荷随事件走（D7-1 / spec §14）：段号/件号/被拒原因在
                # 快照补齐后仍要说得出「在等第几段 / 第几件」。旧会话没有这些键，
                # 保持缺省而不是补一个看似有值的零。键表与校验共用一份常量。
                for key in _ACTION_ITEM_PAYLOAD_KEYS:
                    if key in payload:
                        created[key] = payload[key]
                action_items.append(created)
        elif event.type == "action_item.resolved":
            revisions = payload.get("revisions")
            for item in action_items:
                if item.get("item_id") == payload["item_id"]:
                    item.update({"status": "resolved",
                                 "decision": payload["decision"],
                                 "resolved_at": event.at,
                                 # 修订历史只从这条事件补（D4 spec §7）：快照落后时
                                 # 也要能回显用户当时说了什么。
                                 "revisions": (
                                     [dict(entry) for entry in revisions]
                                     if isinstance(revisions, list) else [])})
                    # 图槽标识（D8 §7.1）：一个待办收两张图，重放要把两个槽都补回来。
                    for key in _ACTION_ITEM_ASSET_KEYS:
                        if isinstance(payload.get(key), str):
                            item[key] = payload[key]
        # artifact.created：产物清单从目录现扫（_artifacts），没有快照字段要补。
    return status, jobs, action_items


class RefOut(BaseModel):
    """素材台账摘要（D7 spec §12）：语义名 / 官方三型 / 同类编号 / 素材标识。

    这里回显的是**台账**（会话状态），不是上传目录的现扫结果；两者语义不同
    （``SessionDetail.assets`` 仍是目录现扫）。``asset_id`` 用来把摘要接到缩略图与
    媒体读取上。**没有本地路径字段**：工作台的素材只能走上传接口（spec §4.1）。
    """

    name: str
    kind: str
    number: int
    asset_id: str


class VideoOutputOut(BaseModel):
    ratio: str
    megapixels: float
    multiple: int


class BriefOut(BaseModel):
    mode: str
    variant: str
    director_mode: str = ""
    # 导演台三个打包参数（D8 §4.2）：创建时问齐、落 Brief，详情页据此回显。空串＝
    # 不是导演台路线（非 director 带它们会在创建口被 422 拒掉）。
    director_node_id: str = ""
    director_cache_name: str = ""
    director_continuity_mode: str = ""
    duration: float
    style: str
    language: str
    plot: str
    clarifications: str = ""
    draft: str = ""
    canvases: dict[str, list[int]] = Field(default_factory=dict)
    video_output: VideoOutputOut | None = None
    refs: list[RefOut] = Field(default_factory=list)


class JobSummary(BaseModel):
    job_id: str
    kind: str = "advance"
    status: str
    started_at: str = ""
    finished_at: str = ""
    error_code: str | None = None
    cancelled_at_node: str | None = None
    # 取消是协作式的：请求后只打标记，job 要等当前节点跑完才进 cancelled。
    cancel_requested: bool = False
    # 给人看的结果/失败说明（#263）：打包 job 在这里带装配结论与节点该设的宽高，
    # 失败的 job 带已点名约束与段的那句话。其余任务留空——它不是事件流的替代品。
    detail: str = ""


class RevisionOut(BaseModel):
    """一条用户修订的对外回显：内部四档层次 + 意见原文（D4 spec §7）。

    历史从**待办列表**重建（决议事件带着当时的载荷），与 ``stage_state.user_revisions``
    是两本账：前者是「用户在这张闸门上说过什么」，后者带轮次与落地标记、只给重跑用。
    """

    layer: str
    text: str


class ActionItemOut(BaseModel):
    item_id: str
    kind: str
    status: str
    created_at: str = ""
    resolved_at: str = ""
    decision: str | None = None
    # 决议时提交的「层次 + 意见」，按提交顺序原样回显；未决议与 approve 是空表。
    revisions: list[RevisionOut] = Field(default_factory=list)
    # 段视频待办的段号（1 起，与 segments[].index 同号，D6 §8「载荷带段号」）。其它
    # 待办类型为 None。resolve 用它判「段号与当前段是否一致」。
    segment_index: int | None = None
    # 素材循环待办的件号（1 起，与素材台账同号，D7 §6.3）。其它待办类型为 None。
    asset_index: int | None = None
    # 导演台开场首帧待办交回的两张素材（D8 §7.1）：**一个待办两个图槽**，首帧必填、
    # 全片尾帧可空。只记素材标识（与 ``asset_id`` 同形），不记路径。用 ``None`` 缺省
    # 而不是空串：这两个槽只属于一种待办，其余 kind 的待办**没有**它们；顺带让这个
    # 新增字段在契约里保持可选，存量客户端与夹具不必为无关 kind 补两个空键。
    start_asset_id: str | None = None
    end_asset_id: str | None = None
    # 被拒原因/校对差异等给人看的文本：段循环被拒、素材校对不通过时填（D7 §6.3）。
    reason: str = ""


class SegmentAssetOut(BaseModel):
    """本段实际要喂进 ComfyUI 的那几张参考图（D7 §10 / #258）。

    ``number`` 是**段内重编号**后的 Picture 号（从 1 连续，段词正文里引用的就是它），
    ``original_number`` 是整片台账原号（溯源用）。界面**只读**：段内子集由系统算，
    没有任何手改入口——逐段手填等于把闭包算法搬到前端。
    """

    number: int
    name: str
    original_number: int = 0


class SegmentOut(BaseModel):
    """长片的一个执行段（D6 spec §11）：规划完成后进会话快照，详情一次取到。

    ``index`` 是**1 起**的执行段号（与 ``segments/shot-NN.md`` 同号）；段状态在
    快照里，不靠 job 历史表达（``JOB_HISTORY_LIMIT`` 对 15 段偏紧）。
    """

    index: int
    start_s: int
    end_s: int
    # 覆盖的镜头号（1 起，与分镜表的 ``[Shot N]`` 同号）。
    shots_in_segment: list[int] = Field(default_factory=list)
    summary: str = ""
    end_hook: str = ""
    status: str = SEGMENT_STATUS_PENDING
    # 本段提示词产物与已回交视频素材：D6-2 起才会被填上。
    prompt_artifact_id: str = ""
    video_asset_id: str = ""
    # 语义闸门拒绝时给用户看的**具体**原因（D6 §5.4）；重交或通过后清空。
    rejection_reason: str = ""
    # 段内参考素材子集（D7 §10 / #258）：只有 ref 有，base 是空表；只读回显。
    assets: list[SegmentAssetOut] = Field(default_factory=list)
    # 导演台的帧执行契约（D8 §6.1）：**只加可选字段**，不另开第二套段模型。
    # base/ref 全部留空，前端按 ``segment_type`` 有没有值决定渲不渲染这一行。
    # ``start_frame`` / ``end_frame`` 是成片坐标（累计导出帧），``start_s`` /
    # ``end_s`` 仍是它们的向下截断——两套坐标同时给出，读的人不用自己除 24。
    start_frame: int | None = None
    end_frame: int | None = None
    declared_frames: int | None = None
    export_frames: int | None = None
    segment_type: str | None = None
    guided: bool | None = None
    give_images: list[str] = Field(default_factory=list)


class ArtifactInfo(BaseModel):
    artifact_id: str
    kind: str
    name: str
    bytes: int
    created_at: str


@dataclass(frozen=True)
class MediaResource:
    """一份可读字节的磁盘文件及其缓存标识（#222 §6.1）。

    ``etag`` 由各来源自己给：素材的文件名**就是**内容哈希，成片则用「mtime+大小」
    ——为一次请求去哈希几百 MiB 的成片是拿磁盘换一个不会变的字符串。
    """

    path: Path
    media_type: str
    size: int
    mtime: float
    etag: str
    # 显示名（进 ``Content-Disposition``）；素材取边车里的名字，产物就是文件名。
    filename: str = ""


class Asset(BaseModel):
    """上传素材的对外形状（#222 §9）。``asset_id`` 与 ``artifact_id`` 同形：
    ``<session_id>/<sha256 前 16 位>``——路径参数按 ``{asset_id:path}`` 承接。

    ``number`` 只有 ref 上传（台账登记）才有值（D7 §12「返回编号与类型」）；base
    上传与目录现扫（列表/详情）为 ``None``。``kind`` 始终是**嗅探类型**
    （image/video/audio），台账里的官方三型（picture/video/audio）在 ``brief.refs``。
    """

    asset_id: str
    kind: str
    name: str
    bytes: int
    created_at: str
    number: int | None = None


class SessionSummary(BaseModel):
    session_id: str
    status: SessionStatus
    legacy: bool
    revision: int
    created_at: str
    updated_at: str
    plot: str
    mode: str
    variant: str


class SessionDetail(SessionSummary):
    brief: BriefOut
    last_event_seq: int
    current_job: JobSummary | None = None
    jobs: list[JobSummary] = Field(default_factory=list)
    action_items: list[ActionItemOut] = Field(default_factory=list)
    artifacts: list[ArtifactInfo] = Field(default_factory=list)
    # 素材是目录现扫的结果，与产物同款（#222 §5.3）：不进快照、不占会话状态写入。
    assets: list[Asset] = Field(default_factory=list)
    # 长片的段列表（D6 spec §11）：来自快照字段，≤10 秒的会话是空表。
    segments: list[SegmentOut] = Field(default_factory=list)
    # 导演台的总长落位说明（D8 §6.1 / #262）：目标帧不可达时写「目标 / 实际 / 差」。
    # 由 ``director/plan.json`` 现读派生，不是会话状态里的一份副本（ADR 0025 的说明
    # 不落盘——读回的表不该带上一轮的措辞）；非导演台会话恒为空表。
    director_notes: list[str] = Field(default_factory=list)
    # 快照 state 的内部副本（D6 §5.4）：只给 ``mutate`` 的 mutator 在会话锁内读，
    # 让「读 state → 改 → 写回」不再绕开单写入者。用 PrivateAttr 而不是字段：
    # 它不进 JSON Schema、不跨 API 边界（D4 §7 的裁定，mutation
    # ``session_detail_leaks_stage_state`` 守着）。
    _stage_state: dict[str, Any] = PrivateAttr(default_factory=dict)

    def stage_state_snapshot(self) -> dict[str, Any]:
        """mutator 内读原始 state 的唯一入口；返回副本，改它不会写回。"""
        return dict(self._stage_state)

    # 素材台账的内部副本（D7 §6.4 同款理由）：素材循环的「在等第几件」只从台账推，
    # 而 mutator 拿到的是详情、不是会话文件。做成内部副本后，收尾写台账不必在锁内
    # 再读一次文件。同样用 PrivateAttr：台账不进 JSON Schema、不跨 API 边界
    # （对外只有 ``brief.refs`` 那份摘要）。
    _asset_ledger: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    def asset_ledger_snapshot(self) -> list[dict[str, Any]]:
        """mutator 内读台账的唯一入口；返回逐条副本，改它不会写回。"""
        return [dict(entry) for entry in self._asset_ledger]


class SessionPage(BaseModel):
    items: list[SessionSummary]
    next_cursor: str | None = None


class IdempotencyRegistry:
    """``key → 响应`` 的最近 N 组登记表（spec §6）。

    每会话一份（``<会话目录>/.idempotency.json``），建会话用根目录下的一份。
    FIFO 淘汰：文件里仍保留插入顺序，超过容量就丢最老的键。
    """

    def __init__(self, path: Path, capacity: int = _IDEMPOTENCY_CAPACITY) -> None:
        self.path = Path(path)
        self.capacity = capacity

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 登记表坏了不等于请求该失败；但也不能静默——丢掉它意味着幂等重发会
            # 重复建目录，日志要留下线索。
            logger.warning("幂等登记表无法读取，按空表处理：%s（%s）", self.path, exc)
            return {}
        entries = raw.get("entries") if isinstance(raw, dict) else None
        return dict(entries) if isinstance(entries, dict) else {}

    def replay(self, key: str, *, fingerprint: str) -> dict[str, Any] | None:
        entry = self._read().get(key)
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise IdempotencyConflict(
                f"Idempotency-Key {key!r} 已用于另一份请求体；换一个键或原样重发。")
        response = entry.get("response")
        return response if isinstance(response, dict) else None

    def record(self, key: str, *, fingerprint: str, response: dict[str, Any]) -> None:
        entries = self._read()
        entries[key] = {
            "fingerprint": fingerprint,
            "response": response,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        while len(entries) > self.capacity:
            entries.pop(next(iter(entries)))
        atomic_write_text(
            self.path,
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
        )


def _status_for(raw_status: str, *, legacy: bool) -> SessionStatus:
    if not legacy:
        if raw_status not in SESSION_STATUSES:
            raise ValueError(f"工作台会话的状态非法：{raw_status!r}")
        return cast(SessionStatus, raw_status)
    return cast(SessionStatus, _LEGACY_STATUS.get(raw_status, "interrupted"))


def _ledger_order(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """台账的对外顺序：图 → 视频 → 音频，同类按编号升序（spec §4.2「按编号升序」）。

    顺序在两个地方用同一份判据（详情回显与上传后的重排），各写一遍时改一处就会让
    列表顺序与编号顺序各说各话。
    """
    return sorted(
        entries,
        key=lambda entry: (KINDS.index(str(entry["kind"])), int(entry["number"]),
                           str(entry.get("asset_id", ""))))


def _renumber_ledger(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同类编号重排成从 1 起的连续序号，返回新列表（不就地改入参）。

    为什么删除后要重排：spec §4.2 的编号语义是「同类内第几件」，删掉第 2 件后留着
    空洞会让下一件与既有件撞号（max+1），素材循环的「第 N 件」也就与编号脱钩。
    台账只在起跑前可改，重排不会推翻任何已经写好的词。
    """
    renumbered = [dict(entry) for entry in _ledger_order(entries)]
    counters = {kind: 0 for kind in KINDS}
    for entry in renumbered:
        counters[str(entry["kind"])] += 1
        entry["number"] = counters[str(entry["kind"])]
    return renumbered


def _check_ref_capacity(entries: Sequence[dict[str, Any]], *, where: str) -> None:
    """按台账算 9 图 / 3 视频 / 3 音频 / 12 文件；超限换成工作台错误码。

    数字与「超了哪一档」的文案都来自 ``ref_capacity``（外部契约只住那一处），这里
    只做词表翻译与错误码包装。
    """
    items = [
        ReferenceItem(kind=str(entry["kind"]), number=int(entry["number"]),
                      name=str(entry["name"]))
        for entry in entries
    ]
    try:
        check_reference_capacity(items, where=where)
    except ReferenceCapacityError as exc:
        raise RefCapacityExceeded(str(exc)) from exc


# 台账条目的初始状态（D7 §6.4）。素材循环（#256）会把轮次与「已定稿」写进同一条目：
# ``registered`` = 已登记、还没出词或上一轮被拒；``passed`` = 校对通过、该件定稿。
ASSET_LEDGER_STATUS_REGISTERED = "registered"
ASSET_LEDGER_STATUS_PASSED = "passed"

# 素材循环把「用户刚交的那张图」暂存在台账条目上，而不是 stage_state：会话文件是唯一
# 真源（ADR 0032），而 ``stage_state`` 只保存白名单里的键——写进去会在下次保存时被
# 静默丢掉。它也是崩溃恢复的唯一依据：本键在＝这张还没校对，重启后重跑校对而不是
# 重新出词（用户不必重交）。校对落定（通过或被拒）即清掉它。
ASSET_PROOFREAD_KEY = "proofread_asset_id"


def _ledger_entry(name: str, asset_kind: str, number: int, asset_id: str,
                  sha256: str) -> dict[str, Any]:
    """一条素材台账记录（D7 §6.4 的七字段）。

    ``kind`` 落**官方三型**（picture / video / audio）而不是嗅探结果的
    image/video/audio：台账是参考素材的账，``<Picture N>`` 与分镜表标签都按这套词表
    说话；翻译只在 ``ASSET_KIND_TO_REFERENCE`` 一处做。
    """
    return {
        "name": name,
        "kind": ASSET_KIND_TO_REFERENCE[asset_kind],
        "number": number,
        "asset_id": asset_id,
        "sha256": sha256,
        "round": 0,
        "status": ASSET_LEDGER_STATUS_REGISTERED,
    }


def _next_number(ledger: Sequence[dict[str, Any]], asset_kind: str) -> int:
    """同类下一件的编号：取现有最大值 + 1（删除后 ``_renumber_ledger`` 会把它压回连续）。"""
    kind = ASSET_KIND_TO_REFERENCE[asset_kind]
    return max((int(entry["number"]) for entry in ledger if entry["kind"] == kind),
               default=0) + 1


def _validate_semantic_name(name: str) -> None:
    """ref 登记的语义名：只限长度。

    「必填」不在这一层：带 ``name`` 才是登记，不带就是不记账的普通素材（#256/#258
    回交通道）。前端「上传参考素材」框的必填校验是界面规则，不是 API 规则。
    """
    if len(name) > ASSET_NAME_MAX_CHARS:
        raise AssetNameInvalid(
            f"语义名最多 {ASSET_NAME_MAX_CHARS} 个字符（收到 {len(name)} 个）。")


def _brief_out(brief: Brief,
               ledger: Sequence[dict[str, Any]] = ()) -> BriefOut:
    return BriefOut(
        mode=brief.mode,
        variant=brief.variant,
        director_mode=brief.director_mode,
        director_node_id=brief.director_node_id,
        director_cache_name=brief.director_cache_name,
        director_continuity_mode=brief.director_continuity_mode,
        duration=brief.duration,
        style=brief.style,
        language=brief.language,
        plot=brief.plot,
        clarifications=brief.clarifications,
        draft=brief.draft,
        canvases={family: [width, height]
                  for family, (width, height) in brief.canvases.items()},
        video_output=(
            VideoOutputOut(
                ratio=brief.video_output.ratio,
                megapixels=brief.video_output.megapixels,
                multiple=brief.video_output.multiple,
            )
            if brief.video_output is not None else None
        ),
        # brief.refs 属于 CLI 文件流程（带本地路径的那套登记），工作台的参考素材
        # 只能从台账来（spec §4.1）：这里统一按台账回显，不让两套登记混进同一个字段。
        refs=[RefOut(name=str(entry["name"]), kind=str(entry["kind"]),
                     number=int(entry["number"]), asset_id=str(entry["asset_id"]))
              for entry in _ledger_order(ledger)],
    )


def _director_notes(directory: Path, duration: float) -> list[str]:
    """导演台总长落位说明（#262）：目标帧与段表实际落位不一致时点名差几帧。

    读的是盘上的 ``director/plan.json``，与续跑/写段同一个严格读回口；坏表在这里
    只降级成「没有说明」（详情端点不该因一张坏表 500，真正的拦阻由执行器做）。
    目标帧从 brief 时长现算——``plan.json`` 的 ``total_frames`` 是**落位后**的实际值
    （ADR 0025），不是目标。
    """
    from .director.frames import FPS, length_note
    from .director.planning import plan_path, read_plan_json

    path = plan_path(directory)
    if not path.is_file():
        return []
    try:
        plan = read_plan_json(path)
    except ValueError:
        return []
    target = round(float(duration) * FPS)
    # 文案与规划器同一份（``director.frames.length_note``）：这里只数无引导段数。
    note = length_note(
        target, plan.total_frames,
        sum(1 for segment in plan.segments if not segment.guided))
    return [note] if note else []


def _artifact_info(session_id: str, name: str, path: Path) -> ArtifactInfo:
    spec = artifact_spec(name)
    if spec is None:
        raise ArtifactNotFound(f"不是登记范围内的产物名：{name!r}")
    stat = path.stat()
    return ArtifactInfo(
        artifact_id=f"{session_id}/{name}",
        kind=spec.kind,
        name=name,
        bytes=stat.st_size,
        created_at=datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc).isoformat(),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _split_artifact_id(artifact_id: str) -> tuple[str, str]:
    """``<会话标识>/<产物名>`` → 两段；按登记表**最长合法后缀**切分。

    产物名可以带斜杠（``segments/shot-01.md``），会话标识本身也可以
    （``<主题>/GEN00N``），所以不能按最后一个斜杠切：从最靠左的斜杠试起，第一个
    能让后半段成为合法产物名的位置就是切点（此时后缀最长）。越界与不存在的判定
    在各自的 ``_resolve`` / ``_artifact_path`` 里，不在这里替它们下结论。
    """
    for index, char in enumerate(artifact_id):
        if char != "/":
            continue
        session_id, name = artifact_id[:index], artifact_id[index + 1:]
        if session_id and artifact_spec(name) is not None:
            return session_id, name
    raise ArtifactNotFound(f"产物标识必须形如 <会话标识>/<产物名>：{artifact_id}")


def _unlink_quietly(path: Path) -> None:
    """删临时文件；已经不在就当成功。清理路径不该因为「本来就没有」再失败一次。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除素材临时文件失败：%s（%s）", path, exc)


def _candidate_artifact_names(directory: Path) -> list[str]:
    """会话目录下**名字合法**的产物名（相对路径形态）：固定名 + 模式目录现扫。

    模式目录取自 ``_PATTERN_ARTIFACT_SPECS``，与解析函数同一份数据——目录或模式
    写死在两处时，改一处就会让清单与读取各说各话。扫描到的候选名仍要过
    ``artifact_spec``：``segments/plan.json`` 这种同目录下的文件不算产物。
    """
    names = set(_FIXED_ARTIFACT_SPECS)
    for dirname, _filename_pattern, _spec in _PATTERN_ARTIFACT_SPECS:
        sub = directory / dirname
        if not sub.is_dir():
            continue
        for entry in sub.rglob("*"):
            name = entry.relative_to(directory).as_posix()
            if artifact_spec(name) is not None:
                names.add(name)
    return sorted(names)


def _artifacts(session_id: str, directory: Path, root: Path) -> list[ArtifactInfo]:
    """会话目录下登记范围内的产物元数据；不读内容（内容读取归 #220）。

    解析后不在登记根之下的条目不出现：名字合法不代表落点在根内（符号链接）。
    """
    found: list[ArtifactInfo] = []
    for name in _candidate_artifact_names(directory):
        path = (directory / name).resolve()
        if not path.is_file() or not _is_within(path, root):
            continue
        found.append(_artifact_info(session_id, name, path))
    return found


def _is_webm(head: bytes) -> bool:
    """EBML 容器里只接受 WebM：DocType 元素（ID ``0x4282``）的值必须是 ``webm``。

    Matroska（``matroska``）与 WebM 共用同一个 4 字节 EBML 头。只认头会把 ``.mkv``
    当视频收进来，而它不在 §5.2 的白名单里（复审实测）。
    """
    marker = head.find(b"\x42\x82")
    if marker < 0:
        return False
    return b"webm" in head[marker:marker + 16]


def _sniff_asset(head: bytes) -> tuple[str, str] | None:
    """按前若干字节判定 ``(kind, 落盘扩展名)``；认不出返回 None。

    只认魔数、不解码（#222 §5.2）：魔数对但内容损坏的文件放行，交给下游报错。
    RIFF 同时是 WebP 与 WAV 的容器头，``ftyp`` 同时被 MP4 / MOV / HEIC 使用，
    EBML 同时被 WebM / Matroska 使用——这三处都必须再往后看一层才算定案。
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpg"
    if head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image", "webp"
        if head[8:12] == b"WAVE":
            return "audio", "wav"
        return None
    if head[4:8] == b"ftyp":
        if head[8:12] in _MP4_BRANDS:
            return "video", "mp4"
        return None
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ("video", "webm") if _is_webm(head) else None
    # MP3 有两种开头：ID3v2 标签，或直接是帧同步（``0xFF`` + 高三位全 1）。
    # JPEG 的 ``0xFF 0xD8`` 不满足后半条（``0xD8 & 0xE0 != 0xE0``），不会撞车。
    if head.startswith(b"ID3"):
        return "audio", "mp3"
    # ZIP（导演包）：普通条目 ``PK\x03\x04``，空包是 ``PK\x05\x06``（只有中央目录
    # 结束记录）。两种都认，否则空包会被当成「认不出类型」拒掉。
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip", "zip"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio", "mp3"
    return None


def _archive_member_problem(info: zipfile.ZipInfo) -> str | None:
    """包内条目名的越界判据；合法返回 ``None``，否则给一句给人看的原因。

    只认 **POSIX 形态**：ZIP 规范用 ``/``；反斜杠在 Windows 上会被解释成分隔符，
    同一份包因此在两端解到不同的位置——一律拒绝，不替它归一。盘符（``C:``）在
    ``PurePosixPath`` 眼里不是「绝对路径」，必须单独判。
    """
    name = str(info.filename or "")
    if not name:
        return "空文件名"
    if "\\" in name:
        return "包含反斜杠"
    parts = name.split("/")
    if name.startswith("/"):
        return "是绝对路径"
    if re.match(r"^[A-Za-z]:", parts[0]):
        return "带盘符（绝对路径）"
    if ".." in parts:
        return "包含 .."
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        return "是符号链接"
    return None


def extract_archive_zip(
    zip_path: str | Path, destination: str | Path,
) -> list[str]:
    """把 zip 安全解到 ``destination``，返回写出的相对路径（POSIX 形态）。

    **不信任包内路径**（D8 spec §9.2 / #263）：绝对路径、``..``、符号链接任一出现
    就整份拒绝，一个字节都不落盘。调用方拿到的异常带稳定码 ``archive_invalid``。

    #266 的最终包比对会复用这一份：两处各写一遍解包，安全判据就会在某一次重构后
    只留在其中一条路径上。
    """
    archive_path = Path(zip_path)
    target_root = Path(destination)
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveInvalid(f"不是可读的 ZIP 包：{archive_path}（{exc}）") from exc
    written: list[str] = []
    with archive:
        for info in archive.infolist():
            problem = _archive_member_problem(info)
            if problem is not None:
                raise ArchiveInvalid(
                    f"包内条目不可信：{info.filename!r} {problem}——"
                    "导演包只接受包内相对路径（ASCII 文件名），"
                    "绝对路径、.. 与符号链接一律拒绝")
            if info.is_dir() or info.filename.endswith("/"):
                continue
            relative = info.filename.rstrip("/")
            target = target_root / relative
            resolved = target.resolve()
            root_resolved = target_root.resolve()
            if root_resolved != resolved and root_resolved not in resolved.parents:
                # 名字层面已拦住越界；这里再按**真实落点**兜一次（防御性，不是主判据）。
                raise ArchiveInvalid(
                    f"包内条目 {relative!r} 解出的位置越出目标目录")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(ASSET_CHUNK_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk)
            written.append(relative)
    return written


def _claimed_extension(filename: str) -> str:
    """取用户文件名声明的扩展名（小写、不含点）；取不出返回空串。"""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    _, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 8:
        return ""
    return ext.lower()


def _check_declared_extension(filename: str, ext: str) -> None:
    """声明的扩展名与嗅探结果矛盾时按 415 拒绝（#222 §5.2 / §14）。

    为什么不是「以字节为准、悄悄改扩展名」：扩展名是发送方的声明，字节是事实；
    两者矛盾说明这份上传的来源本身不可信，替它挑一个不报错，等于把矛盾藏起来。
    声明里没有扩展名（或扩展名不在已知表里）时不做这层判定——那不算矛盾。
    """
    claimed = _claimed_extension(filename)
    known = _ASSET_FORMATS.get(claimed)
    if known is None:
        return
    if known[0] != _ASSET_FORMATS[ext][0] or known[1] != _ASSET_FORMATS[ext][1]:
        raise UnsupportedMediaType(
            f"文件内容的实际格式（{ext}）与文件名声明的扩展名（{claimed}）不符。")


def _display_name(filename: str, fallback: str) -> str:
    """显示名：去掉路径分隔符与控制字符，超长保留尾部（保住扩展名）。

    它**永不参与路径构造**（#222 §5.2）：落盘名永远是内容哈希加嗅探出的扩展名，
    所以这里清洗不干净也不会影响落点，只影响用户看到的名字。
    """
    cleaned = "".join(
        ch for ch in filename
        if ch not in '\\/:*?"<>|' and ord(ch) >= 32 and ch != "\x7f")
    cleaned = cleaned.strip()
    if not cleaned:
        return fallback
    return cleaned[-ASSET_NAME_MAX_CHARS:]


def _read_asset_sidecar(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 边车坏了不影响正本可用：按最小形态退回（显示名由哈希现拼）。
        logger.warning("素材边车无法读取，按最小形态处理：%s（%s）", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def _asset_sidecar(name: str, kind: str, size: int, created_at: str) -> str:
    return json.dumps(
        {"name": name, "kind": kind, "bytes": size, "created_at": created_at},
        ensure_ascii=False, indent=2)


def _asset_default_name(hash16: str, ext: str) -> str:
    """没有可用显示名时的兜底名：孤儿正本与「用户没给文件名」都走它。"""
    return f"asset-{hash16}.{ext}"


def _resolved_asset_file(path: Path, root: Path) -> tuple[Path, re.Match[str]] | None:
    """素材正本 → ``(解析后的路径, 名字匹配)``；残片、边车与根外落点返回 ``None``。

    列表扫描与媒体读取共用这一份判定：两处各写一遍时，改「什么算素材」只会改到一处
    （``.tmp-*`` 与边车靠的是同一个命名正则）。
    """
    match = _ASSET_NAME_RE.match(path.name)
    if match is None:
        return None
    if match.group("ext") not in _ASSET_FORMATS:
        return None
    resolved = path.resolve()
    if not resolved.is_file() or not _is_within(resolved, root):
        return None
    return resolved, match


def _asset_from_path(session_id: str, path: Path, root: Path) -> Asset | None:
    """把一个磁盘文件解释成素材；不是素材形态（临时文件、边车、未知扩展名）返回 None。

    ``bytes`` 与 ``kind`` 一律从磁盘取：边车只是**显示名与首次创建时间**的载体，
    它坏了或被改过都不该让列表里的字节数说谎。
    """
    found = _resolved_asset_file(path, root)
    if found is None:
        return None
    resolved, match = found
    ext = match.group("ext")
    known = _ASSET_FORMATS[ext]
    hash16 = match.group("hash")
    stat = resolved.stat()
    kind = known[0]
    name = _asset_default_name(hash16, ext)
    created_at = datetime.fromtimestamp(
        stat.st_mtime, tz=timezone.utc).isoformat()
    sidecar = _read_asset_sidecar(resolved.with_suffix(".json"))
    if sidecar is not None:
        if isinstance(sidecar.get("name"), str) and sidecar["name"]:
            name = str(sidecar["name"])
        if isinstance(sidecar.get("created_at"), str) and sidecar["created_at"]:
            created_at = str(sidecar["created_at"])
    return Asset(asset_id=f"{session_id}/{hash16}", kind=kind, name=name,
                 bytes=stat.st_size, created_at=created_at)


def _assets_of(session_id: str, directory: Path, root: Path) -> list[Asset]:
    """会话素材清单：``assets/`` 目录现扫（#222 §5.3，与产物登记同款）。

    没有登记表要维护，所以「残片不出现在列表里」不靠清洗、靠命名规则：只有
    ``<16 位十六进制>.<已知扩展名>`` 才是素材，``.tmp-*`` 与边车天然落选。
    """
    assets_dir = directory / ASSETS_DIRNAME
    if not assets_dir.is_dir():
        return []
    found: list[Asset] = []
    for path in sorted(assets_dir.iterdir()):
        asset = _asset_from_path(session_id, path, root)
        if asset is not None:
            found.append(asset)
    found.sort(key=lambda item: (item.created_at, item.asset_id))
    return found


def _encode_cursor(updated_at: str, session_id: str) -> str:
    raw = json.dumps(
        {"u": updated_at, "s": session_id}, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    return str(raw["u"]), str(raw["s"])


def _is_after(item: SessionSummary, cursor: tuple[str, str]) -> bool:
    """列表顺序：``updated_at`` 降序，同刻按 ``session_id`` 升序。"""
    updated_at, session_id = cursor
    if item.updated_at != updated_at:
        return item.updated_at < updated_at
    return item.session_id > session_id


class WorkbenchSessionStore:
    """``sessions_root`` 之上的会话仓库。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # 建会话涉及「分配 GEN 号 + 写文件 + 记幂等」三步，进程内串行避免两发请求
        # 同时看到同一个空目录、都挑 GEN001。
        self._create_lock = threading.Lock()
        # 每会话一把进程内互斥锁，覆盖「读快照 → 改字段 → 整份写回」。执行器的进度写
        # （worker 线程）与取消请求（请求线程）会并发落到同一个会话文件；没有这把锁，
        # 先读到快照的一方会把另一方刚写的字段覆盖回旧值（#217 复审）。它是
        # ``threading.Lock`` 而不是 ``asyncio.Lock``：调用方在 ``asyncio.to_thread``
        # 的线程里，跨线程加锁只能靠线程原语。
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _resolve(self, session_id: str) -> Path:
        """把 ``session_id`` 解析成会话目录；越界、绝对路径、``..`` 一律 404。"""
        if not session_id or "\\" in session_id or session_id.startswith("/"):
            raise SessionNotFound("会话标识必须形如 <主题>/GEN00N。")
        parts = session_id.split("/")
        if any(part in ("", ".", "..") or ":" in part for part in parts):
            raise SessionNotFound("会话标识必须形如 <主题>/GEN00N。")
        root = self.root.resolve()
        directory = (root / Path(*parts)).resolve()
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise SessionNotFound("会话不在登记目录之下。") from exc
        if directory == root or not directory.is_dir():
            raise SessionNotFound(f"会话不存在：{session_id}")
        return directory

    def _derived_session_id(self, directory: Path) -> str:
        return directory.relative_to(self.root.resolve()).as_posix()

    def _load(self, session_id: str) -> tuple[Path, Any, str, bool]:
        directory = self._resolve(session_id)
        session = load_session(directory)
        if session is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        derived = self._derived_session_id(directory)
        legacy = (session.schema_version != WORKBENCH_SCHEMA_VERSION
                  or not session.session_id)
        if not legacy and session.session_id != derived:
            # 冗余存的 session_id 是自校验用的：对不上说明文件被搬过或改过，
            # 静默按目录继续等于把一份来历不明的快照当成合法会话。
            raise ValueError(
                f"会话文件的 session_id 与目录不符：{session.session_id!r} != {derived!r}")
        return directory, session, derived, legacy

    def _summary(self, session_id: str, session: Any, legacy: bool) -> SessionSummary:
        return SessionSummary(
            session_id=session_id,
            status=_status_for(session.status, legacy=legacy),
            legacy=legacy,
            revision=session.revision,
            created_at=session.created_at,
            updated_at=session.updated_at,
            plot=session.brief.plot,
            mode=session.brief.mode,
            variant=session.brief.variant,
        )

    def create(
        self,
        brief: Brief,
        *,
        idempotency_key: str | None = None,
        fingerprint: str,
    ) -> tuple[SessionDetail, bool]:
        """建会话；返回 ``(详情, 是否幂等重放)``。

        ``fingerprint`` 是请求体的摘要：同一个 key 配不同请求体是冲突，不能把
        第一次的响应冒充成第二次的答案（spec 错误码 ``idempotency_conflict``）。
        """
        registry = IdempotencyRegistry(
            self.root / ".workbench" / "create-idempotency.json")
        with self._create_lock:
            if idempotency_key:
                cached = registry.replay(idempotency_key, fingerprint=fingerprint)
                if cached is not None:
                    return SessionDetail.model_validate(cached), True

            # 延迟导入：``topic_generation`` 顶层拉起 LangGraph 管线，API 启动不该为
            # 一个 slug 函数付整条生成链的导入成本。
            from .topic_generation import topic_slug

            slug_dir = self.root / topic_slug(brief.plot)
            slug_dir.mkdir(parents=True, exist_ok=True)
            numbers = [
                int(match.group(1))
                for path in slug_dir.iterdir()
                if path.is_dir() and (match := _GEN_RE.match(path.name))
            ]
            generation = f"GEN{max(numbers, default=0) + 1:03d}"
            directory = slug_dir / generation
            directory.mkdir(parents=True, exist_ok=False)
            session_id = f"{slug_dir.name}/{generation}"
            # 与 ``update`` 共用同一把会话锁：建完立刻就有读者/写者时（幂等重放、
            # 紧跟着的详情查询），不会看到「文件已写、幂等表还没记」的中间态。
            with self._lock_for(session_id):
                # 先事件、后快照（ADR 0032 第 5 条）：崩在两者之间时，快照的
                # last_event_seq 落后，下一次读取按事件重放补齐。
                created = EventLog(directory, session_id).append(
                    [EventDraft("session.created", {"status": "created"})])[0]
                save_session(
                    directory, brief, {},
                    status="created",
                    session_id=session_id,
                    last_event_seq=created.seq,
                )
                detail = self._detail_unlocked(session_id)
                if idempotency_key:
                    registry.record(
                        idempotency_key, fingerprint=fingerprint,
                        response=detail.model_dump(mode="json"))
            return detail, False

    def get_session(self, session_id: str) -> SessionDetail:
        """会话详情；快照落后于事件流时先按事件重放补齐（spec §5.3）。

        为什么要在这里重放而不是只在启动时：崩溃窗口落在「事件已落盘、快照还没写」
        之间时，进程可能还活着（只有那个 job 崩了），读者下一次查询就该看到补齐后的
        真相。补齐是落盘的，所以这件事每个会话最多发生一次。
        """
        with self._lock_for(session_id):
            self._replay_if_needed(session_id)
            return self._detail_unlocked(session_id)

    def _detail_unlocked(self, session_id: str) -> SessionDetail:
        directory, session, derived, legacy = self._load(session_id)
        summary = self._summary(derived, session, legacy)
        jobs = [JobSummary.model_validate(item) for item in session.jobs]
        action_items = [ActionItemOut.model_validate(item)
                        for item in session.action_items]
        current_job = next(
            (job for job in reversed(jobs) if job.status == "running"), None)
        detail = SessionDetail(
            **summary.model_dump(),
            brief=_brief_out(session.brief, session.asset_ledger),
            last_event_seq=session.last_event_seq,
            current_job=current_job,
            jobs=jobs,
            action_items=action_items,
            artifacts=_artifacts(derived, directory, self.root.resolve()),
            assets=_assets_of(derived, directory, self.root.resolve()),
            segments=[SegmentOut.model_validate(item) for item in session.segments],
            director_notes=(
                _director_notes(directory, session.brief.duration)
                if session.brief.is_director else []),
        )
        detail._stage_state = dict(session.stage_state)
        detail._asset_ledger = [dict(entry) for entry in session.asset_ledger]
        return detail

    def _replay_if_needed(self, session_id: str) -> None:
        """快照的 ``last_event_seq`` 落后于事件流尾号时，把差额重放进快照并落盘。

        必须是幂等的：事件只是记录、不是命令（ADR 0032 第 4 条），重放同一批事件
        两次与一次结果相同，见 ``_replay_snapshot``。
        """
        directory, session, derived, legacy = self._load(session_id)
        if legacy:
            # 历史会话没有事件流，也不参与补齐（spec §5.4）。
            return
        log = EventLog(directory, derived)
        tail = log.last_seq()
        if tail <= session.last_event_seq:
            return
        status, jobs, action_items = _replay_snapshot(
            session, log.read(after_seq=session.last_event_seq))
        logger.info("快照落后事件流，已重放补齐：%s（%d → %d）",
                    session_id, session.last_event_seq, tail)
        self._state_with(
            directory, session, derived, status=status, jobs=jobs,
            action_items=action_items, last_event_seq=tail).save()

    def _state_with(
        self,
        directory: Path,
        session: SessionState,
        derived: str,
        *,
        status: str | None = None,
        stage_state: dict[str, Any] | None = None,
        jobs: Sequence[dict[str, Any]] | None = None,
        action_items: Sequence[dict[str, Any]] | None = None,
        last_event_seq: int | None = None,
        brief: Brief | None = None,
        variant_downgraded: str | None = None,
        segments: Sequence[dict[str, Any]] | None = None,
        asset_ledger: Sequence[dict[str, Any]] | None = None,
    ) -> SessionState:
        """以现有快照为底，覆盖调用方点名要改的字段。"""
        return SessionState(
            directory=directory,
            brief=session.brief if brief is None else brief,
            stage_state=(
                dict(stage_state) if stage_state is not None
                else dict(session.stage_state)),
            status=session.status if status is None else status,
            variant_downgraded=(
                session.variant_downgraded if variant_downgraded is None
                else variant_downgraded),
            frame_images=session.frame_images,
            created_at=session.created_at,
            updated_at=session.updated_at,
            schema_version=session.schema_version,
            session_id=derived,
            revision=session.revision,
            last_event_seq=(
                session.last_event_seq if last_event_seq is None
                else last_event_seq),
            jobs=tuple(jobs) if jobs is not None else session.jobs,
            action_items=(
                tuple(action_items) if action_items is not None
                else session.action_items),
            segments=(
                tuple(segments) if segments is not None
                else session.segments),
            asset_ledger=(
                tuple(asset_ledger) if asset_ledger is not None
                else session.asset_ledger),
        )

    def list_sessions(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> SessionPage:
        limit = max(1, min(limit, 200))
        summaries: list[SessionSummary] = []
        if self.root.is_dir():
            for path in self.root.glob(f"**/{SESSION_FILENAME}"):
                directory = path.parent
                session_id = directory.resolve().relative_to(
                    self.root.resolve()).as_posix()
                try:
                    session = load_session(directory)
                except ValueError as exc:
                    # 单个损坏文件不许炸掉整个列表；它也不许静默消失（留日志）。
                    logger.warning("会话文件损坏，列表已跳过：%s（%s）", directory, exc)
                    continue
                if session is None:
                    continue
                legacy = (session.schema_version != WORKBENCH_SCHEMA_VERSION
                          or not session.session_id)
                summaries.append(self._summary(session_id, session, legacy))
        # 两趟稳定排序：先按 id 升序，再按 updated_at 降序。
        summaries.sort(key=lambda item: item.session_id)
        summaries.sort(key=lambda item: item.updated_at, reverse=True)

        if cursor:
            after = _decode_cursor(cursor)
            summaries = [item for item in summaries if _is_after(item, after)]
        page = summaries[:limit]
        next_cursor = None
        if page and len(summaries) > limit:
            last = page[-1]
            next_cursor = _encode_cursor(last.updated_at, last.session_id)
        return SessionPage(items=page, next_cursor=next_cursor)

    def ensure_can_advance(self, detail: SessionDetail) -> None:
        """推进前的闸门（#217 的作业入口复用这一处判定）。"""
        if detail.legacy:
            raise SessionLegacy("历史会话（legacy）只读，不能起任务。")
        if detail.status == "running":
            raise SessionBusy("会话已有任务在跑；同一会话同时只允许一个任务。")
        if detail.status == "completed":
            raise SessionCompleted("会话已完成，不能再推进。")
        if detail.status == "awaiting_human":
            raise ActionItemPending("会话停在人工待办上，请先提交决定。")
        # ref 起跑门槛（D7 spec §4.3）：零素材可以建会话（停在 created），但参考素材
        # 是阶段 1 分镜表 Subject 标签的来源，一张 Picture 都没有就没有可写的闭包。
        if (is_ref_mode(detail.brief.mode)
                and not any(ref.kind == KIND_PICTURE for ref in detail.brief.refs)):
            raise AssetRequired(
                "先上传参考素材：ref 会话至少要有一张 Picture 才能起跑。")

    def session_directory(self, session_id: str) -> Path:
        """会话目录（执行器要在旁边写 ``active-job.json``）。"""
        return self._resolve(session_id)

    def write_text_artifact(self, session_id: str, name: str, text: str,
                            *, job_id: str | None = None) -> ArtifactInfo:
        """写一份文本产物并登记：写盘与发事件只此一处，后来的产出方照这条入口走。

        先落盘再登记——事件说的必须是已经发生的事实，反过来会让订阅者拿到一个
        还不存在的产物标识。
        """
        if _text_spec(name) is None:
            raise ValueError(f"不是文本产物名：{name!r}")
        directory = self.session_directory(session_id)
        atomic_write_text(directory / name, text)
        return self.register_artifact(session_id, name, job_id=job_id)

    def register_artifact(self, session_id: str, name: str, *,
                          job_id: str | None = None) -> ArtifactInfo:
        """产物登记的唯一入口：文件必须已在盘上，这里只发 ``artifact.created``。

        产物清单本身从目录现扫（``_artifacts``），事件是给订阅者的「它出现了」信号；
        重复产出同名文件是真实的第二次创建，每次都发，不在登记表里去重。
        """
        if artifact_spec(name) is None:
            raise ArtifactNotFound(f"不是登记范围内的产物名：{name!r}")
        directory = self.session_directory(session_id)
        path = self._artifact_path(directory, name)
        if path is None:
            raise ArtifactNotFound(f"产物不存在：{session_id}/{name}")
        info = _artifact_info(session_id, name, path)
        self.record_events(session_id, [EventDraft(
            "artifact.created",
            {"artifact_id": info.artifact_id, "kind": info.kind, "name": info.name},
            job_id=job_id)])
        return info

    def read_artifact_text(self, artifact_id: str) -> tuple[ArtifactInfo, str]:
        """文本产物全文；``artifact_id`` = ``<session_id>/<文件名>``（spec §7 / §10）。

        媒体类与越界标识共用 404：能区分「不存在」与「越界」就等于泄漏登记根之外
        是否真有文件（spec §12）。
        """
        session_id, name = _split_artifact_id(artifact_id)
        # 切分已保证名字在登记范围内；这里排除的是媒体形态（成片不按文本读）。
        if _text_spec(name) is None:
            raise ArtifactNotFound(f"没有可读的文本产物：{artifact_id}")
        try:
            directory = self.session_directory(session_id)
        except SessionNotFound as exc:
            # 产物端点不回报「会话存在与否」：越界、不存在的会话与不存在的文件同码。
            raise ArtifactNotFound(f"产物不存在：{artifact_id}") from exc
        path = self._artifact_path(directory, name)
        if path is None:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}")
        size = path.stat().st_size
        if size > ARTIFACT_MAX_BYTES:
            raise ArtifactTooLarge(
                f"文本产物 {artifact_id} 有 {size} 字节，超过 "
                f"{ARTIFACT_MAX_BYTES} 字节上限。")
        return _artifact_info(session_id, name, path), path.read_text(encoding="utf-8")

    def is_media_artifact(self, artifact_id: str) -> bool:
        """该名字按**媒体**形态读吗（False ⇒ 纯文本 + 1 MiB 上限）。

        形态由登记表派生，端点不再抄第二份白名单（#222 §7）。名字不在登记范围
        内就 404，与读内容同码。
        """
        _session_id, name = _split_artifact_id(artifact_id)
        return _media_spec(name) is not None

    def resolve_artifact_media(self, artifact_id: str) -> MediaResource:
        """媒体产物的可读字节（#222 §7）：与素材共用同一套 Range 口径。"""
        session_id, name = _split_artifact_id(artifact_id)
        spec = _media_spec(name)
        if spec is None:
            raise ArtifactNotFound(f"没有可读的媒体产物：{artifact_id}")
        try:
            directory = self.session_directory(session_id)
        except SessionNotFound as exc:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}") from exc
        path = self._artifact_path(directory, name)
        if path is None:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}")
        stat = path.stat()
        return MediaResource(
            path=path,
            media_type=cast(str, spec.media_type),
            size=stat.st_size,
            mtime=stat.st_mtime,
            # 不哈希成片：一次请求读几百 MiB 换个缓存标识不值，mtime+大小够本机用。
            etag=f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
            filename=name,
        )

    def _artifact_path(self, directory: Path, name: str) -> Path | None:
        """解析成真实文件并确认仍在登记根之下；符号链接逃逸返回 ``None``。

        登记表里的名字要么是固定名，要么是 ``segments/shot-NN.md``——都不含
        ``..``；这里再解析一次是因为**文件本身**可能是指向根外的符号链接，
        名字合法不代表落点在根内。
        """
        path = (directory / name).resolve()
        if not path.is_file() or not _is_within(path, self.root.resolve()):
            return None
        return path

    def ensure_upload_target(self, session_id: str) -> None:
        """上传前的会话判定：存在且非 legacy（#222 §5.2 的第一条）。

        单独成方法是为了让路由能在**读请求体之前**就把 404/409 发出去：先收完 512 MiB
        再告诉对方「这个会话不存在」没有道理，也不是 spec 的检查顺序。

        **起跑锁不在这里**：同一个端点既收参考素材登记，也收素材图与段视频回交
        （#256/#258，那时会话早已离开 ``created``）。锁只挡**台账登记**，见
        ``_ensure_asset_writable``——把它挪到这层会把整条回交路径一起锁死。
        """
        _directory, _session, _derived, legacy = self._load(session_id)
        if legacy:
            raise SessionLegacy("历史会话（legacy）只读，不能上传素材。")

    @staticmethod
    def _ensure_asset_writable(session: SessionState) -> None:
        """ref 台账的写入口径：起跑（离开 ``created``）即锁死**登记与撤销**。

        只对「带语义名的登记」生效：不带语义名的上传是普通素材（素材图/段视频回交），
        不在台账里、也不受这条约束。base 会话没有台账，整条判据对它是空操作。
        """
        if session.brief.is_ref and session.status != "created":
            raise AssetLocked(
                "参考素材只能在起跑前增删：会话已经开始，改台账会让已写好的资产图词"
                "与图对不上。")

    def list_assets(self, session_id: str) -> list[Asset]:
        """会话的素材清单（目录现扫）；会话不存在与产物清单同口径，一律 404。"""
        directory, _session, derived, _legacy = self._load(session_id)
        return _assets_of(derived, directory, self.root.resolve())

    def resolve_asset_media(self, asset_id: str) -> MediaResource:
        """把 ``<会话标识>/<16 位内容哈希>`` 解析成可读字节的素材文件。

        ``asset_id`` 只认 §5.4 的形态（哈希前缀、无扩展名）：扩展名由磁盘上的文件
        决定，客户端拼 URL 时不该猜它。非素材命名形态、根外落点、符号链接逃逸与
        不存在的素材一律 ``asset_not_found``（#222 §8，与产物读取同一取舍）。
        """
        session_id, _, hash16 = asset_id.rpartition("/")
        if not session_id or _ASSET_ID_RE.match(hash16) is None:
            raise AssetNotFound("素材标识必须形如 <会话标识>/<16 位内容哈希>。")
        try:
            directory, _session, derived, _legacy = self._load(session_id)
        except SessionNotFound as exc:
            raise AssetNotFound(f"素材不存在：{asset_id}") from exc
        root = self.root.resolve()
        assets_dir = directory / ASSETS_DIRNAME
        # 前缀碰撞时同一哈希可能对应多个扩展名文件；按名字排序取第一个，结果稳定。
        for candidate in sorted(assets_dir.glob(f"{hash16}.*")):
            found = _resolved_asset_file(candidate, root)
            if found is None:
                continue  # 边车（.json）、临时文件与根外落点都落选
            resolved, match = found
            asset = _asset_from_path(derived, resolved, root)
            if asset is None:
                continue
            stat = resolved.stat()
            return MediaResource(
                path=resolved,
                media_type=ASSET_MEDIA_TYPES[match.group("ext")],
                size=stat.st_size,
                mtime=stat.st_mtime,
                etag=match.group("hash"),
                filename=asset.name,
            )
        raise AssetNotFound(f"素材不存在：{asset_id}")

    def resolve_frame_asset(self, session_id: str, asset_id: str) -> Path:
        """把帧槽里的 ``asset_id`` 解析成本会话 ``assets/`` 下的图片文件（D4 spec §9）。

        与 ``resolve_asset_media`` 的分工：那个服务**读字节**（404 语义、标识里自带
        会话），这个服务**写决议**（422 语义、必须正是 URL 里那个会话、必须是图片）。
        「什么算素材」仍只判定一次（``_resolved_asset_file`` 那条路），这里只叠加
        会话归属与类型，不另写一份命名规则。
        """
        return self._resolve_owned_image(
            session_id, asset_id, FrameInvalid, "帧槽",
            missing_is_not_found=False)

    def resolve_image_asset(self, session_id: str, asset_id: str) -> Path:
        """把素材循环交回的 ``asset_id`` 解析成本会话的图片文件（D7 spec §6.3）。

        与 ``resolve_frame_asset`` 同一个判据、不同的错误码：素材图不合格的出路是
        「重新生成一张再交」，与帧槽填错不是一回事，前端要分得开；本会话里**不存在**
        的那一件也归 404 ``asset_not_found``（D7 §13）。
        """
        return self._resolve_owned_image(
            session_id, asset_id, AssetImageInvalid, "素材图",
            missing_is_not_found=True)

    def _resolve_owned_image(self, session_id: str, asset_id: str,
                             error: type[WorkbenchError], what: str, *,
                             missing_is_not_found: bool) -> Path:
        """归属 + 类型两道的单一声明：两个图片写入口共用，不各写一遍。

        「本会话里没有这一件」的码**按路线不同**（``missing_is_not_found``）：
        D4 spec §12 的帧槽是已验收的 422 ``frame_invalid``，D7 spec §13 的素材循环
        是 404 ``asset_not_found``。两边都是各自 spec 的原文，这里故意不折中成同一
        行为——改其中一边要先改对应的 spec，再改这个开关。

        「前缀不属于本会话」两侧一致回 ``error``（422）：能区分就等于给了一个探测
        别人会话素材的 oracle。
        """
        prefix, _, _hash16 = asset_id.rpartition("/")
        if prefix != session_id:
            raise error(f"{what}引用的素材不属于本会话：{asset_id}")
        try:
            resource = self.resolve_asset_media(asset_id)
        except AssetNotFound:
            if missing_is_not_found:
                raise
            raise error(f"{what}引用的素材不存在：{asset_id}") from None
        except WorkbenchError as exc:
            raise error(f"{what}引用的素材不存在：{asset_id}") from exc
        if not resource.media_type.startswith("image/"):
            raise error(f"{what}只接受图片素材（收到 {resource.media_type}）：{asset_id}")
        return resource.path

    def resolve_video_asset(self, session_id: str, asset_id: str) -> MediaResource:
        """把段视频待办的 ``video_asset_id`` 解析成本会话的 mp4 素材（D6 spec §8）。

        与 ``resolve_frame_asset`` 同一条路：归属必须正是 URL 里那个会话、类型收窄；
        差别只在要求 ``video/mp4``——spec §8 的「容器是 mp4」是**硬判据**（webm 之类
        同是视频但下游剥帧/拼接口径不同）。「什么算素材」仍只由 ``resolve_asset_media``
        判定一次，这里不另写命名规则。
        """
        prefix, _, _hash16 = asset_id.rpartition("/")
        if prefix != session_id:
            raise SegmentVideoInvalid(f"段视频素材不属于本会话：{asset_id}")
        try:
            resource = self.resolve_asset_media(asset_id)
        except WorkbenchError as exc:
            # 不存在与不可读同码：能区分就等于给了一个探测他会话素材的 oracle。
            raise SegmentVideoInvalid(f"段视频素材不存在：{asset_id}") from exc
        if resource.media_type != "video/mp4":
            raise SegmentVideoInvalid(
                f"本段视频必须是 mp4 容器（收到 {resource.media_type}）：{asset_id}")
        return resource

    def store_asset(
        self,
        session_id: str,
        source: BinaryIO,
        *,
        filename: str = "",
        semantic_name: str = "",
        idempotency_key: str | None = None,
    ) -> tuple[Asset, bool]:
        """把一份上传写进 ``<会话目录>/assets``；返回 ``(素材, 是否幂等重放)``。

        顺序照 #222 §5.2：会话存在且非 legacy → 嗅探（顺带定上限）→ 边写边判超限。
        嗅探**先于**读完整份内容：类型不在白名单就立刻 415，不陪一份 512 MiB 的垃圾
        走完全程。

        同一个端点收两种东西，判据只有一个：**带语义名 = 登记参考素材**（D7 §4.2，
        ref 专属）——语义名会话内唯一、容量按台账 9/3/3/12 在落盘前就 422、起跑后
        不再接受登记；**不带语义名 = 普通素材**——ref 与 base 行为一致，不记账、不
        编号、不进容量、不受起跑锁（素材图与段视频回交走这条，#256/#258）。base
        会话带语义名是 422 ``asset_name_invalid``：它没有台账可登记。

        「流式」的边界要说清：multipart 的解析由 Starlette 完成，>1 MiB 的部件先落
        **系统临时目录**里的 SpooledTemporaryFile——那一层不归本函数管（#222 §13 已
        记录）。这里保证的是「从部件到会话目录」这一段逐块搬运、逐块计数、超限即停，
        整份内容从不进内存。

        幂等键的指纹是**完整内容哈希**：同一个键配另一份内容属于冲突，不能把第一次的
        素材冒充成第二次的答案（与建会话的 ``idempotency_conflict`` 同一口径）。指纹
        就是内容本身，所以重放判定只能在读完之后——那一次仍会先落临时文件再删掉；
        「不重复写盘」说的是**正式正本**一份都不多写（#222 §5.3 的去重同款）。
        """
        # 会话判定与路由共用一处（``ensure_upload_target``）：两处各写一遍 legacy
        # 判据时，改判据的人只会改到一处。
        self.ensure_upload_target(session_id)
        directory, session, derived, _legacy = self._load(session_id)
        is_ref = session.brief.is_ref
        semantic_name = semantic_name.strip()
        # **带语义名 = 登记参考素材；不带 = 普通素材**（用户 2026-11-13 裁定）。
        # 同一个端点也收素材图与段视频回交（#256/#258），那时会话早已离开 created；
        # 按「起跑就锁全部上传」会把这些回交一起锁死，所以台账约束只挂在登记这条路上。
        registering = bool(semantic_name)
        if registering:
            if not is_ref:
                raise AssetNameInvalid(
                    "base 会话没有参考素材台账：语义名只对 ref 会话有效。")
            # 名字不合格不该陪一份 512 MiB 的字节流跑完整条通路。
            _validate_semantic_name(semantic_name)
        assets_dir = directory / ASSETS_DIRNAME
        tmp = assets_dir / f"{ASSET_TMP_PREFIX}{secrets.token_hex(8)}"
        digest = hashlib.sha256()
        total = 0
        kind = ""
        ext = ""
        limit = 0
        try:
            try:
                assets_dir.mkdir(parents=True, exist_ok=True)
                handle = tmp.open("wb")
            except OSError as exc:
                raise StorageUnavailable(f"素材目录写不进去：{exc}") from exc
            with handle:
                while True:
                    chunk = source.read(ASSET_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not ext:
                        sniffed = _sniff_asset(chunk[:ASSET_SNIFF_BYTES])
                        if sniffed is None:
                            raise UnsupportedMediaType(
                                "认不出上传内容的类型：只接受 PNG / JPEG / WebP 图片、"
                                "MP4 / WebM 视频、WAV / MP3 音频与 ZIP 导演包。")
                        kind, ext = sniffed
                        _check_declared_extension(filename, ext)
                        limit = ASSET_KIND_LIMITS[kind]
                        if registering:
                            # ZIP 是导演包的上传通道，不是参考素材类型：台账只有
                            # picture/video/audio 三型，硬记会在这里 KeyError 成 500。
                            if kind not in ASSET_KIND_TO_REFERENCE:
                                raise UnsupportedMediaType(
                                    "ZIP 归档不能登记为参考素材：参考素材只收图片 / "
                                    "视频 / 音频，导演包请不带语义名上传。")
                            # 快速失败：拿当前台账快照先判一次，超限就不搬剩余字节。
                            # 权威判定在会话锁内再做一次（并发上传可能同时通过这一关）。
                            _check_ref_capacity(
                                [*session.asset_ledger,
                                 _ledger_entry(
                                     semantic_name, kind,
                                     _next_number(session.asset_ledger, kind),
                                     "", "")],
                                where="上传参考素材")
                    # 先判后写：超限的那一块一个字节都不落盘，残片也不会长过上限。
                    if total + len(chunk) > limit:
                        raise AssetTooLarge(
                            f"素材超过 {kind} 类型的 {limit} 字节上限，已中止写入。")
                    handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            if not ext:
                raise UnsupportedMediaType("上传内容为空：无法判定类型。")
        except OSError as exc:
            # 磁盘满、只读目录：请求本身没错，是这台机器的存储不行（#222 §10 的 507）。
            _unlink_quietly(tmp)
            raise StorageUnavailable(f"素材落盘失败：{exc}") from exc
        except BaseException:
            # 任何失败（超限、客户端断开）都不许留下残片。
            _unlink_quietly(tmp)
            raise

        hash_full = digest.hexdigest()
        hash16 = hash_full[:ASSET_HASH_CHARS]
        target = assets_dir / f"{hash16}.{ext}"
        registry = self.session_registry(session_id)
        with self._lock_for(session_id):
            if idempotency_key:
                cached = registry.replay(idempotency_key, fingerprint=hash_full)
                if cached is not None:
                    _unlink_quietly(tmp)
                    return Asset.model_validate(cached), True
            # 锁内重新读一次：另一次并发上传可能已经改了台账；起跑也可能刚好发生
            # （``ensure_upload_target`` 是进入锁之前的事）。
            _directory2, fresh, _derived2, _legacy2 = self._load(session_id)
            ledger = [dict(entry) for entry in fresh.asset_ledger]
            if registering:
                try:
                    ledger = self._register_ledger_entry(
                        fresh, ledger, semantic_name=semantic_name,
                        asset_id=f"{derived}/{hash16}", sha256=hash_full,
                        asset_kind=kind)
                except BaseException:
                    _unlink_quietly(tmp)
                    raise
            if target.exists():
                # 去重：同内容不产生第二份文件，``created_at`` 保持首次（#222 §5.3）。
                _unlink_quietly(tmp)
            else:
                try:
                    os.replace(tmp, target)
                except OSError as exc:
                    _unlink_quietly(tmp)
                    raise StorageUnavailable(f"素材落盘失败：{exc}") from exc
            sidecar = target.with_suffix(".json")
            if not sidecar.exists():
                # 先正本后边车（#222 §5.3）：崩在两者之间只留孤儿正本，启动时补边车。
                try:
                    atomic_write_text(sidecar, _asset_sidecar(
                        _display_name(filename, _asset_default_name(hash16, ext)),
                        kind, target.stat().st_size,
                        datetime.now(timezone.utc).isoformat()))
                except OSError as exc:
                    # 正本已在盘上、列表也能按最小形态读出它，但这次请求确实没完整落盘；
                    # 按存储错误回报，别让调用方以为一切正常（重发会走去重补上边车）。
                    raise StorageUnavailable(f"素材边车写入失败：{exc}") from exc
            asset = _asset_from_path(derived, target, self.root.resolve())
            if asset is None:
                raise StorageUnavailable(f"素材落盘后读不回来：{target.name}")
            if registering:
                # 上传响应带台账分配的编号（D7 §12「返回编号与类型」）；类型仍是
                # 嗅探类型，官方三型见详情里的 brief.refs。
                entry = next(item for item in ledger
                             if item["asset_id"] == asset.asset_id)
                asset = asset.model_copy(update={"number": int(entry["number"])})
                # 台账与快照同一次落盘完成：正本已经在了，登记不能只留在内存里。
                self._state_with(directory, fresh, derived,
                                 asset_ledger=ledger).save()
            if idempotency_key:
                registry.record(idempotency_key, fingerprint=hash_full,
                                response=asset.model_dump(mode="json"))
            return asset, False

    def _register_ledger_entry(
        self,
        session: SessionState,
        ledger: list[dict[str, Any]],
        *,
        semantic_name: str,
        asset_id: str,
        sha256: str,
        asset_kind: str,
    ) -> list[dict[str, Any]]:
        """把一次 ref 上传登记进台账，返回重编号后的新台账（不落盘）。

        三条判据都在会话锁内跑：起跑锁（并发起跑刚发生）、语义名唯一、容量 9/3/3/12。
        同一份内容二次上传单独一码（``asset_duplicate``）——重复登记会让容量与件号
        同时说谎。
        """
        self._ensure_asset_writable(session)
        if any(str(entry["name"]) == semantic_name for entry in ledger):
            raise AssetNameInvalid(
                f"语义名「{semantic_name}」在本会话已用过：参考素材的语义名必须唯一。")
        if any(str(entry["asset_id"]) == asset_id for entry in ledger):
            raise AssetDuplicate(
                f"同一份素材已经在台账里（{asset_id}）：请换一份内容，或删掉旧登记。")
        updated = _renumber_ledger(
            [*ledger, _ledger_entry(semantic_name, asset_kind,
                                    _next_number(ledger, asset_kind), asset_id,
                                    sha256)])
        _check_ref_capacity(updated, where="上传参考素材")
        return updated

    def delete_asset(self, session_id: str, asset_id: str) -> None:
        """撤销一条参考素材**登记**并删掉它的正本（D7 §4.2，用户 2026-11-13 裁定新增）。

        只认台账里登记过的素材：台账外的 asset_id（含普通上传的素材图、段视频）一律
        404 ``asset_not_found``——删除撤销的是「登记」，不是「文件」；#256/#258 回交的
        普通素材不是参考素材，不该被这个端点摘掉。

        ``asset_id`` 与 ``GET /api/v1/media/{asset_id}`` 同形：``<会话标识>/<16 位哈希>``。
        拒法各有自己的码：base 会话调它 → ``route_mismatch``；不在台账里 → 404；
        **在台账里但已起跑** → ``asset_locked``（判据顺序如此：先说「没有这条登记」，
        再说「这条现在不能动」）。
        """
        directory, session, derived, legacy = self._load(session_id)
        if legacy:
            raise SessionLegacy("历史会话（legacy）只读，不能删除素材。")
        if not session.brief.is_ref:
            raise RouteMismatch("只有 ref 会话有参考素材台账；base 会话的素材不能删。")
        prefix, _, hash16 = asset_id.rpartition("/")
        if prefix != derived or _ASSET_ID_RE.match(hash16) is None:
            raise AssetNotFound(f"素材不存在：{asset_id}")
        with self._lock_for(session_id):
            # 锁内重读：起跑与另一次删除都可能刚好发生。
            directory, fresh, derived, _legacy = self._load(session_id)
            if not fresh.brief.is_ref:
                raise RouteMismatch("只有 ref 会话有参考素材台账；base 会话的素材不能删。")
            ledger = [dict(entry) for entry in fresh.asset_ledger]
            remaining = [entry for entry in ledger
                         if str(entry["asset_id"]) != asset_id]
            if len(remaining) == len(ledger):
                raise AssetNotFound(f"素材不存在：{asset_id}")
            # 起跑锁只挡「撤销登记」：先确认它真在台账里，再判能不能动。
            self._ensure_asset_writable(fresh)
            # 先落台账再删文件：台账是唯一真源，反过来会在「已删文件、台账还在」的
            # 窗口里让详情指向一份读不到的素材。
            self._state_with(directory, fresh, derived,
                             asset_ledger=_renumber_ledger(remaining)).save()
            for candidate in sorted((directory / ASSETS_DIRNAME).glob(f"{hash16}.*")):
                try:
                    candidate.unlink()
                except OSError as exc:
                    # 台账已经不再引用它：残留文件不影响任何读法，只记一行。
                    logger.warning("删除素材文件失败（台账已摘除）：%s（%s）",
                                   candidate, exc)

    def cleanup_assets(self) -> None:
        """启动清理（#222 §5.3）：删崩溃残留的 ``.tmp-*``，给孤儿正本补最小边车。

        为什么在启动做：「残片不出现在列表里」已由命名规则保证（见 ``_assets_of``），
        这里要的是**把盘上的垃圾清掉**，而崩溃残留只可能来自上一个进程。单个文件读写
        失败不阻断启动——与 ``list_sessions`` 跳过坏文件是同一个取舍。
        """
        if not self.root.is_dir():
            return
        for path in self.root.glob(f"**/{ASSETS_DIRNAME}/*"):
            try:
                if not path.is_file():
                    continue
                if path.name.startswith(ASSET_TMP_PREFIX):
                    _unlink_quietly(path)
                    continue
                match = _ASSET_NAME_RE.match(path.name)
                if match is None:
                    continue
                known = _ASSET_FORMATS.get(match.group("ext"))
                if known is None:
                    continue
                sidecar = path.with_suffix(".json")
                if sidecar.exists():
                    continue
                hash16 = match.group("hash")
                stat = path.stat()
                atomic_write_text(sidecar, _asset_sidecar(
                    _asset_default_name(hash16, match.group("ext")), known[0],
                    stat.st_size,
                    datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()))
            except OSError as exc:
                logger.warning("启动清理素材时跳过：%s（%s）", path, exc)

    def session_directories(self) -> list[Path]:
        """登记根下所有会话目录；单个坏文件由调用方自己决定怎么跳过。"""
        if not self.root.is_dir():
            return []
        return sorted(path.parent for path in self.root.glob(f"**/{SESSION_FILENAME}"))

    def mutate(
        self,
        session_id: str,
        mutator: Callable[[SessionDetail], dict[str, Any] | None],
        *,
        events: Sequence[EventDraft] = (),
    ) -> SessionDetail:
        """在会话锁内读-改-写：``mutator`` 拿到**当前**详情，返回要覆盖的字段。

        为什么需要回调而不是先 ``get_session`` 再 ``update``：调用方（执行器的进度写与
        取消请求）来自不同线程，两次文件读之间别人可能已经写过。把读也放进临界区，
        「找 job → 改字段 → 写回」才是原子的（#217 复审：取消标记曾被下一次进度写盖掉）。
        可覆盖的字段是 ``status`` / ``stage_state`` / ``jobs`` / ``action_items`` /
        ``brief`` / ``variant_downgraded`` / ``segments``（#219 起阶段 2 收尾要把生效
        变体与降级事实一起写回，绕开会话锁用 ``update_session_file`` 会把并发写盖掉；
        D6 起段状态也是快照字段，每次段通过立刻落盘）。

        ``events`` 是这次改动要**一起**落盘的事件（#218）：事件先写、快照后写，且两者
        在同一把锁内——序号分配与状态转移因此不会被别的写者插进来。``mutator`` 返回空
        dict / None 时不写文件、也不写事件（「没事干」的判定只有一处，不会因为带了事件
        就把一次空转记成真的发生过）。没有状态变化的进展用 ``record_events``。
        """
        lock = self._lock_for(session_id)
        with lock:
            self._replay_if_needed(session_id)
            detail = self._detail_unlocked(session_id)
            changes = mutator(detail) or {}
            if not changes:
                return detail
            directory, session, derived, legacy = self._load(session_id)
            if legacy:
                raise SessionLegacy("历史会话（legacy）只读，不能改写。")
            appended = EventLog(directory, derived).append(events)
            self._state_with(
                directory, session, derived,
                status=changes.get("status"),
                stage_state=changes.get("stage_state"),
                jobs=changes.get("jobs"),
                action_items=changes.get("action_items"),
                brief=changes.get("brief"),
                variant_downgraded=changes.get("variant_downgraded"),
                segments=changes.get("segments"),
                asset_ledger=changes.get("asset_ledger"),
                last_event_seq=(
                    appended[-1].seq if appended else session.last_event_seq),
            ).save()
            return self._detail_unlocked(session_id)

    def record_events(
        self, session_id: str, events: Sequence[EventDraft],
    ) -> SessionDetail:
        """只追加事件、不改快照字段（节点开始这类「没有状态变化」的进展）。

        快照仍然重写一次：把 ``last_event_seq`` 推到事件流尾号，否则下一次读取会把刚
        写的这条事件当成缺口，再重放一遍。
        """
        if not events:
            return self.get_session(session_id)
        with self._lock_for(session_id):
            self._replay_if_needed(session_id)
            directory, session, derived, legacy = self._load(session_id)
            if legacy:
                raise SessionLegacy("历史会话（legacy）只读，不能改写。")
            appended = EventLog(directory, derived).append(events)
            self._state_with(
                directory, session, derived,
                last_event_seq=appended[-1].seq,
            ).save()
            return self._detail_unlocked(session_id)

    def update(
        self,
        session_id: str,
        *,
        status: str | None = None,
        stage_state: dict[str, Any] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        action_items: list[dict[str, Any]] | None = None,
    ) -> SessionDetail:
        """补写会话快照的若干字段（``mutate`` 的固定字段版），其余键原样保留。

        为什么绕开 ``save_session``：执行器要在**不重建**会话对象的前提下推进
        （``variant_downgraded`` / ``frame_images`` 等键必须原样留在文件里）；重新构造
        一份缺省值的 ``SessionState`` 会把它们静默抹掉。``revision`` 由 ``save`` 按
        文件现值 +1，与建会话走同一口径。整段读-改-写在会话锁内完成，不会丢并发更新。
        """
        changes: dict[str, Any] = {}
        if status is not None:
            changes["status"] = status
        if stage_state is not None:
            changes["stage_state"] = stage_state
        if jobs is not None:
            changes["jobs"] = jobs
        if action_items is not None:
            changes["action_items"] = action_items
        return self.mutate(session_id, lambda _detail: changes)

    def session_registry(self, session_id: str) -> IdempotencyRegistry:
        """会话级幂等登记表（#217 起复用；建会话用根目录的那份）。"""
        directory = self._resolve(session_id)
        return IdempotencyRegistry(directory / ".idempotency.json")


__all__ = [
    "ActionItemNotFound", "ActionItemOut", "ActionItemPending", "ActionItemResolved",
    "ARTIFACT_FINAL_FILM", "ARTIFACT_FIRST_FRAME_PROMPT", "ARTIFACT_LAST_FRAME_PROMPT",
    "ARTIFACT_MAX_BYTES", "ARTIFACT_SCRIPT", "ARTIFACT_SHOT_TABLE",
    "ARTIFACT_SUMMARY_JSON", "ARTIFACT_SUMMARY_MARKDOWN",
    "ARTIFACT_VIDEO_PROMPT", "ARTIFACT_INJECTION_CARD",
    "SEGMENT_INJECTION_CARD_KIND", "segment_injection_card_name",
    "Asset", "AssetTooLarge",
    "AssetDuplicate", "AssetImageInvalid", "AssetLocked", "AssetNameInvalid",
    "AssetRequired",
    "ASSET_PROMPT_KIND", "ASSET_LEDGER_STATUS_PASSED", "ASSET_PROOFREAD_KEY",
    "RefCapacityExceeded", "RouteMismatch", "CacheOutOfRoot",
    "ArtifactInfo", "ArtifactNotFound", "ArtifactTooLarge", "BriefOut",
    "EVENT_TYPES", "EVENTS_FILENAME", "Event", "EventDraft", "EventLog",
    "FrameInvalid", "FrameMismatch", "VideoUnreadable",
    "DIRECTOR_FIRST_FRAME_PROMPT_KIND", "DIRECTOR_TAILFRAME_PROMPT_KIND",
    "director_first_frame_prompt_name", "director_tailframe_prompt_name",
    "IdempotencyConflict", "IdempotencyRegistry", "InvalidEventPayload",
    "JobNotFound", "JobNotRunning",
    "JobSummary", "RefOut", "RevisionOut", "SessionBusy", "SessionCompleted", "SessionDetail",
    "SessionLegacy", "SessionNotFound", "SessionPage", "SessionSummary",
    "SegmentOut", "SEGMENT_STATUS_PENDING", "SEGMENT_STATUS_WAITING_VIDEO",
    "SEGMENT_STATUS_VALIDATING", "SEGMENT_STATUS_PASSED", "SEGMENT_STATUSES",
    "SegmentVideoInvalid",
    "SESSION_STATUSES", "StorageUnavailable", "UnknownEventType",
    "UnsupportedMediaType", "UploadInvalid", "VideoOutputOut", "WorkbenchError",
    "WorkbenchSessionStore",
]
