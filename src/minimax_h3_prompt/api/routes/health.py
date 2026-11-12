"""健康检查接口：``GET /api/v1/health``。

返回裸资源（不套 ``data``），带 ``Cache-Control: no-store``——健康状态不该被任何
中间层缓存。版本号由 app 在启动时从包元数据解析后放进 ``app.state``，路由只读，
不再重复解析（读不到时 ``create_app`` 会先失败，见 ``api/app.py``）。

**D5-3 的加法扩展**（#238）：``data_root`` / ``config_path`` / ``env_path`` /
``missing_keys`` 让概览页能告诉用户「去哪个文件配密钥」。字面上破了 spec #235 §13
「其余端点不变」——这是**用户 2026-11-12 明确裁定的例外**（spec §7 要求概览页显示
密钥文件的绝对路径与格式，又不新增端点，只能从健康接口读）。四个字段**只读**，
``missing_keys`` 只给变量名、绝不含密钥值（spec §7 脱敏红线）。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .. import SERVICE_NAME
from ..problems import COMMON_ERROR_RESPONSES
from ... import paths

router = APIRouter(prefix="/api/v1", tags=["health"])


class HealthResponse(BaseModel):
    """健康状态资源。字段约定见 D1 spec §7.1；后四个见 #238 的裁定说明。"""

    status: Literal["ok"]
    service: str
    version: str
    # 可写数据根（冻结态 = EXE 同级 data/，开发态 = 仓库根，--data-root 可覆盖）。
    data_root: str
    # 生效的 agent.yaml 绝对路径：用户要改配置时来这找。
    config_path: str
    # 密钥文件绝对路径（冻结态在 %APPDATA%）：概览页要原样显示给用户。
    env_path: str
    # 当前缺失的密钥**变量名**（只有名字）。空列表 = 两档模型都能解析出 key。
    missing_keys: list[str]


@router.get("/health", response_model=HealthResponse, responses=COMMON_ERROR_RESPONSES)
def get_health(request: Request, response: Response) -> HealthResponse:
    # 延迟 import（与 ``api/app.py::_default_sessions_root`` 同一形态）：顶层 import 会让
    # ``config = Config()`` 单例在**模块 import 期**就建出来，而它的路径取决于当时的
    # ``paths`` 状态（``--data-root`` / 冻结标记）。入口的时序契约是「argv 先于业务
    # import」，多一处 import 期副作用就多一个会静默错位的点。
    from ...config import config as runtime_config

    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=request.app.state.version,
        data_root=str(paths.data_root()),
        config_path=str(paths.config_path()),
        env_path=str(paths.env_file_path()),
        missing_keys=runtime_config.missing_api_keys(),
    )
