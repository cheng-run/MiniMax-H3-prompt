"""D8-3（#263）：导演包 job、归档形态与逐段简报（spec #253 §9 / §8.5 / §13）。

判据分四层：

1. **归档产物形态**：``director/initial.mmxpack.zip`` 登记为 ``director_pack``
   （``application/zip``），与文本/媒体共用同一条 content 端点与单段 Range 语义；
2. **打包 job**：阶段 2 成功自动接上 ``pack`` kind；约束不过即失败点名，不落半份包；
   zip 已在时续跑不重建，只补缺失的简报；
3. **逐段简报**：``director/briefing-NN.md`` 含 ``runSelection`` 只勾第 N 段与预期帧数；
4. **上传 zip 与安全解包**：白名单 512 MiB、魔数嗅探；绝对路径/``..``/符号链接拒绝。

模型桩只接导演台规划/写段与阶段 1；打包本身是真实代码路径（不建模型）。
"""
from __future__ import annotations

import io
import json
import stat
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from minimax_h3_prompt import model_factory, workbench_store
from minimax_h3_prompt.api import jobs as jobs_module
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.director.pack import (
    DirectorPackSettings,
    pack_dir_for,
    zip_path_for,
)
from minimax_h3_prompt.director.planning import plan_path, read_plan_json
from minimax_h3_prompt.workbench_store import (
    ArchiveInvalid,
    extract_archive_zip,
    _sniff_asset,
)
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client
from workbench_deterministic import DEFAULT_REPLIES, DeterministicChatModel

ZIP_MAGIC = b"PK\x03\x04"
FL2VA_BUNDLE = {
    "prompt_node_id": "187",
    "scene_anchor": "雨夜老城街口，霓虹灯在积水里反光",
    "continuity_constraints": ["人物服装与道具在画面内保持一致"],
    "first": {
        "positive_prompt": "雨夜老城街口，撑黑伞的成年女性站在积水前，中景，冷蓝霓虹侧逆光",
        "composition": "中景，人物居中偏左",
        "lighting": "冷蓝霓虹侧逆光",
    },
    "last": {
        "positive_prompt": "雨夜老城街口，撑黑伞的女性走远，积水倒映霓虹，远景，冷蓝主调",
        "composition": "远景，人物偏右",
        "lighting": "冷蓝霓虹顶光",
    },
}


def _png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 40, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def _runtime(tmp_path: Path) -> SimpleNamespace:
    """阶段 1 的桩配置 + 打包要用的导演台配置（不起真实客户端）。"""
    return SimpleNamespace(
        roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=1,
        director_settings=lambda: DirectorPackSettings(
            width=736, height=1280, ref_max_size=864, ref_image_size="match"),
        director_comfy_output_root=lambda: tmp_path / "comfy-output",
    )


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    model = DeterministicChatModel(replies={
        **DEFAULT_REPLIES,
        "frame_prompt_engineer": json.dumps(FL2VA_BUNDLE, ensure_ascii=False),
    })
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: model)
    monkeypatch.setattr(jobs_module, "build_chat_model", lambda: model)
    root = tmp_path / "sessions"
    runtime = _runtime(tmp_path)
    app = create_app(sessions_root=root, runtime_config=runtime)
    return app, model, root, runtime


def _director_body(mode: str = "t2v", duration: float = 4.0) -> dict:
    return {
        "mode": "director",
        "plot": "雨夜街口的追逐",
        "duration": duration,
        "director_mode": mode,
        "director_node_id": "node-42",
        "canvases": {},
    }


def _create(client, mode: str = "t2v") -> str:
    resp = client.post("/api/v1/sessions", json=_director_body(mode))
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _detail(client, session_id: str) -> dict:
    return client.get(f"/api/v1/sessions/{session_id}").json()


def _pending(detail: dict, kind: str) -> list[dict]:
    return [item for item in detail["action_items"]
            if item["kind"] == kind and item["status"] == "pending"]


