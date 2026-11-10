"""工作台会话 API：建、列、详情；执行任务的起、查、取消；事件流 SSE（D2 §7、§8）。

本票之前的 501 占位已被真正的执行入口替换（#217）；事件流归 #218，待办决议与产物
内容分别归 #219/#220。路由顺序是契约的一部分：``{session_id:path}`` 会吞掉斜杠，
所以 ``/jobs``、``/jobs/{job_id}`` 与 ``/events`` 必须注册在
``GET /sessions/{session_id:path}`` **之前**，否则详情路由会把它们当会话标识。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ...brief_parser import Brief, RefItem
from ...injection_card import VideoOutputParams
from ...user_revisions import (
    LAYER_ART,
    LAYER_CHARACTER,
    LAYER_FRAME,
    LAYER_STORY,
)
from ...workbench_store import (
    ActionItemOut,
    ArtifactInfo,
    EventLog,
    JobSummary,
    SessionDetail,
    SessionPage,
    WorkbenchSessionStore,
)
from ..problems import COMMON_ERROR_RESPONSES, ProblemDetail

router = APIRouter(prefix="/api/v1", tags=["sessions"])

# 心跳间隔是 spec §8 的 15 秒。写成模块常量而不是字面量 sleep(15)，用例才能把它
# 调小去验证「空闲时真的会发心跳」——否则那条断言要么等 15 秒，要么只能删掉。
SSE_HEARTBEAT_SECONDS = 15.0
# 轮询事件文件的间隔；循环里还会按心跳把它进一步拆细（见 ``_event_stream``）。
SSE_POLL_SECONDS = 0.5

# 409/501 只在会话路由上有意义；仍然让契约声明 problem+json（D2-AC13）。
_SESSION_ERROR_RESPONSES = {
    **COMMON_ERROR_RESPONSES,
    409: {"model": ProblemDetail, "description": "会话状态冲突（problem+json）"},
}


class VideoOutputIn(BaseModel):
    ratio: str
    megapixels: float
    multiple: int


class RefIn(BaseModel):
    picture: int = 1
    name: str = ""
    description: str = ""
    path: str = ""


class SessionCreate(BaseModel):
    """建会话的 brief 一次性给全（spec §7）。

    D2 只做 ``base`` 短视频，所以 ``mode`` 是单值字面量；扩大路线由后续票改契约，
    不在这里留一个「看起来支持」的自由字符串。
    """

    mode: Literal["base"] = "base"
    variant: str = "T2VA"
    duration: float = Field(default=4.0, gt=0)
    style: str = "Cinematic"
    language: str = "Chinese"
    plot: str = Field(min_length=1)
    clarifications: str = ""
    draft: str = ""
    canvases: dict[str, tuple[int, int]] = Field(default_factory=dict)
    video_output: VideoOutputIn | None = None
    refs: list[RefIn] = Field(default_factory=list)


class RevisionIn(BaseModel):
    layer: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ActionItemResolve(BaseModel):
    """决议载荷（spec §7）：``approve`` 或 ``revise`` + 若干条（层次, 文字）。"""

    decision: Literal["approve", "revise"]
    revisions: list[RevisionIn] = Field(default_factory=list)


# 层次归一表（spec §7 的中文别名 + 向导屏幕标签 → 内部四档）。内部取值只住
# ``user_revisions.LAYER_*``，这里不重抄——重抄一份，改档位名时路由会静默收下旧词。
_LAYER_ALIASES: dict[str, str] = {
    "frame": LAYER_FRAME,
    "画面": LAYER_FRAME,
    "只改画面": LAYER_FRAME,
    "character": LAYER_CHARACTER,
    "人物": LAYER_CHARACTER,
    "人物设定": LAYER_CHARACTER,
    "art": LAYER_ART,
    "美术": LAYER_ART,
    "美术场景": LAYER_ART,
    "美术场景道具": LAYER_ART,
    "story": LAYER_STORY,
    "剧情": LAYER_STORY,
    "剧情分镜": LAYER_STORY,
    # spec §7 把最上游那一档写作「全部」；它重跑整条设定链（从编剧起），
    # 内部就是 story 层。两个词都收，不新增第五档。
    "全部": LAYER_STORY,
}


def _normalize_layer(raw: str) -> str:
    key = raw.strip()
    value = _LAYER_ALIASES.get(key) or _LAYER_ALIASES.get(key.lower())
    if value is None:
        raise HTTPException(422, f"不认识的修订层次：{raw!r}；"
                                 "可用：画面 / 人物设定 / 美术场景 / 全部（或内部四档名）。")
    return value


def _store(request: Request) -> WorkbenchSessionStore:
    return request.app.state.session_store


def _runner(request: Request):
    return request.app.state.job_runner


def _brief_from_payload(payload: SessionCreate) -> Brief:
    video_output = None
    if payload.video_output is not None:
        try:
            video_output = VideoOutputParams(
                ratio=payload.video_output.ratio,
                megapixels=payload.video_output.megapixels,
                multiple=payload.video_output.multiple,
            )
        except ValueError as exc:
            # 三元组有独立的节点契约校验（比例档/面积/对齐）；越界是请求错误，
            # 不是服务端故障，统一落 422 problem+json。
            raise HTTPException(422, str(exc)) from exc
    return Brief(
        mode=payload.mode,
        variant=payload.variant,
        duration=payload.duration,
        style=payload.style,
        language=payload.language,
        plot=payload.plot,
        clarifications=payload.clarifications,
        draft=payload.draft,
        canvases=dict(payload.canvases),
        video_output=video_output,
        refs=[RefItem(picture=item.picture, name=item.name,
                      description=item.description, path=item.path)
              for item in payload.refs],
    )


def _fingerprint(payload: SessionCreate) -> str:
    """幂等键绑定的请求体摘要；省略字段与显式默认值算同一份请求。"""
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.post(
    "/sessions",
    status_code=201,
    response_model=SessionDetail,
    responses={
        200: {
            "model": SessionDetail,
            "description": "幂等重放：返回第一次的结果",
            "headers": {
                "Idempotency-Replayed": {
                    "description": "重放标记；首次创建不返回该头",
                    "schema": {"type": "string"},
                },
            },
        },
        **_SESSION_ERROR_RESPONSES,
    },
)
def create_session(
    payload: SessionCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SessionDetail:
    detail, replayed = _store(request).create(
        _brief_from_payload(payload),
        idempotency_key=idempotency_key or None,
        fingerprint=_fingerprint(payload),
    )
    if replayed:
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
    return detail


@router.get(
    "/sessions",
    response_model=SessionPage,
    responses=_SESSION_ERROR_RESPONSES,
)
def list_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> SessionPage:
    try:
        return _store(request).list_sessions(limit=limit, cursor=cursor)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, "cursor 无效：无法解析分页游标。") from exc


_JOB_RESPONSES = {
    **_SESSION_ERROR_RESPONSES,
    404: {"model": ProblemDetail, "description": "任务不存在（problem+json）"},
    409: {"model": ProblemDetail, "description": "会话/任务状态冲突（problem+json）"},
}


@router.post(
    "/sessions/{session_id:path}/jobs",
    status_code=202,
    response_model=JobSummary,
    responses={
        202: {
            "model": JobSummary,
            "description": "任务已受理（异步执行）",
            "headers": {
                "Idempotency-Replayed": {
                    "description": "重放标记；首次起任务不返回该头",
                    "schema": {"type": "string"},
                },
            },
        },
        **_JOB_RESPONSES,
    },
)
async def start_job(
    session_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobSummary:
    """起一次 ``advance``：202 返回任务对象，执行在后台任务里。

    闸门与单写入者判定在 ``JobRunner.advance`` 的会话临界区里完成；这里不再判一遍，
    否则「先查后写」的两处判据会在并发下各说各话（D2-AC3）。
    """
    job, replayed = await _runner(request).advance(
        session_id, idempotency_key=idempotency_key or None)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return job


@router.get(
    "/sessions/{session_id:path}/jobs/{job_id}",
    response_model=JobSummary,
    responses=_JOB_RESPONSES,
)
def get_job(session_id: str, job_id: str, request: Request) -> JobSummary:
    return _runner(request).get_job(session_id, job_id)


@router.post(
    "/sessions/{session_id:path}/jobs/{job_id}/cancel",
    status_code=202,
    response_model=JobSummary,
    responses=_JOB_RESPONSES,
)
async def cancel_job(session_id: str, job_id: str, request: Request) -> JobSummary:
    """请求取消：只打标记，任务在当前节点跑完后才停（**不承诺立即停止**）。"""
    return await _runner(request).cancel(session_id, job_id)


_EVENTS_RESPONSES = {
    **_SESSION_ERROR_RESPONSES,
    422: {"model": ProblemDetail, "description": "Last-Event-ID 不是事件序号（problem+json）"},
}


def _parse_cursor(header_value: str | None, query_value: str | None) -> int:
    """补发起点：头优先，查询参数兜底（浏览器 ``EventSource`` 不能自定义请求头）。"""
    raw = header_value if header_value not in (None, "") else query_value
    if raw in (None, ""):
        return 0
    try:
        cursor = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Last-Event-ID 必须是事件序号（非负整数）。") from exc
    if cursor < 0:
        raise HTTPException(422, "Last-Event-ID 必须是事件序号（非负整数）。")
    return cursor


async def _event_stream(log: EventLog, cursor: int, tail: int) -> AsyncIterator[str]:
    """从 ``cursor`` 之后补发，然后持续跟读；空闲时发心跳注释行。

    读文件走 ``asyncio.to_thread``：长任务本身也在线程池里跑，事件循环必须一直是空的，
    否则详情查询与别的订阅会被拖着（spec §6）。
    """
    if cursor > tail:
        # 缺口补不上（事件文件被清掉、换了机器）：注释行给人看，
        # ``X-Resync-Required`` 响应头给程序看——注释行浏览器拿不到，光有它等于没信号。
        yield ": resync required：事件序号超出事件流尾号，请重新 GET 详情拿快照\n\n"
    last = cursor
    last_ping = time.monotonic()
    while True:
        events = await asyncio.to_thread(log.read, after_seq=last)
        for event in events:
            yield event.sse_frame()
            last = event.seq
        now = time.monotonic()
        if events:
            last_ping = now
            continue
        heartbeat = SSE_HEARTBEAT_SECONDS
        if now - last_ping >= heartbeat:
            yield ": heartbeat\n\n"
            last_ping = now
        # 睡眠不超过半个心跳：否则心跳会被一次长轮询推后，最坏晚一整个轮询间隔。
        await asyncio.sleep(max(0.01, min(SSE_POLL_SECONDS, heartbeat / 2)))


@router.get(
    "/sessions/{session_id:path}/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "事件流（SSE）。快照优先：先 GET 详情拿快照，再订阅本流；断线重连带 "
                "Last-Event-ID（或 ?last_event_id=）补发序号更大的事件；序号超出事件流"
                "尾号时响应头带 X-Resync-Required: true，要求客户端重取快照。"
                "每 15 秒一条心跳注释行；同一会话允许多条并发订阅。"),
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
            "headers": {
                "X-Resync-Required": {
                    "description": "缺口补不上，需要重新 GET 详情拿快照",
                    "schema": {"type": "string"},
                },
            },
        },
        **_EVENTS_RESPONSES,
    },
)
async def stream_events(
    session_id: str,
    request: Request,
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id: str | None = Query(default=None),
) -> StreamingResponse:
    """订阅会话事件流。

    **快照优先**（spec §8）：先 ``GET /api/v1/sessions/{session_id}`` 拿快照，再订阅
    本流；补不上缺口时按响应头的 ``X-Resync-Required`` 重取快照。
    """
    cursor = _parse_cursor(last_event_id_header, last_event_id)
    store = _store(request)
    # 顺带取详情：会话不存在要在这里就 404——流一旦开始，状态码已经发出去了。
    detail = await asyncio.to_thread(store.get_session, session_id)
    log = EventLog(store.session_directory(session_id), detail.session_id)
    tail = await asyncio.to_thread(log.last_seq)
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if cursor > tail:
        headers["X-Resync-Required"] = "true"
    return StreamingResponse(
        _event_stream(log, cursor, tail),
        media_type="text/event-stream",
        headers=headers,
    )


_ACTION_ITEM_RESPONSES = {
    **_SESSION_ERROR_RESPONSES,
    404: {"model": ProblemDetail, "description": "待办不存在（problem+json）"},
    409: {"model": ProblemDetail, "description": "会话/待办状态冲突（problem+json）"},
}


@router.get(
    "/sessions/{session_id:path}/artifacts",
    response_model=list[ArtifactInfo],
    responses=_SESSION_ERROR_RESPONSES,
)
def list_artifacts(session_id: str, request: Request) -> list[ArtifactInfo]:
    """会话的产物清单：与详情里的 ``artifacts`` 同一份来源（目录现扫）。"""
    return _store(request).get_session(session_id).artifacts


_ARTIFACT_RESPONSES = {
    **_SESSION_ERROR_RESPONSES,
    404: {"model": ProblemDetail,
          "description": "产物不存在、标识越界，或该产物不提供内容（problem+json）"},
    413: {"model": ProblemDetail, "description": "文本产物超过 1 MiB（problem+json）"},
}


@router.get(
    "/artifacts/{artifact_id:path}/content",
    response_class=PlainTextResponse,
    responses={
        200: {
            "description": "文本产物全文（UTF-8）；视频类在 D2 不提供内容。",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        **_ARTIFACT_RESPONSES,
    },
)
def get_artifact_content(artifact_id: str, request: Request) -> PlainTextResponse:
    """读文本产物全文（spec §10）。

    ``artifact_id`` 是 ``<session_id>/<文件名>``：会话标识本身含斜杠，所以 ``:path``
    沿用会话路由的口径，最后一段才是文件名。越界与不存在一律 404 ``artifact_not_found``。
    """
    _info, text = _store(request).read_artifact_text(artifact_id)
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@router.get(
    "/sessions/{session_id:path}/action-items",
    response_model=list[ActionItemOut],
    responses=_SESSION_ERROR_RESPONSES,
)
def list_action_items(session_id: str, request: Request) -> list[ActionItemOut]:
    """会话的全部待办（含已决议的）：创建与决议成对可查，前端不必自己攒。"""
    return _store(request).get_session(session_id).action_items


@router.post(
    "/sessions/{session_id:path}/action-items/{item_id}/resolve",
    status_code=202,
    response_model=JobSummary,
    responses={
        202: {
            "model": JobSummary,
            "description": "决议已受理，重跑在后台执行（异步）",
            "headers": {
                "Idempotency-Replayed": {
                    "description": "重放标记；首次决议不返回该头",
                    "schema": {"type": "string"},
                },
            },
        },
        **_ACTION_ITEM_RESPONSES,
    },
)
async def resolve_action_item(
    session_id: str,
    item_id: str,
    payload: ActionItemResolve,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobSummary:
    """提交人工决定：``approve`` 起阶段 2，``revise`` 从声明层次重跑阶段 1。

    状态判定与「决议 + 起任务」的原子性都在 ``JobRunner.resolve`` 的会话临界区里，
    这里只把请求体归一成内部词汇，不先查一遍状态（两处判据会在并发下各说各话）。
    """
    revisions: list[dict[str, str]] = []
    if payload.decision == "approve":
        if payload.revisions:
            raise HTTPException(422, "approve 不接受 revisions：通过就是通过。")
    else:
        if not payload.revisions:
            raise HTTPException(422, "revise 至少要给一条修订。")
        for revision in payload.revisions:
            text = revision.text.strip()
            if not text:
                raise HTTPException(422, "修订内容不能为空。")
            revisions.append(
                {"layer": _normalize_layer(revision.layer), "text": text})
    job, replayed = await _runner(request).resolve(
        session_id, item_id, decision=payload.decision, revisions=revisions,
        idempotency_key=idempotency_key or None)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return job


@router.get(
    "/sessions/{session_id:path}",
    response_model=SessionDetail,
    responses=_SESSION_ERROR_RESPONSES,
)
def get_session(session_id: str, request: Request) -> SessionDetail:
    return _store(request).get_session(session_id)
