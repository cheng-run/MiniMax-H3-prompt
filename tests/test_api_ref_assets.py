"""D7-2（#255）：ref 路线放开与素材台账——创建口、语义名/编号、同步容量闸。

HTTP 面是本票的验收面（spec #252 §4.1–§4.3、§8、§12、§13）。判据尽量来自
**独立来源**：编号与容量的期望值手写在用例里（不从 ``ref_capacity`` 现取），
台账落盘形状直接读会话文件复核——不让接口自己证明自己。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.session_store import SESSION_FILENAME
from workbench_client import workbench_client

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"
WAV_MAGIC = b"RIFF\x00\x00\x00\x00WAVEfmt "


def _png(seed: int = 0) -> bytes:
    """魔数正确、内容各不相同的“图片”：内容一不同，哈希与台账条目就不同。"""
    return PNG_MAGIC + bytes([seed % 256]) * 4096


def _mp4(seed: int = 0) -> bytes:
    return MP4_MAGIC + bytes([seed % 256]) * 4096


def _wav(seed: int = 0) -> bytes:
    return WAV_MAGIC + bytes([seed % 256]) * 4096


def _client(tmp_path: Path) -> TestClient:
    return workbench_client(create_app(sessions_root=tmp_path))


def _create(client: TestClient, mode: str = "ref", **overrides) -> dict:
    body = {
        "mode": mode, "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信",
        **overrides,
    }
    resp = client.post("/api/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload(client: TestClient, session_id: str, payload: bytes, filename: str,
            *, name: str | None = None, content_type: str = "application/octet-stream"):
    files = {"file": (filename, payload, content_type)}
    data = {} if name is None else {"name": name}
    return client.post(f"/api/v1/sessions/{session_id}/assets",
                       files=files, data=data)


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    return body


def _session_file(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / Path(session_id) / SESSION_FILENAME


# ---------------------------------------------------------------------------
# 创建口（spec §4.1 / AC1）
# ---------------------------------------------------------------------------

def test_create_ref_session_lands_mode_ref_and_fixed_variant(tmp_path):
    client = _client(tmp_path)

    detail = _create(client, mode="ref")

    assert detail["mode"] == "ref"
    assert detail["brief"]["mode"] == "ref"
    # ref 不走帧链（ADR 0011）：variant 由服务端固定落库，前端不发也不影响。
    assert detail["brief"]["variant"] == "T2VA"
    on_disk = json.loads(_session_file(tmp_path, detail["session_id"]).read_text("utf-8"))
    assert on_disk["brief"]["mode"] == "ref"
    assert on_disk["brief"]["variant"] == "T2VA"


def test_create_ref_session_rejects_a_frame_chain_variant(tmp_path):
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json={
        "mode": "ref", "variant": "FL2VA", "duration": 4.0,
        "plot": "雨夜旧信"})

    assert resp.status_code == 422, resp.text
    assert "variant" in resp.text.lower() or "变体" in resp.text


def test_create_still_rejects_the_director_route(tmp_path):
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json={
        "mode": "director", "duration": 4.0, "plot": "雨夜旧信"})

    assert resp.status_code == 422


def test_create_no_longer_accepts_refs_in_the_payload(tmp_path):
    """DTO 里不再出现本地路径（spec §4.1）：带 ``refs`` 的请求要响，不许静默忽略。"""
    client = _client(tmp_path)

    resp = client.post("/api/v1/sessions", json={
        "mode": "ref", "duration": 4.0, "plot": "雨夜旧信",
        "refs": [{"picture": 1, "name": "橘猫", "path": "D:/素材库/猫.png"}]})

    assert resp.status_code == 422, resp.text
    assert not list(tmp_path.glob("*/GEN*")), "带废弃字段的请求不该建出会话目录"


# ---------------------------------------------------------------------------
# 上传：语义名、嗅探定类型、同类编号（spec §4.2 / AC2）
# ---------------------------------------------------------------------------

def test_ref_upload_registers_a_ledger_entry_with_sniffed_kind(tmp_path):
    payload = _png(1)
    digest = hashlib.sha256(payload).hexdigest()
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]

        resp = _upload(client, session_id, payload, "猫.png", name="橘猫")

        assert resp.status_code == 201, resp.text
        body = resp.json()
        # 上传响应带回台账编号与嗅探类型（spec §12「返回编号与类型」）。
        assert (body["kind"], body["number"]) == ("image", 1)
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    refs = detail["brief"]["refs"]
    assert refs == [{
        "name": "橘猫", "kind": "picture", "number": 1,
        "asset_id": f"{session_id}/{digest[:16]}",
        # 四态与来路（#279）：上传建立的行是 uploaded，还没校对过是 registered。
        "status": "registered", "source": "uploaded",
    }]
    # 台账住会话状态（ADR 0032），不另写 asset-ledger.json。
    raw = json.loads(_session_file(tmp_path, session_id).read_text("utf-8"))
    ledger = raw["asset_ledger"]
    assert [entry["name"] for entry in ledger] == ["橘猫"]
    assert ledger[0]["kind"] == "picture"
    assert ledger[0]["number"] == 1
    assert ledger[0]["sha256"] == digest
    assert ledger[0]["asset_id"] == f"{session_id}/{digest[:16]}"
    assert ledger[0]["round"] == 0
    assert ledger[0]["status"]
    assert not (tmp_path / Path(session_id) / "asset-ledger.json").exists()


def test_upload_without_a_name_is_a_plain_asset_not_a_registration(tmp_path):
    """不带语义名 = 普通素材（素材图/段视频回交的通道）：ref 会话里也合法、不记账。

    用户 2026-11-13 裁定：起跑锁只挡台账登记，不挡全部上传——否则 #256/#258 的回交
    在会话早已离开 ``created`` 时会被 409 锁死。
    """
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]

        resp = _upload(client, session_id, _png(1), "素材图.png")
        # 全空白与省略同义：它同样不是一次登记。
        blank = _upload(client, session_id, _png(2), "素材图2.png", name="   ")
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert resp.status_code == 201, resp.text
    assert resp.json()["number"] is None
    assert blank.status_code == 201, blank.text
    raw = json.loads(_session_file(tmp_path, session_id).read_text("utf-8"))
    assert raw["asset_ledger"] == []
    # 文件仍然照常落盘、照常出现在目录现扫的 assets 里。
    assert len(detail["assets"]) == 2
    assert detail["brief"]["refs"] == []


def test_ref_upload_rejects_a_duplicate_semantic_name(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        _upload(client, session_id, _png(1), "猫.png", name="橘猫")

        resp = _upload(client, session_id, _png(2), "另一只.png", name="橘猫")

    body = _assert_problem(resp, 422, "asset_name_invalid")
    assert "橘猫" in body["detail"]


def test_numbers_run_per_kind_from_one_in_upload_order(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        uploaded = [
            _upload(client, session_id, _png(1), "a.png", name="甲").json(),
            _upload(client, session_id, _mp4(1), "b.mp4", name="乙").json(),
            _upload(client, session_id, _png(2), "c.png", name="丙").json(),
            _upload(client, session_id, _wav(1), "d.wav", name="丁").json(),
        ]
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    # 上传响应里的编号与详情台账同源，且按同类上传序从 1 起。
    assert [item["number"] for item in uploaded] == [1, 1, 2, 1]
    refs = detail["brief"]["refs"]
    assert [(r["kind"], r["number"], r["name"]) for r in refs] == [
        ("picture", 1, "甲"),
        ("picture", 2, "丙"),
        ("video", 1, "乙"),
        ("audio", 1, "丁"),
    ]


def test_base_upload_without_a_name_keeps_working(tmp_path):
    """base 会话的帧提交上传走同一个端点（D4）：逼它带语义名会打断既有链路。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client, mode="base")["session_id"]

        resp = _upload(client, session_id, _png(1), "帧.png")
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert resp.status_code == 201, resp.text
    assert detail["brief"]["refs"] == []
    raw = json.loads(_session_file(tmp_path, session_id).read_text("utf-8"))
    assert raw.get("asset_ledger", []) == []


