"""工作台画布 T1（#364）：创建口的 ``canvas`` 契约与比例档只读端点。

判据全在 HTTP 面上：创建请求照路线展开后的 ``brief.canvases``、两字段同给时的 422、
以及 ``GET /api/v1/canvas-presets`` 的响应体与后端常量逐条相等。**期望值从常量现取**
（票面 AC：用例直接比对常量，不从实现反推），端点自己再抄一份表这里就会红。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from workbench_client import workbench_client


def _client(tmp_path: Path) -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    return workbench_client(create_app(sessions_root=tmp_path))


def _base_body(**overrides: object) -> dict:
    body: dict = {
        "mode": "base",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
    }
    body.update(overrides)
    return body


def _director_body(**overrides: object) -> dict:
    body: dict = {
        "mode": "director",
        "plot": "雨夜街口的追逐",
        "duration": 4.0,
        "director_mode": "fl2v",
        "director_node_id": "node-42",
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


# ---------------------------------------------------------------------------
# 创建请求：``canvas`` 按路线展开
# ---------------------------------------------------------------------------

def test_without_canvas_the_canvases_map_stays_empty(tmp_path):
    """省略 ``canvas`` 与现状逐字一致：空表，消费方回落各档默认。"""
    resp = _client(tmp_path).post("/api/v1/sessions", json=_base_body())

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["canvases"] == {}


def test_ref_route_lands_the_canvas_on_the_image_family(tmp_path):
    """ref 路线：``canvas`` 落生图侧画布家族（``CANVAS_CHOICE_FAMILIES``）。"""
    from minimax_h3_prompt.generation import CANVAS_CHOICE_FAMILIES

    resp = _client(tmp_path).post(
        "/api/v1/sessions",
        json=_base_body(mode="ref", canvas={"width": 1080, "height": 1920}))

    assert resp.status_code == 201, resp.text
    canvases = resp.json()["brief"]["canvases"]
    assert sorted(canvases) == sorted(CANVAS_CHOICE_FAMILIES)
    assert all(size == [1080, 1920] for size in canvases.values())


def test_director_route_snaps_the_canvas_and_uses_the_frame_family(tmp_path, monkeypatch):
    """导演台路线：``canvas`` 是成片画幅，按 32 网格对齐、键落在当前关键帧模型家族上。"""
    from minimax_h3_prompt import image_config

    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "qwen")

    resp = _client(tmp_path).post(
        "/api/v1/sessions",
        json=_director_body(canvas={"width": 1920, "height": 1080}))

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["canvases"] == {"qwen": [1920, 1088]}


def test_director_route_key_follows_the_configured_frame_model(tmp_path, monkeypatch):
    """换关键帧模型后键跟着换：不许把上一档的尺寸硬套过来（#144 D）。"""
    from minimax_h3_prompt import image_config

    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "moss")

    resp = _client(tmp_path).post(
        "/api/v1/sessions",
        json=_director_body(canvas={"width": 640, "height": 1280}))

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["canvases"] == {"moss": [640, 1280]}


def test_base_route_ignores_the_canvas_without_erroring(tmp_path):
    """base 路线当前不读这个答案：按普通省略处理，不报错（spec「Out of Scope」）。"""
    resp = _client(tmp_path).post(
        "/api/v1/sessions",
        json=_base_body(canvas={"width": 1920, "height": 1080}))

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["canvases"] == {}


def test_canvas_and_non_empty_canvases_together_are_a_conflict(tmp_path):
    """两种写法同时给内容 → 422，报文说清二选一，且不建会话目录。"""
    resp = _client(tmp_path).post(
        "/api/v1/sessions",
        json=_base_body(mode="ref",
                        canvas={"width": 1080, "height": 1920},
                        canvases={"z_image": [640, 1280]}))

    body = _assert_problem(resp, 422, "canvas_conflict")
    assert "二选一" in body["detail"]
    assert not list(tmp_path.glob("*/GEN*")), "冲突请求不该建出会话目录"


