"""D2-1（#215）：会话 API——建、列、详情、legacy 只读、幂等与契约。

HTTP 形状是这张票的验收面（D2-AC1 / AC4 建会话部分 / AC12 / AC13 / AC14）。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from minimax_h3_prompt.session_store import LEGACY_SCHEMA_VERSION, SESSION_FILENAME
from workbench_client import workbench_client


def _client(tmp_path: Path) -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    return workbench_client(create_app(sessions_root=tmp_path))


def _brief_body(plot: str = "雨夜旧信") -> dict:
    return {
        "mode": "base",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": plot,
    }


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    return body


def _write_legacy(root: Path, session_id: str = "旧主题/GEN001") -> Path:
    directory = root / Path(session_id)
    directory.mkdir(parents=True)
    (directory / SESSION_FILENAME).write_text(json.dumps({
        "schema_version": LEGACY_SCHEMA_VERSION,
        "status": "completed",
        "brief": {"mode": "base", "variant": "T2VA", "duration": 4.0,
                  "plot": "旧主题"},
        "stage_state": {"script": "旧剧本"},
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:10:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")
    (directory / "video-prompt.md").write_text("旧提示词", encoding="utf-8")
    return directory


def test_create_session_returns_detail_and_writes_a_file(tmp_path):
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json=_brief_body())

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "created"
    assert body["legacy"] is False
    assert body["revision"] == 1
    assert body["session_id"].endswith("/GEN001")
    assert body["brief"]["plot"] == "雨夜旧信"
    assert body["artifacts"] == []
    assert body["current_job"] is None
    on_disk = tmp_path / body["session_id"] / SESSION_FILENAME
    assert on_disk.is_file()
    assert json.loads(on_disk.read_text(encoding="utf-8"))["schema_version"] == "workbench.v1"


def test_create_with_idempotency_key_replays_the_first_response(tmp_path):
    client = _client(tmp_path)
    headers = {"Idempotency-Key": "create-1"}

    first = client.post("/api/v1/sessions", json=_brief_body(), headers=headers)
    second = client.post("/api/v1/sessions", json=_brief_body(), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["idempotency-replayed"] == "true"
    assert second.json()["session_id"] == first.json()["session_id"]
    slug_dir = next(p for p in tmp_path.iterdir() if p.is_dir() and p.name != ".workbench")
    assert [p.name for p in slug_dir.iterdir() if p.is_dir()] == ["GEN001"]


def test_same_key_with_a_different_body_is_a_conflict(tmp_path):
    client = _client(tmp_path)
    headers = {"Idempotency-Key": "create-1"}
    client.post("/api/v1/sessions", json=_brief_body("甲"), headers=headers)

    resp = client.post("/api/v1/sessions", json=_brief_body("乙"), headers=headers)

    _assert_problem(resp, 409, "idempotency_conflict")


def test_list_sessions_includes_legacy_and_paginates(tmp_path):
    client = _client(tmp_path)
    _write_legacy(tmp_path)
    created = client.post("/api/v1/sessions", json=_brief_body()).json()

    resp = client.get("/api/v1/sessions", params={"limit": 1})

    assert resp.status_code == 200
    page = resp.json()
    assert len(page["items"]) == 1
    assert page["next_cursor"]
    rest = client.get("/api/v1/sessions",
                      params={"limit": 10, "cursor": page["next_cursor"]}).json()
    ids = [item["session_id"] for item in page["items"] + rest["items"]]
    assert set(ids) == {created["session_id"], "旧主题/GEN001"}
    legacy = next(item for item in page["items"] + rest["items"]
                  if item["session_id"] == "旧主题/GEN001")
    assert legacy["legacy"] is True and legacy["status"] == "completed"


def test_get_session_by_url_encoded_path(tmp_path):
    client = _client(tmp_path)
    created = client.post("/api/v1/sessions", json=_brief_body()).json()
    encoded = quote(created["session_id"], safe="")

    resp = client.get(f"/api/v1/sessions/{encoded}")

    assert resp.status_code == 200
    assert resp.json()["session_id"] == created["session_id"]


def test_get_unknown_session_is_problem_404(tmp_path):
    resp = _client(tmp_path).get("/api/v1/sessions/不存在/GEN001")

    _assert_problem(resp, 404, "session_not_found")


def test_legacy_session_jobs_returns_problem_409(tmp_path):
    client = _client(tmp_path)
    _write_legacy(tmp_path)

    resp = client.post("/api/v1/sessions/旧主题/GEN001/jobs")

    body = _assert_problem(resp, 409, "session_legacy")
    assert "legacy" in body["detail"].lower() or "历史" in body["detail"]


def test_create_rejects_non_base_routes_and_bad_video_output(tmp_path):
    client = _client(tmp_path)

    other_route = client.post("/api/v1/sessions", json={**_brief_body(), "mode": "ref"})
    assert other_route.status_code == 422

    bad_output = client.post("/api/v1/sessions", json={
        **_brief_body(),
        "video_output": {"ratio": "5:6", "megapixels": 0.4, "multiple": 32},
    })
    assert bad_output.status_code == 422
    assert bad_output.headers["content-type"].startswith("application/problem+json")


# ---------------------------------------------------------------------------
# 时长范围（D4 spec §5 / 本票 D4-AC1 的服务端一半）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("duration", [3.9, 60.1, 0, 61])
def test_create_session_rejects_duration_out_of_scope(tmp_path, duration):
    """越界两端都回 422 + 专用码；文案给出**新的** 4–60 范围（D6-AC1）。

    文案不再点名「长片归 D6」：那张票已经落地，长片就在同一个表单里。
    """
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json={**_brief_body(), "duration": duration})

    body = _assert_problem(resp, 422, "duration_out_of_scope")
    assert "4–60" in body["detail"]
    assert "D6" not in body["detail"]
    assert not list(tmp_path.glob("*/GEN*")), "越界请求不该建出会话目录"


@pytest.mark.parametrize("duration", [4, 10, 12, 60])
def test_create_session_accepts_duration_at_the_bounds(tmp_path, duration):
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json={**_brief_body(), "duration": duration})

    assert resp.status_code == 201, resp.text
    assert resp.json()["brief"]["duration"] == duration


def test_contract_declares_sessions_and_problem_responses():
    from minimax_h3_prompt.api.app import create_app

    schema = create_app().openapi()
    paths = schema["paths"]
    assert "/api/v1/sessions" in paths
    assert "get" in paths["/api/v1/sessions"] and "post" in paths["/api/v1/sessions"]
    detail_path = "/api/v1/sessions/{session_id}"
    assert detail_path in paths
    assert "SessionDetail" in schema["components"]["schemas"]
    assert "SessionPage" in schema["components"]["schemas"]

    jobs = paths["/api/v1/sessions/{session_id}/jobs"]["post"]["responses"]
    assert "202" in jobs
    assert "409" in jobs
    assert "application/problem+json" in jobs["409"]["content"]
    job_path = "/api/v1/sessions/{session_id}/jobs/{job_id}"
    assert "get" in paths[job_path]
    cancel_path = job_path + "/cancel"
    assert "post" in paths[cancel_path]
    assert "JobSummary" in schema["components"]["schemas"]

    create_responses = paths["/api/v1/sessions"]["post"]["responses"]
    for code in ("200", "201", "409", "422", "500"):
        assert code in create_responses
    assert "application/problem+json" in create_responses["409"]["content"]
    create_params = paths["/api/v1/sessions"]["post"].get("parameters", [])
    assert any(param["name"] == "Idempotency-Key" and param["in"] == "header"
               for param in create_params), "幂等请求头没有进契约"
    assert "Idempotency-Replayed" in create_responses["200"]["headers"]
