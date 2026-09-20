"""生成台账：从磁盘重建「镜头 → 视频 → 输入帧」的链路。

背景：会话目录里只有 plan.json / progress.json / session-state.json，
桥接帧文件（``shot-0N-start.png``）与视频文件编号之间没有显式映射，
废弃版本与成品混在同一目录且无任何标记。结果是"哪个镜头用了哪张帧"
在磁盘上无法还原——2026-09-20 一次诊断中，分析者正是因此把废弃初版
``00003`` 当成了真实镜头。

本模块的做法是**建边而不是猜身份**：每张桥接帧对全部视频的尾窗口和首窗口
各做一次 argmin 比对，得到 ``src -> dst`` 的边，再从起点顺着边走。消歧靠
「链延续性 > 生成时间 > 命名自洽」，判不出来的一律进 `review_needed`，不猜。

人写的内容放 ``ledger.override.json``，本模块永不覆盖它。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SCHEMA = "h3-ledger/1"


@dataclass
class InputFrame:
    """某个镜头的输入帧。"""

    file: str
    kind: str            # "generated"（生图）| "extracted"（视频抽帧）| "unknown"
    source: dict | None  # {"video": "...", "frame": "head" | "tail"}；生图为 None
    match_mad: float | None
    confidence: str      # "high" | "medium" | "low"


@dataclass
class Shot:
    """一个进入正片的镜头。"""

    shot: int
    video: str
    video_dir: str
    frames: int
    duration_s: float
    generated_at: str
    input_frame: InputFrame
    status: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Discarded:
    """被判定为废弃初版、不进正片的视频。"""

    video: str
    video_dir: str
    reason: str
    replaced_by: str | None
    confidence: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Interruption:
    """流程中断：plan 期望的某一段没有产出。"""

    at_shot: int
    type: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "medium"
    hint: str = ""


@dataclass
class Ledger:
    """整套台账。用 to_dict / from_dict 做唯一的序列化出入口。"""

    schema: str
    session: dict
    scan: dict
    shots: list[Shot]
    discarded: list[Discarded]
    interruptions: list[Interruption]
    review_needed: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Ledger":
        schema = data.get("schema")
        if schema != SCHEMA:
            raise ValueError(
                f"不认识的 ledger schema: {schema!r}（本工具只支持 {SCHEMA!r}）"
            )
        return cls(
            schema=schema,
            session=dict(data.get("session") or {}),
            scan=dict(data.get("scan") or {}),
            shots=[
                Shot(input_frame=InputFrame(**s["input_frame"]), **{
                    k: v for k, v in s.items() if k != "input_frame"
                })
                for s in data.get("shots") or []
            ],
            discarded=[Discarded(**d) for d in data.get("discarded") or []],
            interruptions=[Interruption(**i) for i in data.get("interruptions") or []],
            review_needed=list(data.get("review_needed") or []),
            warnings=list(data.get("warnings") or []),
        )
