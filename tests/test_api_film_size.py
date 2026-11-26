"""工作台画布 T4（#367）：概览能读到的画布行与成片尺寸**声明值**。

判据在 HTTP 面上，两条数据都从会话详情取：

- ``brief.canvases``：本次会话的画布（逐档），与创建请求落库值一致；
- ``brief.canvas_lines``：上面那张表的显示行（家族显示名只有服务端一份）；
- ``film_size``：成片尺寸的**声明值**，不是实测值（CONTEXT「画布」「成片画幅」
  「视频输出」三节）。

推导值**直接与既有推导函数比对**（票面 AC）：导演台读
``director.film_canvas.film_size_for``，base/ref 读注入卡的
``injection_card.resolve_video_output``——用例不重算一遍，也不把 1920×1088 之类
的金值抄进断言（那等于在前端另写一份算法）。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from minimax_h3_prompt.session_store import SESSION_FILENAME
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


def _ref_body(**overrides: object) -> dict:
    return _base_body(mode="ref", **overrides)


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


def _detail(client: TestClient, created: dict) -> dict:
    """取详情（会话标识含斜杠，逐段编码后仍保留路径分隔）。"""
    encoded = quote(created["session_id"], safe="")
    resp = client.get(f"/api/v1/sessions/{encoded}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _canvases_of(detail: dict) -> dict:
    """详情里的画布表转成推导函数的入参形状（列表 → 元组，值原样）。"""
    return {family: tuple(size) for family, size in detail["brief"]["canvases"].items()}


def _expected_injection_size(detail: dict) -> tuple[int, int]:
    from minimax_h3_prompt.injection_card import resolve_video_output

    derivation = resolve_video_output(None, _canvases_of(detail))
    return derivation.params.size


def _pin_frame_model(monkeypatch, model: str) -> None:
    """钉住关键帧模型：导演台的成片画幅键跟随它，测试不能跟着本机配置跑。"""
    from minimax_h3_prompt import image_config

    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: model)


# ---------------------------------------------------------------------------
# 画布行：值与 brief.canvases 一致
# ---------------------------------------------------------------------------

def test_ref_session_echoes_the_canvas_and_its_display_line(tmp_path):
    """ref 会话：画布行读 ``brief.canvases``；显示行带家族显示名（服务端渲染）。"""
    from minimax_h3_prompt.generation import IMAGE_FAMILY_LABELS

    client = _client(tmp_path)
    created = client.post(
        "/api/v1/sessions",
        json=_ref_body(canvas={"width": 1080, "height": 1920})).json()
    detail = _detail(client, created)

    canvases = detail["brief"]["canvases"]
    assert canvases, "选了画布就有值"
    lines = detail["brief"]["canvas_lines"]
    assert lines == [f"{IMAGE_FAMILY_LABELS[family]}：{width}×{height}"
                     for family, (width, height) in canvases.items()]


def test_session_without_a_canvas_has_no_display_lines(tmp_path):
    """没选画布：``canvas_lines`` 是空表——「未选，按各档默认」由前端照实说，
    不在后端编一句假数据。"""
    client = _client(tmp_path)
    detail = _detail(client, client.post("/api/v1/sessions", json=_ref_body()).json())

    assert detail["brief"]["canvases"] == {}
    assert detail["brief"]["canvas_lines"] == []


# ---------------------------------------------------------------------------
# 成片尺寸声明值：base / ref 走注入卡口径
# ---------------------------------------------------------------------------

def test_base_session_declares_the_injection_card_size(tmp_path):
    """base 不产注入卡：概览这一行是它唯一能看到成片尺寸声明的地方。

    值与 ``resolve_video_output`` 的推导逐位相等——不管推导源是哪一档，用例都
    调那份函数取期望值，不重算。
    """
    client = _client(tmp_path)
    detail = _detail(client, client.post("/api/v1/sessions", json=_base_body()).json())

    film = detail["film_size"]
    assert film is not None
    assert film["source"] == "video_output"
    assert (film["width"], film["height"]) == _expected_injection_size(detail)


def test_ref_session_film_size_follows_the_same_derivation(tmp_path):
    """ref 选了 Z-Image 画布也不改推导源（#146 的「不许跨档继承」）：仍读同一份函数。"""
    from minimax_h3_prompt.injection_card import resolve_video_output

    client = _client(tmp_path)
    detail = _detail(client, client.post(
        "/api/v1/sessions",
        json=_ref_body(canvas={"width": 1080, "height": 1920})).json())

    derivation = resolve_video_output(None, _canvases_of(detail))
    film = detail["film_size"]
    assert film == {
        "width": derivation.params.size[0],
        "height": derivation.params.size[1],
        "source": "video_output",
        "canvas": list(derivation.canvas) if derivation.canvas else None,
        "notes": list(derivation.notes),
    }


