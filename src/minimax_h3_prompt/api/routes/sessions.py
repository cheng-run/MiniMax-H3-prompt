"""工作台会话 API：建、列、详情；执行任务的起、查、取消（D2 §7 / #215、#217）。

本票之前的 501 占位已被真正的执行入口替换（#217）；事件流、待办决议与产物内容
分别归 #218–#220。路由顺序是契约的一部分：``{session_id:path}`` 会吞掉斜杠，
所以 ``/jobs`` 与 ``/jobs/{job_id}`` 必须注册在 ``GET /sessions/{session_id:path}``
**之前**，否则详情路由会把它们当会话标识。
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ...brief_parser import Brief, RefItem
from ...injection_card import VideoOutputParams
from ...workbench_store import (
    JobSummary,
    SessionDetail,
    SessionPage,
    WorkbenchSessionStore,
)
from ..problems import COMMON_ERROR_RESPONSES, ProblemDetail

router = APIRouter(prefix="/api/v1", tags=["sessions"])

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


@router.get(
    "/sessions/{session_id:path}",
    response_model=SessionDetail,
    responses=_SESSION_ERROR_RESPONSES,
)
def get_session(session_id: str, request: Request) -> SessionDetail:
    return _store(request).get_session(session_id)