# ---------------------------------------------------------------------------
# 同步容量闸（spec §8 / AC3）
# ---------------------------------------------------------------------------

def test_tenth_picture_is_refused_and_names_the_picture_bucket(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        for index in range(9):
            assert _upload(client, session_id, _png(index), f"{index}.png",
                           name=f"图{index}").status_code == 201, index
        resp = _upload(client, session_id, _png(99), "超.png", name="图9")

    body = _assert_problem(resp, 422, "ref_capacity_exceeded")
    assert "9" in body["detail"] and "图" in body["detail"]


def test_fourth_video_is_refused(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        for index in range(3):
            assert _upload(client, session_id, _mp4(index), f"{index}.mp4",
                           name=f"片{index}").status_code == 201
        resp = _upload(client, session_id, _mp4(9), "超.mp4", name="片3")

    body = _assert_problem(resp, 422, "ref_capacity_exceeded")
    assert "视频" in body["detail"]


def test_total_file_cap_is_independent_of_the_per_kind_caps(tmp_path):
    """9 图 + 3 视频已经用满 12；再挂 1 条音频就是 13——12 是独立上限，不是求和。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        for index in range(9):
            _upload(client, session_id, _png(index), f"{index}.png", name=f"图{index}")
        for index in range(3):
            _upload(client, session_id, _mp4(index), f"{index}.mp4", name=f"片{index}")
        resp = _upload(client, session_id, _wav(1), "声.wav", name="声")

    body = _assert_problem(resp, 422, "ref_capacity_exceeded")
    assert "12" in body["detail"]


def test_base_uploads_are_not_counted_against_ref_capacity(tmp_path):
    """容量闸只算 ref 会话的台账：base 的帧图没有参考素材语义。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client, mode="base")["session_id"]
        for index in range(12):
            assert _upload(client, session_id, _png(index), f"{index}.png").status_code == 201

    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    assert len(detail["assets"]) == 12
    assert detail["brief"]["refs"] == []


def test_ref_capacity_counts_ledger_entries_only(tmp_path):
    """9/3/3/12 按台账条目算：不带语义名的普通素材不占额度、也不推进编号。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        for index in range(9):
            assert _upload(client, session_id, _png(index), f"{index}.png",
                           name=f"图{index}").status_code == 201
        # 普通素材（素材图回交）不记账：9 张台账已经满，它照样传得上去。
        plain = _upload(client, session_id, _png(90), "素材图.png")
        # 第 10 条**登记**仍被容量闸拒绝。
        tenth = _upload(client, session_id, _png(91), "第10张.png", name="图9")
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert plain.status_code == 201, plain.text
    _assert_problem(tenth, 422, "ref_capacity_exceeded")
    assert len(detail["brief"]["refs"]) == 9
    assert len(detail["assets"]) == 10


# ---------------------------------------------------------------------------
# 资产清单派生与会话标记（#279 修订「资产清单的派生与落盘」）
# ---------------------------------------------------------------------------

def _stage1_state() -> dict:
    """固定的阶段 1 产物：两件资产，剧本里先写橘猫、后写台灯。"""
    return {
        "script": "分场剧本：橘猫蹲在窗台，台灯亮着。",
        "shot_table": "[Shot 1] At 00:00.000, a cat sitting by the window.",
        "character_image_prompts": [
            {"name": "橘猫", "positive_prompt": "一只橘色短毛猫。",
             "negative_prompt": "", "locked_features": []},
            {"name": "台灯", "positive_prompt": "一盏黄铜台灯。",
             "negative_prompt": "", "locked_features": []},
        ],
    }


def _wait_until(client: TestClient, session_id: str, predicate, timeout: float = 15.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/sessions/{session_id}").json()
        if predicate(body):
            return body
        time.sleep(0.02)
    raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{session_id}")


def test_created_ref_session_is_not_marked_as_derived(tmp_path):
    """空台账 + 未派生 ≠ 阶段 1 已完成：没跑阶段 1 的会话不许被标成「本片没有素材」。

    两个状态在台账上都是空的，分开它们的只有这一个标记；读成已派生，素材循环就会被
    整条跳过（`_asset_step_of` 对空台账恒判「走完了」）。
    """
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]

    client.get(f"/api/v1/sessions/{session_id}")

    raw = json.loads(_session_file(tmp_path, session_id).read_text("utf-8"))
    assert raw["asset_ledger"] == []
    assert raw["asset_manifest_derived"] is False


def test_derivation_is_idempotent_and_backfills_on_session_read(tmp_path, monkeypatch):
    """派生幂等；「阶段 1 已完成但未派生」的会话重进页面就补齐（#279）。

    补齐不是可有可无的兜底：漏落一次盘之后，空台账会被读成「本片确实没有素材」，
    用户手里那几件参考素材就静默消失了。三条入口（续跑/恢复/重进页面）都走同一个
    幂等口，所以第二次补出来的台账必须与第一次逐字相同。
    """
    from minimax_h3_prompt.graph import pipeline

    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: (_stage1_state(), None, None))
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        started = client.post(f"/api/v1/sessions/{session_id}/jobs")
        assert started.status_code == 202, started.text
        _wait_until(client, session_id, lambda body: any(
            item["kind"] == "stage1_review" and item["status"] == "pending"
            for item in body["action_items"]))

        raw = json.loads(_session_file(tmp_path, session_id).read_text("utf-8"))
        assert raw["asset_manifest_derived"] is True
        assert [(entry["name"], entry["number"], entry["status"], entry["source"])
                for entry in raw["asset_ledger"]] == [
            ("橘猫", 1, "pending", "derived"), ("台灯", 2, "pending", "derived")]
        derived = raw["asset_ledger"]

        # 造出「阶段 1 已完成、清单没派生」的崩溃窗口，再重进页面。
        store = client.app.state.session_store
        store.mutate(session_id, lambda _detail: {
            "asset_ledger": [], "asset_manifest_derived": False})
        detail = client.get(f"/api/v1/sessions/{session_id}").json()
        assert [(ref["name"], ref["number"], ref["status"], ref["source"])
                for ref in detail["brief"]["refs"]] == [
            ("橘猫", 1, "pending", "derived"), ("台灯", 2, "pending", "derived")]

        # 幂等：再读几次不多行、件号与状态都不动。
        for _ in range(3):
            client.get(f"/api/v1/sessions/{session_id}")
        again = json.loads(_session_file(tmp_path, session_id).read_text("utf-8"))
        assert again["asset_ledger"] == derived


def test_base_session_upload_with_a_name_is_rejected(tmp_path):
    """base 没有素材台账：带语义名是 ref 专属动作，响亮拒绝而不是静默忽略。

    选 422 ``asset_name_invalid``（语义名在这条路线上没有意义），与容量/类型错误
    分开；既有 base 客户端的请求体没有这个字段，行为一字不变。
    """
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client, mode="base")["session_id"]

        resp = _upload(client, session_id, _png(1), "帧.png", name="橘猫")
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    body = _assert_problem(resp, 422, "asset_name_invalid")
    assert "ref" in body["detail"]
    assert detail["assets"] == []


# ---------------------------------------------------------------------------
# 起跑锁与起跑门槛（spec §4.3 / AC4）
# ---------------------------------------------------------------------------

def test_registration_after_start_only_accepts_the_current_item(tmp_path):
    """位置锁（#282）：起跑后带语义名的登记只放行**当前待办那一件**。

    旧判据是时间锁（起跑即全锁死）。#279 把素材集合改成阶段 1 派生之后，循环里交图
    本来就是流程的一部分——锁的对象因此从「什么时候」换成「哪一件」：别的名字一律
    409，且文案点名当前是第几件（只说「不能登记」等于让用户猜该交哪一件）。
    不带语义名的普通上传（素材图/段视频回交）本来就不进台账，仍然不受影响。
    """
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]
    _upload(client, session_id, _png(1), "a.png", name="甲")
    _upload(client, session_id, _png(2), "b.png", name="乙")
    # 用存储层把会话推进到起跑后（真起任务会跑模型；这里只要状态）。
    client.app.state.session_store.update(session_id, status="running")

    blocked = _upload(client, session_id, _png(3), "c.png", name="丙")
    plain = _upload(client, session_id, _png(4), "素材图.png")
    detail = client.get(f"/api/v1/sessions/{session_id}").json()

    body = _assert_problem(blocked, 409, "asset_locked")
    assert "第 1 件" in body["detail"] and "甲" in body["detail"]
    assert plain.status_code == 201, plain.text
    # 被拒的那次不落行；普通上传也不进台账：refs 里仍是起跑前登记的那两件。
    assert [ref["name"] for ref in detail["brief"]["refs"]] == ["甲", "乙"]
    assert len(detail["assets"]) == 3


def test_registration_after_start_of_the_current_name_is_accepted(tmp_path):
    """当前那一件自己的名字放行：位置锁回答「这一件能不能交」，不是「一律不许」。

    上传行（导演台 r2v 与起跑前的预上传）的图连同资产图词一起已经定下了这一件的
    描述，起跑后再换一张就是「事后推翻已写好的词」——所以这里只放行文件落盘，
    台账一行不动（编号仍为空）。派生行的「交图」在素材循环里有另一条用例。
    """
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]
    _upload(client, session_id, _png(1), "a.png", name="甲")
    client.app.state.session_store.update(session_id, status="running")

    allowed = _upload(client, session_id, _png(2), "a2.png", name="甲")
    detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["number"] is None, "位置锁放行不等于新增一行台账"
    assert [(ref["name"], ref["number"]) for ref in detail["brief"]["refs"]] == [("甲", 1)]


def test_ref_session_starts_with_zero_pictures(tmp_path, monkeypatch):
    """零素材就能起跑（#279 修订 §4.3）：素材集合改由阶段 1 派生。

    旧判据要求的「先上传一张 Picture」是死锁的前半截——没素材不能起跑 → 不起跑就没有
    词 → 没有词就生成不出素材。这里只钉起跑闸门放行（job 被受理），阶段 1 本身另有
    用例覆盖，所以把管线换成不会真发请求的桩。
    """
    from minimax_h3_prompt.graph import pipeline

    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: ({}, None, None))
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]

    resp = client.post(f"/api/v1/sessions/{session_id}/jobs")

    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "advance"


def test_director_r2v_still_requires_a_picture_to_start(tmp_path):
    """导演台 r2v 不动（#279「路线差异」）：开场首帧必须是真实存在的图，没有等效派生。"""
    client = _client(tmp_path)
    created = client.post("/api/v1/sessions", json={
        "mode": "director", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信",
        "director_mode": "r2v", "director_node_id": "node-r2v"})
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]

    resp = client.post(f"/api/v1/sessions/{session_id}/jobs")

    body = _assert_problem(resp, 409, "asset_required")
    assert "素材" in body["detail"]
    assert client.get(f"/api/v1/sessions/{session_id}").json()["status"] == "created"