def _wait_job(client, session_id: str, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(
            f"/api/v1/sessions/{session_id}/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")


def _wait_idle(client, session_id: str, timeout: float = 60.0) -> dict:
    """等到没有 running 任务：阶段 2 收尾会把打包 job 接在同一次落盘里。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = _detail(client, session_id)
        if detail["status"] != "running" and not any(
                job["status"] == "running" for job in detail["jobs"]):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"会话没有在 {timeout}s 内停下：{session_id}")


def _run_to_review(client, session_id: str) -> dict:
    started = client.post(f"/api/v1/sessions/{session_id}/jobs")
    assert started.status_code == 202, started.text
    job = _wait_job(client, session_id, started.json()["job_id"])
    assert job["status"] == "succeeded", job
    review = _pending(_detail(client, session_id), "stage1_review")
    assert len(review) == 1
    return review[0]


def _run_through_pack(client, session_id: str, review: dict) -> dict:
    approved = client.post(
        f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
        json={"decision": "approve"})
    assert approved.status_code == 202, approved.text
    job = approved.json()
    assert job is not None
    body = _wait_job(client, session_id, job["job_id"])
    assert body["status"] == "succeeded", body
    return _wait_idle(client, session_id)


# ---------------------------------------------------------------------------
# 产物形态与上传白名单（纯登记表/嗅探层，先钉住再动执行器）
# ---------------------------------------------------------------------------

def test_initial_pack_and_briefing_are_registered_artifacts():
    pack = workbench_store.artifact_spec("director/initial.mmxpack.zip")
    assert pack is not None
    assert pack.kind == "director_pack"
    assert pack.media_type == "application/zip"

    briefing = workbench_store.artifact_spec("director/briefing-01.md")
    assert briefing is not None
    assert briefing.kind == "director_briefing"
    assert briefing.media_type is None  # 文本形态：与段词同一条 1 MiB 上限

    assert (workbench_store.ARTIFACT_MEDIA_TYPES
            ["director/initial.mmxpack.zip"] == "application/zip")


@pytest.mark.parametrize(("head", "expected"), [
    (b"PK\x03\x04" + b"\x00" * 8, ("zip", "zip")),
    (b"PK\x05\x06" + b"\x00" * 18, ("zip", "zip")),
])
def test_zip_magic_is_sniffed(head, expected):
    assert _sniff_asset(head) == expected


def test_zip_limit_matches_video_tier():
    assert workbench_store.ASSET_KIND_LIMITS["zip"] == 512 * 1024 * 1024
    assert workbench_store._ASSET_FORMATS["zip"] == ("zip", "zip")
    assert workbench_store.ASSET_MEDIA_TYPES["zip"] == "application/zip"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def _symlink_zip(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


@pytest.mark.parametrize("name", ["../escape.txt", "/abs.txt", "C:/abs.txt"])
def test_extract_archive_rejects_escaping_names(tmp_path, name):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_zip_bytes({name: b"x"}))
    with pytest.raises(ArchiveInvalid) as excinfo:
        extract_archive_zip(archive, tmp_path / "out")
    assert excinfo.value.code == "archive_invalid"
    assert not (tmp_path / "escape.txt").exists()


def test_archive_member_guard_rejects_backslash_names():
    """Windows 把反斜杠当分隔符：同一份包在两端会解到不同位置，这里直接判非法。

    ``zipfile`` 在 Windows 上写包时会先把 ``\\`` 归一成 ``/``，所以造一份带反斜杠
    的**真实** zip 在写入侧做不到；判据函数本身仍要咬得住（包也可能来自别的系统）。
    """
    from types import SimpleNamespace

    from minimax_h3_prompt.workbench_store import _archive_member_problem

    info = SimpleNamespace(filename="a\\b.txt", external_attr=0)
    assert _archive_member_problem(info) == "包含反斜杠"


def test_extract_archive_rejects_symlinks(tmp_path):
    archive = tmp_path / "link.zip"
    archive.write_bytes(_symlink_zip("link"))
    with pytest.raises(ArchiveInvalid) as excinfo:
        extract_archive_zip(archive, tmp_path / "out")
    assert excinfo.value.code == "archive_invalid"


def test_extract_archive_writes_normal_entries(tmp_path):
    archive = tmp_path / "ok.zip"
    archive.write_bytes(_zip_bytes({"director/plan.json": b"{}"}))
    written = extract_archive_zip(archive, tmp_path / "out")
    assert written == ["director/plan.json"]
    assert (tmp_path / "out" / "director" / "plan.json").read_bytes() == b"{}"


# ---------------------------------------------------------------------------
# 打包 job：阶段 2 之后自动接上，产物走归档形态，简报逐段一份
# ---------------------------------------------------------------------------

def test_pack_job_runs_after_stage2_and_the_archive_is_downloadable(workbench):
    app, model, root, runtime = workbench
    with workbench_client(app) as client:
        session_id = _create(client)
        review = _run_to_review(client, session_id)
        detail = _run_through_pack(client, session_id, review)

        directory = root / Path(session_id)
        zip_file = zip_path_for(directory)
        assert zip_file.is_file(), "打包 job 应产出 initial.mmxpack.zip"
        assert detail["status"] == "awaiting_human"

        # kind 能区分：阶段 2 是 advance，打包是 pack。
        kinds = [job["kind"] for job in detail["jobs"]]
        assert "pack" in kinds
        pack_job = [job for job in detail["jobs"] if job["kind"] == "pack"][-1]
        assert pack_job["status"] == "succeeded"
        assert "装配约束" in pack_job["detail"]
        assert "width / height = 736 / 1280" in pack_job["detail"]

        artifacts = {item["name"]: item for item in detail["artifacts"]}
        pack_artifact = artifacts["director/initial.mmxpack.zip"]
        assert pack_artifact["kind"] == "director_pack"
        assert "director/plan.json" not in artifacts

        url = f"/api/v1/artifacts/{pack_artifact['artifact_id']}/content"
        body = client.get(url)
        assert body.status_code == 200, body.text
        assert body.headers["content-type"] == "application/zip"
        assert body.content == zip_file.read_bytes()

        head = client.head(url)
        assert head.status_code == 200
        assert int(head.headers["content-length"]) == zip_file.stat().st_size

        ranged = client.get(url, headers={"Range": "bytes=0-3"})
        assert ranged.status_code == 206
        assert ranged.content == ZIP_MAGIC
        assert ranged.headers["content-range"].startswith("bytes 0-3/")

        beyond = client.get(url, headers={"Range": f"bytes={zip_file.stat().st_size}-"})
        assert beyond.status_code == 416
        assert beyond.headers["content-range"] == f"bytes */{zip_file.stat().st_size}"


def test_each_segment_has_a_copyable_briefing_with_runselection_and_frames(workbench):
    app, model, root, runtime = workbench
    with workbench_client(app) as client:
        session_id = _create(client)
        review = _run_to_review(client, session_id)
        detail = _run_through_pack(client, session_id, review)

        directory = root / Path(session_id)
        plan = read_plan_json(plan_path(directory))
        artifacts = {item["name"]: item for item in detail["artifacts"]}
        for segment in plan.segments:
            name = f"director/briefing-{segment.index:02d}.md"
            assert name in artifacts, name
            assert artifacts[name]["kind"] == "director_briefing"
            text = client.get(
                f"/api/v1/artifacts/{artifacts[name]['artifact_id']}/content").text
            assert "runSelection" in text
            assert f"只勾第 {segment.index} 段" in text
            assert f"导出 {segment.export_frames} 帧" in text


def test_pack_constraint_failure_is_named_and_leaves_no_half_pack(workbench, monkeypatch):
    app, model, root, runtime = workbench
    # 画布不是 32 的倍数：第三条装配约束必挂，消息要点名「画布 / 32」。
    monkeypatch.setattr(
        runtime, "director_settings",
        lambda: DirectorPackSettings(
            width=1000, height=1280, ref_max_size=864, ref_image_size="match"))
    with workbench_client(app) as client:
        session_id = _create(client)
        review = _run_to_review(client, session_id)
        approved = client.post(
            f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
            json={"decision": "approve"})
        assert approved.status_code == 202, approved.text
        assert _wait_job(client, session_id, approved.json()["job_id"])["status"] == "succeeded"
        detail = _wait_idle(client, session_id)

        assert detail["status"] == "failed"
        pack_job = [job for job in detail["jobs"] if job["kind"] == "pack"][-1]
        assert pack_job["status"] == "failed"
        assert pack_job["error_code"] == "pack_build_failed"
        assert "画布" in pack_job["detail"]
        assert "32" in pack_job["detail"]

        directory = root / Path(session_id)
        assert not zip_path_for(directory).exists(), "约束不过不许落半份包"
        assert not pack_dir_for(directory).exists()


def test_advance_after_interrupt_keeps_the_existing_zip_and_fills_missing_briefings(
    workbench,
):
    app, model, root, runtime = workbench
    with workbench_client(app) as client:
        session_id = _create(client)
        review = _run_to_review(client, session_id)
        _run_through_pack(client, session_id, review)

        directory = root / Path(session_id)
        zip_file = zip_path_for(directory)
        original = zip_file.read_bytes()
        briefing = directory / "director" / "briefing-01.md"
        assert briefing.is_file()
        briefing.unlink()

        session_file = directory / SESSION_FILENAME
        raw = json.loads(session_file.read_text(encoding="utf-8"))
        raw["status"] = "interrupted"
        session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        resumed = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert resumed.status_code == 202, resumed.text
        body = _wait_job(client, session_id, resumed.json()["job_id"])
        assert body["status"] == "succeeded", body
        assert "跳过重建" in body["detail"]
        detail = _wait_idle(client, session_id)

        assert zip_file.read_bytes() == original, "zip 已在时不许重建"
        assert briefing.is_file(), "缺失的简报仍要补"
        assert any(item["kind"] == "director_briefing" for item in detail["artifacts"])


# ---------------------------------------------------------------------------
# 上传 zip（D8 §9.2）：与视频同档上限、魔数嗅探
# ---------------------------------------------------------------------------

def test_zip_upload_is_accepted(workbench):
    app, model, root, runtime = workbench
    payload = _zip_bytes({"timeline.json": b"{}"})
    empty = b"PK\x05\x06" + b"\x00" * 18
    with workbench_client(app) as client:
        session_id = _create(client)
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": ("initial.mmxpack.zip", payload, "application/zip")})
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["kind"] == "zip"

        empty_pack = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": ("empty.zip", empty, "application/zip")})
        assert empty_pack.status_code == 201, empty_pack.text
        assert empty_pack.json()["kind"] == "zip"

        rejected = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": ("假的.zip", b"not a zip at all", "application/zip")})
        assert rejected.status_code == 415, rejected.text
        assert rejected.json()["code"] == "unsupported_media_type"


def test_zip_cannot_be_registered_as_reference_material(workbench):
    """ZIP 不是参考素材三型之一：带语义名上传要 415，不是 500（台账没有 zip 这一型）。"""
    app, model, root, runtime = workbench
    with workbench_client(app) as client:
        created = client.post("/api/v1/sessions", json={
            "mode": "ref", "variant": "T2VA", "duration": 4.0,
            "style": "Cinematic", "language": "Chinese", "plot": "雨夜街口",
        })
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        resp = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            data={"name": "导演包"},
            files={"file": ("pack.zip", _zip_bytes({"a.txt": b"x"}),
                            "application/zip")})
        assert resp.status_code == 415, resp.text
        assert resp.json()["code"] == "unsupported_media_type"
