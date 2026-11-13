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
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ...brief_parser import Brief
from ...injection_card import VideoOutputParams
from ...user_revisions import (
    LAYER_ART,
    LAYER_CHARACTER,
    LAYER_FRAME,
    LAYER_STORY,
)
from ...workbench_store import (
    ARTIFACT_MEDIA_TYPES,
    ActionItemOut,
    ArtifactInfo,
    AssetImageInvalid,
    EventLog,
    FrameInvalid,
    JobSummary,
    SegmentVideoInvalid,
    SessionDetail,
    SessionPage,
    WorkbenchSessionStore,
)
from ..media import (
    MEDIA_RESPONSE_HEADERS,
    RANGE_RESPONSE_HEADERS,
    binary_content,
    build_media_response,
)
from ..problems import (
    AUTH_ERROR_RESPONSES,
    COMMON_ERROR_RESPONSES,
    ProblemDetail,
    ProblemError,
)

# 401/403 来自最外层的访问控制（api/auth.py），对所有会话路由都成立；豁免的健康检查
# 与引导端点不在此路由器下，所以它们不会在契约里多出这两个分支。
router = APIRouter(prefix="/api/v1", tags=["sessions"], responses=AUTH_ERROR_RESPONSES)

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


# D6 起时长放开到 4–60 秒（D6-AC1）：越过 10 秒的会话走长片分段陪跑，表单与校验
# 共用同一个上界。范围写在常量里而不是塞进 ``Field(ge=, le=)``：pydantic 的校验失败
# 走通用 422（没有 ``code``），前端只能靠解析文案分支；这个范围是一条**产品规则**，
# 要有自己的稳定错误码。
DURATION_MIN_SECONDS = 4.0
DURATION_MAX_SECONDS = 60.0

# ref 路线不走帧链（ADR 0011）：variant 是帧形态（T2VA/I2VA/FL2VA/L2VA），ref 段
# 不写单图锚，所以服务端固定落这一个值；DTO 里因此不出现「有时没有这个字段」。
REF_VARIANT = "T2VA"


class DurationOutOfScope(ProblemError):
    """时长越界：文案给出当前支持的范围，不指向别的票。"""

    status_code = 422
    code = "duration_out_of_scope"


def _validate_duration(duration: float) -> None:
    if DURATION_MIN_SECONDS <= duration <= DURATION_MAX_SECONDS:
        return
    raise DurationOutOfScope(
        f"V1 支持 {DURATION_MIN_SECONDS:g}–{DURATION_MAX_SECONDS:g} 秒；"
        f"收到的 duration={duration:g}。")


def _validate_route_parameters(payload: SessionCreate) -> None:
    """ref 路线不接受帧链的变体（D7 §4.1）。

    前端选 ref 时会隐藏 variant 下拉，这条只兜外部调用方。用通用 422 而不是
    ``route_mismatch``（409）：错的是**请求体字段**，不是会话状态冲突。
    """
    if payload.mode == "ref" and payload.variant != REF_VARIANT:
        raise HTTPException(
            422,
            f"ref 路线不走帧链（ADR 0011）：variant 只能是 {REF_VARIANT}，"
            f"收到 {payload.variant!r}。")


class SessionCreate(BaseModel):
    """建会话的 brief 一次性给全（spec §7；D7 §4.1 放开 ``ref``）。

    D2 起 ``mode`` 被写成单值字面量，由后续票扩成真实的两条路线。``ref`` 的参考
    素材**不进创建表单**：素材是会话内资产，创建时还没有 asset_id——``RefIn`` 与
    ``refs`` 一并删除，DTO 里不再出现本地路径。
    """

    mode: Literal["base", "ref"] = "base"
    variant: str = "T2VA"
    # 范围校验在 ``_validate_duration`` 里做（为的是拿到专用错误码）；这里不再写
    # ``gt=0``——pydantic 先拒会把 0/负数拦成没有 code 的通用 422。
    duration: float = 4.0
    style: str = "Cinematic"
    language: str = "Chinese"
    plot: str = Field(min_length=1)
    clarifications: str = ""
    draft: str = ""
    canvases: dict[str, tuple[int, int]] = Field(default_factory=dict)
    video_output: VideoOutputIn | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_refs(cls, data: Any) -> Any:
        """``refs`` 已经不在契约里：带它的请求要响，不能静默忽略。

        pydantic 默认忽略未知字段；旧客户端把一串本地路径塞进 ``refs`` 时会以为
        素材登记成功了。这里点名拒绝，让它立刻改成上传接口。
        """
        if isinstance(data, dict) and "refs" in data:
            raise ValueError(
                "创建表单不再接受 refs：参考素材只能建会话后走上传接口。")
        return data


