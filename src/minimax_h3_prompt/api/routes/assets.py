"""上传素材：``POST/GET /api/v1/sessions/{session_id:path}/assets``（#222 §5，D3-2）。

为什么单独一个路由模块：``{session_id:path}`` 会吞掉斜杠，``/assets`` 必须在
``GET /sessions/{session_id:path}`` **之前**注册，否则会被详情路由当成会话标识
（``routes/sessions.py`` 的模块 docstring 记着同一条教训）。放在这里，顺序由
``app.create_app`` 的注册行直接表达，而不是靠人在同一个文件里维护「谁写在前面」。

multipart 自己解析、不用声明式的 ``file: UploadFile`` 参数：FastAPI 的端点校验会把
「不是表单」「缺 ``file`` 字段」变成 422，而契约要求这两种是 400 ``upload_invalid``
（#222 §5.2）——两种失败的处置对前端不同：一个是「补字段」，一个是「改发送方式」。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Header, Request, Response
# 认的是 **Starlette 的** ``UploadFile``：``fastapi.UploadFile`` 是它的子类，而
# ``request.form()`` 解析出来的永远是父类实例——拿 FastAPI 那个做 isinstance 判定，
# 每一份合法上传都会被判成「没有 file 字段」（实测）。
from starlette.datastructures import UploadFile

from ...workbench_store import Asset, UploadInvalid, WorkbenchError
from ..problems import AUTH_ERROR_RESPONSES, COMMON_ERROR_RESPONSES, ProblemDetail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["assets"], responses=AUTH_ERROR_RESPONSES)

# 手工解析请求体时 FastAPI 不会替我们推导 requestBody；不补这一段，契约里这次上传
# 就是「没有请求体」——生成客户端会把它当无参 POST（#222 §9 的契约链要求）。
_MULTIPART_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        # 语义名（D7 §4.2）：带 name = 登记参考素材（会话内唯一、编号、
                        # 受起跑锁约束）；不带 = 普通素材（素材图与段视频回交走这条，
                        # 同一个端点，不能逼它们带语义名）。
                        "name": {
                            "type": "string",
                            "description": (
                                "参考素材的语义名；带上即登记进台账（会话内唯一，"
                                "起跑后不允许再登记），不带则按普通素材上传。"),
                        },
                    },
                },
            },
        },
    },
}

_UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    **COMMON_ERROR_RESPONSES,
    400: {"model": ProblemDetail,
          "description": "请求不是可解析的 multipart 表单，或缺 file 字段（problem+json）"},
    409: {"model": ProblemDetail,
          "description": "历史会话只读、幂等键已用于另一份内容，"
                         "或 ref 会话已起跑（asset_locked）（problem+json）"},
    413: {"model": ProblemDetail, "description": "超过该类型上限，已中止写入（problem+json）"},
    415: {"model": ProblemDetail,
          "description": "魔数不在白名单，或声明的扩展名与内容不符（problem+json）"},
    507: {"model": ProblemDetail, "description": "磁盘写入失败（problem+json）"},
}


async def _receive_upload(
    request: Request, session_id: str, idempotency_key: str | None,
) -> tuple[Asset, bool]:
    """解析 multipart 并把内容交给存储层；失败按 #222 §10 的错误码抛出。"""
    store = request.app.state.session_store
    # 会话判定先于读请求体（#222 §5.2 的第一条）：给一个不存在的会话传 512 MiB，
    # 不该先让 Starlette 把它整个收进临时目录再回 404。读盘仍走线程池——这是 async
    # 路由，同步读会话文件会把事件循环卡在这里（同文件其余读盘都在 to_thread 里）。
    await asyncio.to_thread(store.ensure_upload_target, session_id)
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001 - 解析不出表单就是「这份上传读不出来」
        # 不回显解析器的原文：边界串与内部偏移量对排障没帮助，反而会进日志。
        raise UploadInvalid("请求不是可解析的 multipart/form-data。") from exc
    try:
        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile):
            raise UploadInvalid("上传必须带单文件字段 file。")
        # 语义名是普通文本部件；缺失或不是文本（例如客户端误当文件发）一律按空串，
        # 由存储层对 ref 会话判「必填」——判据只住一处。
        raw_name = form.get("name")
        semantic_name = raw_name if isinstance(raw_name, str) else ""
        # 读盘走线程池：搬运 512 MiB 不能占着事件循环。
        return await asyncio.to_thread(
            store.store_asset,
            session_id,
            uploaded.file,
            filename=uploaded.filename or "",
            semantic_name=semantic_name,
            idempotency_key=idempotency_key,
        )
    finally:
        await form.close()


