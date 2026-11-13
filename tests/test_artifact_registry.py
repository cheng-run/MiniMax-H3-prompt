"""#245 / D6-AC8：段提示词产物的白名单模式化。

「哪些名字算产物」只有一个来源：固定名表 + 一条模式 ``segments/shot-NN.md``。
这些用例从**外部**钉三件事——清单从目录现扫、``artifact_id`` 按最长合法后缀切分、
模式外的文件与越界标识一律 404 且不泄漏根外是否存在文件；模式本身不被误伤是
反方向的那一半（``segments/plan.json``、``segments/shot-ledger.jsonl``）。

本票不产出段提示词（没有生产者），所以这里**手写**文件充当产物。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.workbench_store import (
    ARTIFACT_FIRST_FRAME_PROMPT,
    ARTIFACT_LAST_FRAME_PROMPT,
    ARTIFACT_SCRIPT,
    ARTIFACT_SHOT_TABLE,
    ARTIFACT_SUMMARY_JSON,
    ARTIFACT_SUMMARY_MARKDOWN,
    ARTIFACT_VIDEO_PROMPT,
    WorkbenchSessionStore,
)
from workbench_client import workbench_client

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_qa=False, max_qa_iterations=0)

SEGMENT_PROMPT = "segments/shot-01.md"

# 八个既有产物的 kind：这份期望值来自 D2 §10 / D4 §7 的登记表，不从实现常量派生
# ——照着实现抄一份期望值，改错实现也照样绿。
FIXED_KINDS = {
    ARTIFACT_VIDEO_PROMPT: "video_prompt",
    ARTIFACT_SCRIPT: "script",
    ARTIFACT_SHOT_TABLE: "shot_table",
    ARTIFACT_FIRST_FRAME_PROMPT: "frame_prompt",
    ARTIFACT_LAST_FRAME_PROMPT: "frame_prompt",
    ARTIFACT_SUMMARY_MARKDOWN: "summary_markdown",
    ARTIFACT_SUMMARY_JSON: "summary_json",
    "final.mp4": "video",
}


def _brief_body() -> dict:
    return {
        "mode": "base",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
    }


@pytest.fixture
def workbench(tmp_path):
    root = tmp_path / "sessions"
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    return app, root


def _new_session(client: TestClient) -> tuple[str, Path]:
    """建会话并返回 (session_id, 会话目录)。建会话不起任务，不必桩模型。"""
    session = client.post("/api/v1/sessions", json=_brief_body())
    assert session.status_code == 201, session.text
    session_id = session.json()["session_id"]
    root = Path(client.app.state.session_store.root)
    return session_id, root / Path(session_id)


def _write_segment_prompt(directory: Path, text: str = "第 1 段：开场镜头") -> Path:
    path = directory / SEGMENT_PROMPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _encode(target: str) -> str:
    """点号百分号编码：httpx 会在发请求前把字面 ``..`` 规范化掉。

    不编码就测不到服务端的越界判定，测的是客户端自己的路径清洗。
    """
    return target.replace(".", "%2E")


# ---------------------------------------------------------------------------
# 登记、清单、读取
# ---------------------------------------------------------------------------

def test_segment_prompt_is_listed_and_readable(workbench):
    app, _root = workbench
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        _write_segment_prompt(directory, "第 1 段：雨夜街口的开场镜头")

        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        listed = client.get(f"/api/v1/sessions/{session_id}/artifacts").json()
        content = client.get(f"/api/v1/artifacts/{session_id}/{SEGMENT_PROMPT}/content")

    by_name = {item["name"]: item for item in detail["artifacts"]}
    assert SEGMENT_PROMPT in by_name
    assert by_name[SEGMENT_PROMPT] == {
        "artifact_id": f"{session_id}/{SEGMENT_PROMPT}",
        "kind": "segment_prompt",
        "name": SEGMENT_PROMPT,
        "bytes": len("第 1 段：雨夜街口的开场镜头".encode("utf-8")),
        "created_at": by_name[SEGMENT_PROMPT]["created_at"],
    }
    # 详情与清单端点同一份来源：一处有、一处没有就是两套口径。
    assert {item["name"]: item for item in listed} == by_name
    assert content.status_code == 200, content.text
    assert content.headers["content-type"].startswith("text/plain")
    assert content.text == "第 1 段：雨夜街口的开场镜头"


def test_every_fixed_artifact_keeps_its_kind(workbench):
    """八个既有产物的 kind 一件都不许动（D6-AC8 的另一半）。"""
    app, _root = workbench
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        for name in FIXED_KINDS:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00\x01" if name.endswith(".mp4") else b"x")
        _write_segment_prompt(directory)

        listed = client.get(f"/api/v1/sessions/{session_id}/artifacts").json()

    kinds = {item["name"]: item["kind"] for item in listed}
    assert kinds == {**FIXED_KINDS, SEGMENT_PROMPT: "segment_prompt"}


def test_segment_numbers_are_two_digit(workbench):
    """``shot-NN.md`` 的 NN 是两位序号（与 CLI 的 ``{:02d}`` 一致）。"""
    app, _root = workbench
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        _write_segment_prompt(directory)
        (directory / "segments" / "shot-12.md").write_text("第 12 段", encoding="utf-8")

        listed = {item["name"] for item in
                  client.get(f"/api/v1/sessions/{session_id}/artifacts").json()}

    assert listed == {SEGMENT_PROMPT, "segments/shot-12.md"}


# ---------------------------------------------------------------------------
# 模式不误伤：segments/ 下的其他文件既不出现在清单里，也读不出来
# ---------------------------------------------------------------------------

NEIGHBOURS_OF_SEGMENT_PROMPTS = (
    "segments/plan.json",
    "segments/shot-ledger.jsonl",
    "segments/progress.json",
    "segments/notes.md",
    "segments/shot-1.md",
    "segments/shot-001.md",
    "segments/shot-01.txt",
    "segments/shot-01.md.bak",
    "segments/sub/shot-01.md",
    "shot-01.md",
)


def test_files_outside_the_pattern_are_neither_listed_nor_readable(workbench):
    app, _root = workbench
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        for name in NEIGHBOURS_OF_SEGMENT_PROMPTS:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("不该被当成产物", encoding="utf-8")

        listed = {item["name"] for item in
                  client.get(f"/api/v1/sessions/{session_id}/artifacts").json()}
        responses = {
            name: client.get(f"/api/v1/artifacts/{session_id}/{name}/content")
            for name in NEIGHBOURS_OF_SEGMENT_PROMPTS
        }

    assert listed == set()
    for name, resp in responses.items():
        assert resp.status_code == 404, name
        assert resp.json()["code"] == "artifact_not_found", name
        assert "不该被当成产物" not in resp.text, name


# ---------------------------------------------------------------------------
# artifact_id 的最长合法后缀切分与越界
# ---------------------------------------------------------------------------

ESCAPING_SEGMENT_IDS = (
    "{session}/segments/../segments/shot-01.md",
    "{session}/segments/../../shot-01.md",
    "{session}/segments/sub/shot-01.md",
    "{session}//segments/shot-01.md",
    "{session}/segments\\shot-01.md",
    "/etc/segments/shot-01.md",
    "C:/x/segments/shot-01.md",
    "segments/shot-01.md",
)


@pytest.mark.parametrize("artifact_id", ESCAPING_SEGMENT_IDS)
def test_escaping_segment_ids_are_problem_404(workbench, artifact_id):
    app, root = workbench
    outside = root.parent / "secret"
    (outside / "segments").mkdir(parents=True)
    (outside / "segments" / "shot-01.md").write_text("根外机密", encoding="utf-8")
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        _write_segment_prompt(directory, "本会话的段提示词")
        target = artifact_id.format(session=session_id)
        resp = client.get(f"/api/v1/artifacts/{_encode(target)}/content")

    assert resp.status_code == 404, target
    assert resp.json()["code"] == "artifact_not_found"
    assert "根外机密" not in resp.text


def test_segment_prompt_symlinked_outside_the_root_is_404(workbench):
    app, root = workbench
    outside = root.parent / "secret-shot.md"
    outside.write_text("根外机密", encoding="utf-8")
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        (directory / "segments").mkdir(parents=True, exist_ok=True)
        link = directory / SEGMENT_PROMPT
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"本机建不了符号链接：{exc}")

        resp = client.get(f"/api/v1/artifacts/{session_id}/{SEGMENT_PROMPT}/content")
        listed = client.get(f"/api/v1/sessions/{session_id}/artifacts")

    assert resp.status_code == 404
    assert "根外机密" not in resp.text
    assert listed.json() == []


# ---------------------------------------------------------------------------
# 文本形态：1 MiB 上限原样适用；媒体形态不受影响
# ---------------------------------------------------------------------------

def test_segment_prompt_over_one_mib_is_413(workbench):
    app, _root = workbench
    limit = 1024 * 1024
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        path = _write_segment_prompt(directory, "a" * (limit + 1))
        too_large = client.get(f"/api/v1/artifacts/{session_id}/{SEGMENT_PROMPT}/content")

        path.write_text("a" * limit, encoding="utf-8")
        at_limit = client.get(f"/api/v1/artifacts/{session_id}/{SEGMENT_PROMPT}/content")

    assert too_large.status_code == 413
    assert too_large.json()["code"] == "artifact_too_large"
    assert at_limit.status_code == 200
    assert len(at_limit.text) == limit


def test_segment_prompt_is_text_while_final_mp4_stays_media(workbench):
    """形态判定也走同一张登记表：段提示词是文本，成片仍按媒体读。"""
    app, root = workbench
    with workbench_client(app) as client:
        session_id, directory = _new_session(client)
        _write_segment_prompt(directory)
        (directory / "final.mp4").write_bytes(bytes(range(64)))

        segment = client.get(f"/api/v1/artifacts/{session_id}/{SEGMENT_PROMPT}/content")
        film = client.get(f"/api/v1/artifacts/{session_id}/final.mp4/content")

    assert segment.headers["content-type"].startswith("text/plain")
    assert "accept-ranges" not in segment.headers
    assert film.status_code == 200
    assert film.headers["content-type"] == "video/mp4"
    assert film.headers["accept-ranges"] == "bytes"
    assert film.content == bytes(range(64))


# ---------------------------------------------------------------------------
# 写入口与登记入口（D6 的执行器将来要靠它落段提示词）
# ---------------------------------------------------------------------------

def test_store_can_write_and_register_a_segment_prompt(tmp_path):
    store = WorkbenchSessionStore(tmp_path)
    detail, _replayed = store.create(
        Brief(mode="base", variant="T2VA", duration=4.0, plot="雨夜旧信"),
        fingerprint="body-1",
    )

    info = store.write_text_artifact(detail.session_id, SEGMENT_PROMPT, "第 1 段")

    assert info.artifact_id == f"{detail.session_id}/{SEGMENT_PROMPT}"
    assert info.kind == "segment_prompt"
    assert store.is_media_artifact(info.artifact_id) is False
    assert store.read_artifact_text(info.artifact_id)[1] == "第 1 段"
    with pytest.raises(ValueError):
        store.write_text_artifact(detail.session_id, "segments/plan.json", "{}")
