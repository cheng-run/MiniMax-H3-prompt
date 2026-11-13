"""D8-1（#261）：创建口放开 ``director`` 路线与三个打包参数（spec #253 §4.1 / §4.2 / §14）。

创建口此前只有一个字面量 ``mode: Literal["base"]``（后来放开到 ``ref``），真正的写侧
早就是三路线。这一票把门打开，并把「导演台参数只在导演台路线上有意义」做成明确的
422 而不是静默忽略——忽略的后果是用户以为填了节点 id，打包时才撞上缓存目录对不上。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from workbench_client import workbench_client


def _client(tmp_path: Path) -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    return workbench_client(create_app(sessions_root=tmp_path))


def _director_body(**overrides: object) -> dict:
    body: dict = {
        "mode": "director",
        "plot": "雨夜街口的追逐",
        "duration": 4.0,
        "director_mode": "i2v",
        "director_node_id": "node-42",
        "director_cache_name": "noir",
        "director_continuity_mode": "continue",
    }
    body.update(overrides)
    return body


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    return body


def test_director_session_is_created_with_the_three_packing_parameters(tmp_path):
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json=_director_body())

    assert resp.status_code == 201, resp.text
    brief = resp.json()["brief"]
    assert brief["mode"] == "director"
    assert brief["director_mode"] == "i2v"
    assert brief["director_node_id"] == "node-42"
    assert brief["director_cache_name"] == "noir"
    assert brief["director_continuity_mode"] == "continue"
    # 导演台的 variant 是 T2VA 占位（ADR 0024）：服务端固定落库，DTO 仍恒有该字段。
    assert brief["variant"] == "T2VA"


def test_director_variant_is_fixed_server_side(tmp_path):
    """前端隐藏 variant 之后，外部调用方塞什么都不会改变落库值。"""
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json=_director_body(variant="FL2VA"))

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["variant"] == "T2VA"


def test_continuity_mode_defaults_to_guide(tmp_path):
    client = _client(tmp_path)
    body = _director_body()
    body.pop("director_continuity_mode")

    resp = client.post("/api/v1/sessions", json=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["director_continuity_mode"] == "guide"


def test_unknown_route_is_director_mode_invalid(tmp_path):
    """v2v / rv2v / mixed 已取消（#170）：它们是**未知模式**，不是「暂不支持」。"""
    client = _client(tmp_path)

    for mode in ("v2v", "rv2v", "mixed", "Director"):
        _assert_problem(
            client.post("/api/v1/sessions", json={"mode": mode, "plot": "雨夜"}),
            422, "director_mode_invalid")


def test_unknown_director_mode_is_rejected(tmp_path):
    client = _client(tmp_path)

    for mode in ("v2v", "rv2v", "mixed", "T2V", ""):
        _assert_problem(
            client.post("/api/v1/sessions", json=_director_body(director_mode=mode)),
            422, "director_mode_invalid")


def test_missing_node_id_is_director_param_missing(tmp_path):
    client = _client(tmp_path)

    _assert_problem(
        client.post("/api/v1/sessions", json=_director_body(director_node_id="")),
        422, "director_param_missing")


def test_director_parameters_on_a_non_director_route_are_rejected(tmp_path):
    """非 director 带导演台参数要响：静默忽略会让用户以为参数生效了。"""
    client = _client(tmp_path)

    for value in ({"director_mode": "i2v"}, {"director_node_id": "node-42"},
                  {"director_cache_name": "noir"},
                  {"director_continuity_mode": "continue"}):
        _assert_problem(
            client.post("/api/v1/sessions", json={"mode": "base", "plot": "雨夜", **value}),
            422, "director_param_missing")
        _assert_problem(
            client.post("/api/v1/sessions", json={"mode": "ref", "plot": "雨夜", **value}),
            422, "director_param_missing")


def test_invalid_continuity_mode_names_the_plugin_field(tmp_path):
    """文案必须点名 ``continuityMode``：它就是要抄进导演包的那个字段名。"""
    client = _client(tmp_path)

    body = _assert_problem(
        client.post("/api/v1/sessions",
                    json=_director_body(director_continuity_mode="guidee")),
        422, "director_continuity_invalid")
    assert "continuityMode" in body["detail"]
    assert "guide" in body["detail"]


def test_base_route_still_defaults_and_keeps_its_variant(tmp_path):
    """base 一字不变：默认值、变体与三个导演台字段的空值。"""
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions",
                       json={"plot": "雨夜旧信", "variant": "FL2VA"})

    assert resp.status_code == 201, resp.text
    brief = resp.json()["brief"]
    assert brief["mode"] == "base"
    assert brief["variant"] == "FL2VA"
    assert brief["director_mode"] == ""
    assert brief["director_node_id"] == ""
    assert brief["director_cache_name"] == ""
    assert brief["director_continuity_mode"] == ""