@router.post(
    "/sessions/{session_id:path}/assets",
    status_code=201,
    response_model=Asset,
    openapi_extra=_MULTIPART_BODY,
    responses={
        200: {
            "model": Asset,
            "description": "幂等重放：返回第一次写入的素材",
            "headers": {
                "Idempotency-Replayed": {
                    "description": "重放标记；首次上传不返回该头",
                    "schema": {"type": "string"},
                },
            },
        },
        **_UPLOAD_RESPONSES,
    },
)
async def upload_asset(
    session_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Asset:
    """收一份素材：边写边判超限、按字节定类型、按内容哈希去重（#222 §5）。

    上传**不改会话状态、不产生事件**（D3-AC13）：素材是不可变的旁挂资源，与运行中的
    任务并行安全。
    """
    started = time.monotonic()
    try:
        asset, replayed = await _receive_upload(
            request, session_id, idempotency_key or None)
    except WorkbenchError as exc:
        # #222 §12：失败也记一行，带稳定错误码；不记显示名（那是用户内容）。
        logger.warning("素材上传失败：%s → %s（%.0f ms）", session_id, exc.code,
                       (time.monotonic() - started) * 1000)
        raise
    if replayed:
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
    logger.info("素材上传：%s → %s（%d 字节，%.0f ms）%s", session_id,
                asset.asset_id, asset.bytes, (time.monotonic() - started) * 1000,
                "（幂等重放）" if replayed else "")
    return asset


@router.get(
    "/sessions/{session_id:path}/assets",
    response_model=list[Asset],
    responses=COMMON_ERROR_RESPONSES,
)
def list_assets(session_id: str, request: Request) -> list[Asset]:
    """会话的素材清单：与详情里的 ``assets`` 同一份来源（目录现扫，无登记表）。"""
    return request.app.state.session_store.list_assets(session_id)


_DELETE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **COMMON_ERROR_RESPONSES,
    404: {"model": ProblemDetail,
          "description": "素材不存在，或不属于 URL 里那个会话（problem+json）"},
    409: {"model": ProblemDetail,
          "description": "base 会话调用（route_mismatch）、会话已起跑"
                         "（asset_locked）或历史会话只读（problem+json）"},
}


@router.delete(
    "/sessions/{session_id:path}/assets/{asset_id:path}",
    status_code=204,
    responses=_DELETE_RESPONSES,
)
def delete_asset(session_id: str, asset_id: str, request: Request) -> Response:
    """删掉一条参考素材登记与它的正本（D7 spec §4.2）。

    spec §4.2 要求「循环开始前可删素材」，§12 又写「不新增端点」——两句话冲突，
    用户 2026-11-13 裁定**新增删除端点**。``asset_id`` 与 ``GET /api/v1/media/…``
    同形（``<会话标识>/<16 位内容哈希>``），归属校验在存储层：不属于本会话与不存在
    同码 404，不给出探测他会话素材的 oracle。

    成功回 204 空体；**不发事件**（素材增删仍不是事件，D3-AC13）。
    """
    request.app.state.session_store.delete_asset(session_id, asset_id)
    return Response(status_code=204)