class RevisionIn(BaseModel):
    layer: str = Field(min_length=1)
    text: str = Field(min_length=1)


class FrameSelection(BaseModel):
    """帧槽选择（D4 spec §9）：首/尾各绑本会话一个 D3 素材，都可留空。

    两个字段都是 ``asset_id``（``<会话标识>/<16 位内容哈希>``），不是路径；服务端
    负责归属与存在性校验。首尾都空＝显式跳过（降级为纯文字），不是「没带载荷」。
    """

    first: str | None = None
    last: str | None = None


class ActionItemResolve(BaseModel):
    """决议载荷（spec §7 / D4 §9 / D6 §8 / D7 §6.3）。

    四种人工待办各收一种载荷：
    - ``stage1_review`` 收 ``decision``（approve/revise，revise 带 ``revisions``）；
    - ``frame_submission`` 收 ``frames``；
    - ``segment_video`` 收 ``video_asset_id``（本段视频素材）；
    - ``asset_image`` 收 ``asset_id``（这一件资产图的生成结果，普通素材上传得到）。

    kind 与载荷的错配由服务端判成 422（``frame_invalid`` / ``segment_video_invalid`` /
    ``asset_image_invalid``，不是 pydantic 的通用 422）。
    """

    decision: Literal["approve", "revise"] | None = None
    revisions: list[RevisionIn] = Field(default_factory=list)
    frames: FrameSelection | None = None
    # 段视频待办（D6 §8）：本会话已上传的 mp4 素材 id。`Decision` 载荷互斥由路由先拦。
    video_asset_id: str | None = None
    # 素材图待办（D7 §6.3）：本会话已上传的图片素材 id（上传时不带语义名——它是
    # 用户生成的产物图，不是参考素材，不进台账）。
    asset_id: str | None = None


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
        # ref 的 variant 由服务端固定（见 ``REF_VARIANT``）；校验已在
        # ``_validate_route_parameters`` 里做过，这里只负责落库口径。
        variant=REF_VARIANT if payload.mode == "ref" else payload.variant,
        duration=payload.duration,
        style=payload.style,
        language=payload.language,
        plot=payload.plot,
        clarifications=payload.clarifications,
        draft=payload.draft,
        canvases=dict(payload.canvases),
        video_output=video_output,
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
    _validate_duration(payload.duration)
    _validate_route_parameters(payload)
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
          "description": "产物不存在、标识越界，或该名字不在登记范围内（problem+json）"},
    413: {"model": ProblemDetail, "description": "文本产物超过 1 MiB（problem+json）"},
    416: {"model": ProblemDetail,
          "description": "媒体产物的 Range 越界、语法非法或多段（problem+json）；"
                         "带 Content-Range: bytes */<len>"},
}

# 契约里的 200 形态同样由登记表派生（#222 §7）：文本表给 text/plain，媒体表给各自的
# Content-Type。端点里只按形态分支，不在这里另抄一份「哪些名字能读」。
_ARTIFACT_CONTENT = {
    "text/plain": {"schema": {"type": "string"}},
    **binary_content(ARTIFACT_MEDIA_TYPES.values()),
}

# GET 与 HEAD 的差别只在**成功**响应有没有体（HEAD 按定义没有）；错误两条路径同形，
# 都是 problem+json，所以共用 ``_ARTIFACT_RESPONSES``。
_ARTIFACT_GET_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "文本产物全文（UTF-8，1 MiB 上限）或媒体产物的整份字节。",
        "content": _ARTIFACT_CONTENT,
        "headers": MEDIA_RESPONSE_HEADERS,
    },
    206: {
        "description": "媒体产物的单段字节范围（Range: bytes=…）。",
        "content": binary_content(ARTIFACT_MEDIA_TYPES.values()),
        "headers": RANGE_RESPONSE_HEADERS,
    },
    **_ARTIFACT_RESPONSES,
}
_ARTIFACT_HEAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "与 GET 同头、无体。", "headers": MEDIA_RESPONSE_HEADERS},
    206: {"description": "媒体产物的字节范围，与 GET 同头、无体。",
          "headers": RANGE_RESPONSE_HEADERS},
    **_ARTIFACT_RESPONSES,
}


