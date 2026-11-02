"""出网守卫的变异检验（retro 2026-10-30 候选 2）。

`tests/conftest.py` 的 ``no_real_network`` 是 autouse 守卫，但「装了守卫」和「守卫真的会
咬」是两件事：把 monkeypatch 那三行删掉，任何依赖它的用例都不会变红（本仓反复记过的
「永不为红＝没有守卫」）。所以这里直接验证两个方向：

1. 默认情形下真实建连被拒（RuntimeError，且消息点名了放行方式）；
2. 标了 ``allow_network`` 时补丁确实不生效（拿到的必须是**系统层**错误，不是守卫错误）。

IP 刻意用 ``203.0.113.0/24``（TEST-NET-3，协议规定不可路由）与 ``127.0.0.1:1``：
前者保证即使守卫失效也不会真的连出去，后者保证放行分支不需要外部网络就能拿到 OSError。
"""
from __future__ import annotations

import socket

import pytest


def test_default_refuses_real_connect():
    """默认必须被守卫拦下，且错误消息要告诉调用者怎么放行。"""
    with pytest.raises(RuntimeError, match="禁止真实网络连接"):
        socket.create_connection(("203.0.113.1", 9), timeout=0.1)


def test_default_refuses_socket_connect():
    """socket 对象直接被调时同样拦（httpx/requests 底下走的就是这条）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="禁止真实网络连接"):
            sock.connect(("203.0.113.1", 9))
    finally:
        sock.close()


@pytest.mark.allow_network
def test_marker_disables_the_guard():
    """放行标记生效时，失败必须来自操作系统（端口没人听），而不是守卫。"""
    with pytest.raises(OSError) as excinfo:
        socket.create_connection(("127.0.0.1", 1), timeout=0.1)
    assert not isinstance(excinfo.value, RuntimeError), (
        "allow_network 没生效：守卫仍在拦截"
    )