def test_ref_session_without_pictures_can_still_be_created(tmp_path):
    client = _client(tmp_path)

    detail = _create(client)

    assert detail["status"] == "created"
    assert detail["brief"]["refs"] == []


# ---------------------------------------------------------------------------
# 删除（spec §4.2；§12「不新增端点」与 §4.2 冲突，用户 2026-11-13 裁定新增）
# ---------------------------------------------------------------------------

def _delete(client: TestClient, session_id: str, asset_id: str):
    return client.delete(f"/api/v1/sessions/{session_id}/assets/{asset_id}")


def test_delete_removes_the_entry_the_file_and_renumbers(tmp_path):
    first, second = _png(1), _png(2)
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        first_id = _upload(client, session_id, first, "a.png", name="甲").json()["asset_id"]
        second_id = _upload(client, session_id, second, "b.png", name="乙").json()["asset_id"]

        resp = _delete(client, session_id, first_id)
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    refs = detail["brief"]["refs"]
    # 删掉第 1 件后，第 2 件重排成 1：编号语义是「同类第几件」，不留空洞。
    assert [(r["name"], r["number"]) for r in refs] == [("乙", 1)]
    assert [asset["asset_id"] for asset in detail["assets"]] == [second_id]
    assets_dir = tmp_path / Path(session_id) / "assets"
    assert sorted(path.name for path in assets_dir.iterdir()) == [
        f"{second_id.rsplit('/', 1)[1]}.json",
        f"{second_id.rsplit('/', 1)[1]}.png",
    ]