def test_empty_canvases_map_does_not_conflict_with_canvas(tmp_path):
    """空表与省略同义（同 ``clarification_pairs`` 的口径），不是「给了两种写法」。"""
    resp = _client(tmp_path).post(
        "/api/v1/sessions",
        json=_base_body(mode="ref",
                        canvas={"width": 1080, "height": 1920},
                        canvases={}))

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["canvases"] == {"z_image": [1080, 1920]}


def test_canvas_sides_must_be_positive(tmp_path):
    """宽高是正整数：0 与负数走通用请求校验 422。"""
    client = _client(tmp_path)

    for size in ({"width": 0, "height": 1080}, {"width": 1920, "height": -32}):
        resp = client.post("/api/v1/sessions",
                           json=_base_body(mode="ref", canvas=size))
        assert resp.status_code == 422, resp.text
        assert not list(tmp_path.glob("*/GEN*"))


# ---------------------------------------------------------------------------
# 只读端点：比例档与兜底画布
# ---------------------------------------------------------------------------

def test_canvas_presets_match_the_backend_constants(tmp_path):
    """比例档与后端常量逐条相等；预设尺寸逐档投影，不从实现反推。"""
    from minimax_h3_prompt.generation import CANVAS_RATIO_PRESETS, CANVAS_CHOICE_FAMILIES

    resp = _client(tmp_path).get("/api/v1/canvas-presets")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected = [
        {
            "name": preset.name,
            "w_ratio": preset.w_ratio,
            "h_ratio": preset.h_ratio,
            "sizes": [{"width": width, "height": height}
                      for width, height in preset.sizes],
        }
        for preset in CANVAS_RATIO_PRESETS
    ]
    assert body["presets"] == expected
    assert body["canvas_choice_families"] == list(CANVAS_CHOICE_FAMILIES)


def test_canvas_presets_carry_the_plugin_grid_align(tmp_path):
    """网格对齐值随档表下发，值取自 ``director.pack.CANVAS_ALIGN``（#363 复审 A4）。

    前端拿它算「实际执行尺寸」，不再自己写一份 32 的字面量——这条比对常量，
    端点若写死数这里就红。
    """
    from minimax_h3_prompt.director.pack import CANVAS_ALIGN

    body = _client(tmp_path).get("/api/v1/canvas-presets").json()

    assert body["align"] == CANVAS_ALIGN


def test_canvas_presets_carry_each_models_fallback_canvas(tmp_path):
    """兜底画布与 ``IMAGE_PROFILE_DEFAULTS`` 一致（含显示名，前端不另抄一张对照表）。"""
    from minimax_h3_prompt.generation import IMAGE_FAMILY_LABELS, IMAGE_PROFILE_DEFAULTS

    body = _client(tmp_path).get("/api/v1/canvas-presets").json()

    expected = [
        {
            "model": family,
            "label": IMAGE_FAMILY_LABELS[family],
            "width": profile.width,
            "height": profile.height,
        }
        for family, profile in IMAGE_PROFILE_DEFAULTS.items()
    ]
    assert body["fallback_canvases"] == expected


def test_canvas_presets_is_read_only(tmp_path):
    """只读：不建会话、不落盘——连会话根目录都不该被创建。"""
    client = _client(tmp_path)

    resp = client.get("/api/v1/canvas-presets")

    assert resp.status_code == 200, resp.text
    assert client.get("/api/v1/sessions").json()["items"] == []
    assert not list(tmp_path.glob("*/GEN*"))


def test_contract_declares_the_canvas_presets_endpoint(tmp_path):
    """契约里要能看到这个端点与创建请求的新字段（前端类型由它生成）。"""
    from minimax_h3_prompt.api.app import create_app

    schema = create_app().openapi()
    assert "/api/v1/canvas-presets" in schema["paths"]
    assert "get" in schema["paths"]["/api/v1/canvas-presets"]
    create = schema["components"]["schemas"]["SessionCreate"]
    assert "canvas" in create["properties"]
    assert "CanvasPresetsOut" in schema["components"]["schemas"]