def _artifact_content_response(artifact_id: str, request: Request) -> Response:
    """按登记形态回产物字节；文本与媒体共用同一个入口，HEAD 由调用方声明。"""
    store = _store(request)
    head = request.method == "HEAD"
    if not store.is_media_artifact(artifact_id):
        _info, text = store.read_artifact_text(artifact_id)
        if head:
            # HEAD 与 GET 同头、无体：长度按 GET 会给的 UTF-8 字节数报。
            return Response(
                status_code=200,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Length": str(len(text.encode("utf-8"))),
                },
            )
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
    return build_media_response(
        store.resolve_artifact_media(artifact_id),
        request.headers.get("range"),
        request_path=request.url.path,
        head=head,
    )


@router.get(
    "/artifacts/{artifact_id:path}/content",
    response_class=Response,
    responses=_ARTIFACT_GET_RESPONSES,
)
def get_artifact_content(artifact_id: str, request: Request) -> Response:
    """按登记形态读产物（spec §10、#222 §7）。

    文本产物回纯文本（1 MiB 上限），媒体产物回字节并支持单段 Range——形态来自
    ``workbench_store`` 的登记表，这里不认文件名。``artifact_id`` 是
    ``<session_id>/<产物名>``：两边都可能含斜杠（``<主题>/GEN00N`` +
    ``segments/shot-01.md``），切分按登记表的最长合法后缀走，``:path`` 沿用会话
    路由的口径。越界与不存在一律 404 ``artifact_not_found``。
    """
    return _artifact_content_response(artifact_id, request)


