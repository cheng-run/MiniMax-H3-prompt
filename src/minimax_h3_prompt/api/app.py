"""FastAPI 应用工厂。

为什么用工厂而不是模块级单例：版本解析要在**启动阶段**失败（读不到包元数据就报错，
绝不返回假版本号），工厂让测试能在构造点钉住这条。生产入口在 ``__main__.py``。
"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path

from fastapi import FastAPI

from . import SERVICE_NAME
from .problems import install_problem_handlers
from .routes.health import router as health_router
from .spa import DEFAULT_DIST, install_spa


def _resolve_version() -> str:
    """从已安装包元数据读版本；读不到就明确报错，不写假版本号。"""
    try:
        return importlib.metadata.version(SERVICE_NAME)
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"读不到包 {SERVICE_NAME!r} 的版本元数据：请先用 uv sync 安装本包，"
            "不能返回假版本号。"
        ) from exc


def create_app(
    *, include_test_routes: bool = False, dist_dir: Path | None = None
) -> FastAPI:
    """构造 FastAPI 应用。

    ``include_test_routes=True`` 挂载测试专用的校验/崩溃探针（证明 422/500 接线），
    生产入口与 OpenAPI 导出都用默认 ``False``，探针不进入契约。
    ``dist_dir`` 默认指向仓库的 ``frontend/dist``；测试可注入临时目录。
    """
    app = FastAPI(title="MiniMax H3 Prompt API")
    # 版本在启动阶段解析并落在 app.state：健康接口读它，而非每次请求都查元数据。
    app.state.version = _resolve_version()
    install_problem_handlers(app)
    app.include_router(health_router)
    if include_test_routes:
        from .routes.validation_probe import router as probe_router

        app.include_router(probe_router)
    # 兜底路由必须最后注册：/openapi.json、/docs 与 /api/* 都要先匹配到自己的处理器。
    install_spa(app, dist_dir if dist_dir is not None else DEFAULT_DIST)
    return app
