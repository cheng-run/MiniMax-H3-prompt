"""FastAPI 应用工厂。

为什么用工厂而不是模块级单例：版本解析要在**启动阶段**失败（读不到包元数据就报错，
绝不返回假版本号），工厂让测试能在构造点钉住这条。生产入口在 ``__main__.py``。
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from . import SERVICE_NAME
from .auth import attach_local_access
from .fake_pipeline import FAKE_PIPELINE_ENV, fake_pipeline_enabled
from .jobs import JobRunner
from .problems import apply_problem_media_type_openapi, install_problem_handlers
from .routes.assets import router as assets_router
from .routes.auth import router as auth_router
from .routes.health import router as health_router
from .routes.media import router as media_router
from .routes.sessions import router as sessions_router
from .spa import DEFAULT_DIST, install_spa

logger = logging.getLogger(__name__)


def _default_sessions_root() -> Path:
    """默认会话根取项目配置（spec §5.1）；延迟导入避免 API 启动就拉起整份配置。

    测试与工作树通过 ``create_app(sessions_root=...)`` 注入临时目录，不碰真实
    ``output/sessions``——那里是用户正在跑的会话，测试不许读写。
    """
    from ..config import config

    return config.sessions_root


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
    *, include_test_routes: bool = False, dist_dir: Path | None = None,
    sessions_root: Path | None = None, runtime_config: Any = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    ``include_test_routes=True`` 挂载测试专用的校验/崩溃探针（证明 422/500 接线），
    生产入口与 OpenAPI 导出都用默认 ``False``，探针不进入契约。
    ``dist_dir`` 默认指向仓库的 ``frontend/dist``；测试可注入临时目录。
    ``sessions_root`` 默认取项目配置；测试注入 ``tmp_path``，绝不碰真实会话目录。
    ``runtime_config`` 是执行阶段 1 用的配置对象（轮数、质检开关）；测试注入确定性
    桩配置，生产走项目 ``config``。它不影响 HTTP 契约，故不进 OpenAPI。
    """
    from ..workbench_store import WorkbenchSessionStore

    store = WorkbenchSessionStore(
        sessions_root if sessions_root is not None else _default_sessions_root())
    # 桩开关只从环境变量读（D4 spec §11）：默认关、无 HTTP 入口。真正的工作在
    # ``JobRunner`` 里，这里只负责把决定传下去并在启动时把它说清楚。
    fake = fake_pipeline_enabled()
    runner = JobRunner(store, runtime_config, fake_pipeline=fake)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 启动日志明示（spec §11）：桩管线开着时，用户必须能从日志知道「这次不起
        # 真实模型」。写在 lifespan 而不是工厂里：uvicorn 的日志配置在 serve 时才生效，
        # 工厂里打的 warning 会被静默吃掉（本机实测）。
        if fake:
            logger.warning(
                "[桩管线] %s 已开启：阶段 1/2 与修订跑确定性假产出（不建任何模型）；"
                "仅供 Playwright CI 与本地调试，真实运行请关掉它。", FAKE_PIPELINE_ENV)
        else:
            logger.info("[管线] 真实阶段 1/2（%s 未开启）。", FAKE_PIPELINE_ENV)
        # 启动清理（D3-2）：删崩溃残留的素材临时文件、给孤儿正本补边车。放在对账之前
        # 是因为它是纯文件清理，不依赖任何会话状态。
        await asyncio.to_thread(store.cleanup_assets)
        # 启动对账（spec §6）：上一个进程留下的 running 会话在这里收成 interrupted。
        # 文件扫描是阻塞的，放进线程池，别拖住事件循环的第一次心跳。
        await asyncio.to_thread(runner.reconcile)
        yield

    app = FastAPI(title="MiniMax H3 Prompt API", lifespan=lifespan)
    # 版本在启动阶段解析并落在 app.state：健康接口读它，而非每次请求都查元数据。
    app.state.version = _resolve_version()
    app.state.session_store = store
    app.state.job_runner = runner
    # 本地访问控制（D3-1）：每进程随机令牌 + Host/Origin 白名单。中间件在最外层，
    # 静态兜底与 /api/* 都要先过它；豁免面由 ``LocalAccessPolicy.is_exempt`` 收窄。
    attach_local_access(app)
    install_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    # 顺序是契约（#222 §9）：``{session_id:path}`` 会吞掉斜杠，``/sessions/{id}/assets``
    # 必须先于 ``GET /sessions/{session_id:path}`` 注册，否则详情路由会把 ``assets``
    # 当作会话标识的一部分。assets 路由器整只排在 sessions 之前，这条顺序不靠人记。
    app.include_router(assets_router)
    # 媒体读取（D3-3）：``{asset_id:path}`` 同样自带会话标识里的斜杠，理由同上。
    app.include_router(media_router)
    app.include_router(sessions_router)
    if include_test_routes:
        from .routes.validation_probe import router as probe_router

        app.include_router(probe_router)
    # 兜底路由必须最后注册：/openapi.json、/docs 与 /api/* 都要先匹配到自己的处理器。
    install_spa(app, dist_dir if dist_dir is not None else DEFAULT_DIST)
    # 让错误响应在契约里声明 application/problem+json（与运行时一致）。
    apply_problem_media_type_openapi(app)
    return app
