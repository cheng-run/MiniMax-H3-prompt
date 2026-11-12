"""生产形态：FastAPI 同源托管前端构建产物（``frontend/dist``）。

路由优先级（D1 spec §8）：
1. ``/api/*`` 由 API 处理；未知接口返回 problem+json 404，**绝不能被 SPA fallback 吞掉**；
2. ``/openapi.json``、``/docs`` 由 FastAPI 自己的路由处理（它们在 app 初始化时就注册，
   比本模块的兜底路由更早匹配，因此不会被 fallback 拦截）；
3. ``dist`` 中已存在的文件按原路径返回——用 Starlette ``StaticFiles`` 解析，不手拼
   用户路径（避免路径穿越）；
4. 其余 GET/HEAD 返回 ``index.html``（SPA 深链刷新不 404）；
5. ``dist`` 缺失时返回 503 problem，提示先构建。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from .. import paths

# 前端构建产物目录：由路径模块唯一产出（开发态仓库内 ``frontend/dist``，冻结态解包目录下
# 的同一相对路径，见 ``paths.frontend_dist_dir``）。别再在这里写 ``parents[N]``。
DEFAULT_DIST = paths.frontend_dist_dir()

_BUILD_HINT = "前端构建产物不存在，请先在 frontend/ 目录执行 pnpm build。"


def install_spa(app: FastAPI, dist_dir: Path) -> None:
    # check_dir=False：Python 侧单测/CI 的 check job 没有构建 dist，不能在 app 创建时
    # 因目录缺失而抛错；真正用到 dist 的路径会先 is_dir 守卫再返回 503。
    static = StaticFiles(directory=dist_dir, check_dir=False)

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request) -> Response:
        # 1. 未知 /api/* → problem+json 404（明确失败，不退化成 index.html）
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        # 2. dist 缺失（或没有 index.html）→ 503 problem，指明先构建
        index = dist_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail=_BUILD_HINT)

        # 3. 已存在的静态文件交给 StaticFiles；找不到再回退 index.html
        try:
            return await static.get_response(full_path, request.scope)
        except StarletteHTTPException:
            return FileResponse(index)
