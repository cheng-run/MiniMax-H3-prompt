"""测试用 HTTP 客户端：回环 Host + 启动凭据（D3-1 起 `/api/*` 都要凭据）。

为什么统一成一个入口：``TestClient`` 默认 base_url 是 ``http://testserver``，D3-1 的
Host 白名单会把它当非回环拒掉；而令牌又是每个 app 各自生成的。散在各文件里手拼
这两个值，改规则时必然漏一处——那时的红会看着像业务坏了。
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

# 头名从实现派生：这里是测试基础设施，不是契约断言（契约名由 test_api_auth.py
# 用字面量钉住）。字面重抄一份，改常量时它会静默指向旧名字。
from minimax_h3_prompt.api.auth import TOKEN_HEADER


def workbench_client(
    app: Any, *, base_url: str = "http://127.0.0.1", **kwargs: Any,
) -> TestClient:
    """构造一个默认带启动令牌、Host 为回环的 TestClient。

    需要验证「缺凭据」的用例自己 ``headers.pop`` 或另建客户端。
    """
    client = TestClient(app, base_url=base_url, **kwargs)
    client.headers[TOKEN_HEADER] = app.state.local_access.token
    return client