def test_delete_then_reupload_the_same_content_is_allowed(tmp_path):
    payload = _png(1)
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        asset_id = _upload(client, session_id, payload, "a.png", name="甲").json()["asset_id"]
        _delete(client, session_id, asset_id)

        resp = _upload(client, session_id, payload, "a.png", name="甲")

    assert resp.status_code == 201, resp.text
    assert resp.json()["asset_id"] == asset_id


def test_delete_on_a_base_session_is_route_mismatch(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client, mode="base")["session_id"]
        asset_id = _upload(client, session_id, _png(1), "帧.png").json()["asset_id"]

        resp = _delete(client, session_id, asset_id)

    _assert_problem(resp, 409, "route_mismatch")


def test_delete_after_start_is_locked(tmp_path):
    client = _client(tmp_path)
    session_id = _create(client)["session_id"]
    asset_id = _upload(client, session_id, _png(1), "a.png", name="甲").json()["asset_id"]
    client.app.state.session_store.update(session_id, status="running")

    resp = _delete(client, session_id, asset_id)

    _assert_problem(resp, 409, "asset_locked")
    assert client.get(f"/api/v1/sessions/{session_id}").json()["brief"]["refs"]


def test_delete_unknown_or_foreign_asset_is_not_found(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        _upload(client, session_id, _png(1), "a.png", name="甲")
        unknown = _delete(client, session_id, f"{session_id}/{'0' * 16}")

        other = _create(client, plot="另一个故事")["session_id"]
        foreign = _delete(client, other, f"{session_id}/{'0' * 16}")

    _assert_problem(unknown, 404, "asset_not_found")
    _assert_problem(foreign, 404, "asset_not_found")


def test_delete_of_an_unregistered_plain_asset_is_not_found(tmp_path):
    """删除只撤销**台账登记**：普通上传（素材图/段视频）不在台账里，删不动它。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        asset_id = _upload(client, session_id, _png(1), "素材图.png").json()["asset_id"]

        resp = _delete(client, session_id, asset_id)
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    _assert_problem(resp, 404, "asset_not_found")
    # 文件没有被删：它本来就不是参考素材。
    assert [asset["asset_id"] for asset in detail["assets"]] == [asset_id]


def test_delete_does_not_emit_events(tmp_path):
    """素材增删不发事件（D3-AC13）：事件闭集不因本票而扩。"""
    from minimax_h3_prompt.workbench_store import EVENTS_FILENAME

    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _create(client)["session_id"]
        asset_id = _upload(client, session_id, _png(1), "a.png", name="甲").json()["asset_id"]
        before = (tmp_path / Path(session_id) / EVENTS_FILENAME).read_text("utf-8")
        _delete(client, session_id, asset_id)
        after = (tmp_path / Path(session_id) / EVENTS_FILENAME).read_text("utf-8")

    assert before == after