def test_explicit_video_output_wins_over_the_derivation(tmp_path):
    """brief 显式声明优先（issue #49）：声明分支没有推导源画布。"""
    from minimax_h3_prompt.injection_card import VideoOutputParams

    client = _client(tmp_path)
    created = client.post("/api/v1/sessions", json=_ref_body(
        video_output={"ratio": "9:16 (Portrait Widescreen)",
                      "megapixels": 0.4, "multiple": 32})).json()
    detail = _detail(client, created)

    expected = VideoOutputParams("9:16 (Portrait Widescreen)", 0.4, 32).size
    film = detail["film_size"]
    assert (film["width"], film["height"]) == expected
    assert film["canvas"] is None
    assert film["notes"] == []


# ---------------------------------------------------------------------------
# 成片尺寸声明值：director 走成片画幅
# ---------------------------------------------------------------------------

def test_director_film_size_is_the_film_aspect_answer(tmp_path, monkeypatch):
    """导演台：值＝成片画幅（向导唯一问一次的那个答案），与 ``film_size_for`` 相等。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.director.film_canvas import film_size_for

    _pin_frame_model(monkeypatch, "qwen")
    client = _client(tmp_path)
    detail = _detail(client, client.post(
        "/api/v1/sessions",
        json=_director_body(canvas={"width": 1920, "height": 1080})).json())

    brief = Brief(mode="director", variant="T2VA", director_mode="fl2v",
                  duration=4.0, plot="雨夜街口的追逐",
                  canvases=_canvases_of(detail))
    expected = film_size_for(brief)
    assert expected is not None, "选了成片画幅就该读得到"
    film = detail["film_size"]
    assert (film["width"], film["height"]) == expected
    assert film["source"] == "film_aspect"
    assert film["canvas"] is None


def test_director_session_without_an_answer_has_no_film_size(tmp_path, monkeypatch):
    """旧会话（没有成片画幅这一问的答案）→ ``None``，而不是拿配置兜底值冒充答案。"""
    _pin_frame_model(monkeypatch, "qwen")
    client = _client(tmp_path)
    detail = _detail(client, client.post(
        "/api/v1/sessions", json=_director_body()).json())

    assert detail["film_size"] is None


# ---------------------------------------------------------------------------
# 只读与契约
# ---------------------------------------------------------------------------

def test_detail_reads_do_not_change_the_session(tmp_path):
    """新字段只读：连读两次，快照逐字相同、目录里不多东西。"""
    client = _client(tmp_path)
    created = client.post("/api/v1/sessions", json=_ref_body()).json()

    first = _detail(client, created)
    second = _detail(client, created)

    assert first == second
    directory = tmp_path / Path(created["session_id"])
    assert list(directory.glob("GEN*")) == []


def test_broken_canvas_degrades_instead_of_failing_the_detail(tmp_path):
    """画布非法时详情仍 200、``film_size`` 为 ``None``（同 ``director_notes`` 的降级口径）。

    推导函数本身**照旧大声报错**（这里钉的是详情端点不吃那颗雷）：否则一张坏画布
    会让整页打不开，用户连待办都处理不了。

    坏值必须落在这条路线**真正读的那一档**上（base/ref 读人物档画布，见
    ``resolve_video_output`` 的说明）：落在 Z-Image 那格推导根本不会碰它，
    那样测到的只是「详情忽略无关的坏值」。
    """
    from minimax_h3_prompt.image_config import image_model_for

    client = _client(tmp_path)
    created = client.post("/api/v1/sessions", json=_base_body()).json()
    session_file = tmp_path / Path(created["session_id"]) / SESSION_FILENAME
    raw = json.loads(session_file.read_text(encoding="utf-8"))
    raw["brief"]["canvases"] = {image_model_for("character"): [0, -1]}
    session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    detail = _detail(client, created)
    assert detail["film_size"] is None


def test_contract_declares_the_film_size_and_canvas_lines(tmp_path):
    """契约里要能看到新字段（前端类型由它生成）。"""
    from minimax_h3_prompt.api.app import create_app

    schema = create_app().openapi()["components"]["schemas"]
    assert "FilmSizeOut" in schema
    assert "film_size" in schema["SessionDetail"]["properties"]
    assert "canvas_lines" in schema["BriefOut"]["properties"]