@router.head(
    "/artifacts/{artifact_id:path}/content",
    response_class=Response,
    responses=_ARTIFACT_HEAD_RESPONSES,
)
def head_artifact_content(artifact_id: str, request: Request) -> Response:
    """探测产物：与 GET 同头、无体（#222 §6.2 的 HEAD 一格同样适用于成片）。"""
    return _artifact_content_response(artifact_id, request)


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
    response_model=JobSummary | None,
    responses={
        202: {
            # 202 的响应体形状由返回类型注解派生（``JobSummary | None``）；这里只补
            # 语义说明与幂等头，别再写一份 ``model``——两份会拼出重复的 anyOf。
            "description": (
                "决议已受理，重跑在后台执行（异步）。帧链通过审查不启动任务"
                "（等待帧提交），此时响应体为 null。"),
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
) -> JobSummary | None:
    """提交人工决定：``approve`` 走后续步骤（base 进阶段 2，ref 先进素材循环），
    ``revise`` 从声明层次重跑阶段 1。

    第二个人工待办（``frame_submission``）复用同一个端点、靠 body 分型（D4 §9）：
    带 ``frames`` 即帧提交；kind 与载荷错配一律 422 ``frame_invalid``。段视频
    （``video_asset_id``，D6 §8）与素材图（``asset_id``，D7 §6.3）同理。

    状态判定与「决议 + 起任务」的原子性都在 ``JobRunner.resolve`` 的会话临界区里，
    这里只把请求体归一成内部词汇，不先查一遍状态（两处判据会在并发下各说各话）。
    """
    revisions: list[dict[str, str]] = []
    if payload.decision == "approve":
        if payload.revisions:
            raise HTTPException(422, "approve 不接受 revisions：通过就是通过。")
    elif payload.decision == "revise":
        if not payload.revisions:
            raise HTTPException(422, "revise 至少要给一条修订。")
    # 归一化并带上：revise 要走修订回路；「没带 decision 却带 revisions + 素材载荷」
    # 也要带过去，由锁内 kind 判据给对应错误码（这里不抢先判，否则错误码会被
    # 帧提交专用的 frame_invalid 抢先）。
    if (payload.decision == "revise"
            or (payload.decision is None
                and (payload.video_asset_id is not None
                     or payload.asset_id is not None))):
        for revision in payload.revisions:
            text = revision.text.strip()
            if not text:
                raise HTTPException(422, "修订内容不能为空。")
            revisions.append(
                {"layer": _normalize_layer(revision.layer), "text": text})
    if (payload.decision is None and payload.revisions
            and payload.video_asset_id is None and payload.asset_id is None):
        # 帧提交没有修订语义；没有 decision 时若不点名拦下，这条会被静默忽略。
        raise FrameInvalid("帧提交不接受 revisions。")
    # 四套载荷互斥（D4 §9 / D6 §8 / D7 §6.3）：decision 属于审查待办，frames 属于帧
    # 提交待办，video_asset_id 属于段视频待办，asset_id 属于素材图待办。这一层先拦
    # 「同时给了多套」——哪个待办都不接受。
    if payload.decision is not None and payload.frames is not None:
        raise FrameInvalid(
            "decision 与 frames 不能同时提交：审查待办不帧提交，帧待办不 approve/revise。")
    if payload.video_asset_id is not None and (
            payload.decision is not None or payload.frames is not None
            or payload.asset_id is not None):
        raise SegmentVideoInvalid(
            "video_asset_id 不能与 decision/frames/asset_id 同时提交：段视频待办只收视频素材。")
    if payload.asset_id is not None and (
            payload.decision is not None or payload.frames is not None):
        raise AssetImageInvalid(
            "asset_id 不能与 decision/frames 同时提交：素材图待办只收图片素材。")
    if (payload.decision is None and payload.frames is None
            and payload.video_asset_id is None and payload.asset_id is None):
        raise HTTPException(
            422,
            "resolve 需要 decision（approve/revise）、frames、video_asset_id 或 asset_id。")
    frames: dict[str, str] | None = None
    if payload.frames is not None:
        # 空串与省略同义（前端清空槽位时两种形态都可能出现）；双空字典保留下来，
        # 它是「显式跳过」的载荷，不是「没带载荷」。
        frames = {
            role: value.strip()
            for role, value in (("first", payload.frames.first),
                                ("last", payload.frames.last))
            if value is not None
        }
    video_asset_id = (
        None if payload.video_asset_id is None else payload.video_asset_id.strip())
    asset_id = None if payload.asset_id is None else payload.asset_id.strip()
    job, replayed = await _runner(request).resolve(
        session_id, item_id, decision=payload.decision, revisions=revisions,
        frames=frames, video_asset_id=video_asset_id, asset_id=asset_id,
        idempotency_key=idempotency_key or None)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return job


_REOPEN_RESPONSES = {
    **_SESSION_ERROR_RESPONSES,
    200: {
        "model": ActionItemOut,
        "description": "段已回到等待交视频，返回新挂出的 segment_video 待办",
        "headers": {
            "Idempotency-Replayed": {
                "description": "重放标记；首次换段不返回该头",
                "schema": {"type": "string"},
            },
        },
    },
    404: {"model": ProblemDetail, "description": "会话或段不存在（problem+json）"},
    409: {"model": ProblemDetail, "description": "会话/段状态冲突（problem+json）"},
}


@router.post(
    "/sessions/{session_id:path}/segments/{index}/reopen",
    response_model=ActionItemOut,
    responses=_REOPEN_RESPONSES,
)
async def reopen_segment(
    session_id: str,
    index: int,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionItemOut:
    """把一条**已通过**的 ref 段打回等待交视频（D7 §5.5 / #258）。

    spec §12 原本写「不新增端点」，用户 2026-11-13 裁定新增这条段级端点。换段只换
    视频：段词与段内参考素材子集不动，重交后重过双闸门。base 会话回 409
    ``segment_resubmit_unsupported``（中间段换掉会断掉下一段的帧锚，ADR 0023）；
    未通过的段回 409 ``segment_not_passed``。

    状态判定与落盘在 ``JobRunner.reopen_segment`` 的会话临界区里完成，这里不先查
    一遍状态——两处判据会在并发下各说各话。
    """
    item, replayed = await _runner(request).reopen_segment(
        session_id, index, idempotency_key=idempotency_key or None)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return item


@router.get(
    "/sessions/{session_id:path}",
    response_model=SessionDetail,
    responses=_SESSION_ERROR_RESPONSES,
)
def get_session(session_id: str, request: Request) -> SessionDetail:
    return _store(request).get_session(session_id)
