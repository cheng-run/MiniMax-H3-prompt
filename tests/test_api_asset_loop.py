"""D7-3（#256）：素材循环纵切——出词 job、校对 job、``asset_image`` 待办。

验收面取 HTTP（spec #252 §5.2/§5.3/§6/§7/§14）：起跑 → 阶段 1 → 通过审查 →
逐件出词/交图/校对/通过或重开。判据尽量来自**独立来源**：
- 件号、轮次、待办形状直接读会话文件，不用接口自证；
- 读图（``reference_auditor.describe_image``）与改写（``build_chat_model``）都在
  测试里打桩——本文件绝不真发网络请求。

阶段 1 用**真实执行路径**（``JobRunner._execute_stage1``），只把
``graph.pipeline.run_stage1`` 换成固定 state 的桩：这样"素材循环接在阶段 1
之后"这条接线是被真跑出来的，而不是用例自己拼 state 绕过去。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.workbench_store import artifact_spec
from workbench_client import workbench_client

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"

# 两件参考素材的语义名。出词 job 按**语义名**去阶段 1 的资产图提示词里取词
# （越权假设，见收工报告）：名字对不上时任务要响，不许拿别的件的词顶替。
CAT = "橘猫"
LAMP = "台灯"


def _png(seed: int = 0) -> bytes:
    return PNG_MAGIC + bytes([seed % 256]) * 4096


def _mp4(seed: int = 0) -> bytes:
    return MP4_MAGIC + bytes([seed % 256]) * 4096


class _RewriteStub:
    """改写用的最小直调假模型：``invoke(request, config=...)``，首字节喂回调。"""

    def __init__(self, reply: str = "改写后的资产图词") -> None:
        self.reply = reply
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        for callback in (config or {}).get("callbacks") or []:
            callback.on_llm_new_token("…")
        return self.reply


def _stage1_state(prompts: dict[str, str]) -> dict:
    """阶段 1 的固定 state：剧本、分镜表与逐件资产图提示词（按语义名命名）。"""
    return {
        "script": "分场剧本：橘猫蹲在窗台，台灯亮着。",
        "shot_table": "[Shot 1] At 00:00.000, a medium shot of the cat by the lamp.",
        "character_image_prompts": [
            {
                "name": name,
                "positive_prompt": text,
                "negative_prompt": "",
                # 锁死清单必须是正文片段（对照器按字面收窄偏差）。
                "locked_features": [text],
            }
            for name, text in prompts.items()
        ],
    }


class Harness:
    """一份 ref 会话的完整夹具：创建、传素材、起跑、通过审查。"""

    def __init__(self, client, root: Path, rewrite: _RewriteStub,
                 readings: list[str]) -> None:
        self.client = client
        self.root = root
        self.rewrite = rewrite
        # 每次读图按顺序取一条：用例自己决定「第一件通过、第二件不通过」。
        self.readings = readings
        self.read_calls: list[tuple[str, str]] = []
        self.session_id = ""

    # -- HTTP 小抄 ---------------------------------------------------
    def detail(self) -> dict:
        return self.client.get(f"/api/v1/sessions/{self.session_id}").json()

    def pending(self, kind: str) -> list[dict]:
        return [item for item in self.detail()["action_items"]
                if item["kind"] == kind and item["status"] == "pending"]

    def on_disk(self) -> dict:
        path = self.root / Path(self.session_id) / SESSION_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))

    def ledger(self) -> list[dict]:
        return self.on_disk().get("asset_ledger", [])

    def artifact_text(self, name: str) -> str:
        path = self.root / Path(self.session_id) / name
        return path.read_text(encoding="utf-8")

    def wait_until(self, predicate, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.detail()
            if predicate(body):
                return body
            time.sleep(0.02)
        raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{self.session_id}")

    def advance(self) -> dict:
        resp = self.client.post(f"/api/v1/sessions/{self.session_id}/jobs")
        assert resp.status_code == 202, resp.text
        return resp.json()

    def wait_idle(self) -> dict:
        """等到不再有任务在跑（人工待办与终态都算停）。"""
        return self.wait_until(
            lambda body: body["status"] != "running"
            and body.get("current_job") is None)

    # -- 流程 --------------------------------------------------------
    def create(self, duration: float = 4.0) -> None:
        resp = self.client.post("/api/v1/sessions", json={
            "mode": "ref", "variant": "T2VA", "duration": duration,
            "style": "写实", "language": "Chinese", "plot": "雨夜旧信"})
        assert resp.status_code == 201, resp.text
        self.session_id = resp.json()["session_id"]

    def upload(self, payload: bytes, filename: str, *, name: str | None = None) -> dict:
        files = {"file": (filename, payload, "application/octet-stream")}
        data = {} if name is None else {"name": name}
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/assets", files=files, data=data)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def register_pictures(self) -> None:
        self.upload(_png(1), "cat.png", name=CAT)
        self.upload(_png(2), "lamp.png", name=LAMP)

    def start_stage1(self) -> dict:
        self.advance()
        return self.wait_until(
            lambda body: any(item["kind"] == "stage1_review"
                             and item["status"] == "pending"
                             for item in body["action_items"]))

    def approve_stage1(self) -> None:
        item = self.pending("stage1_review")[0]
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/action-items/{item['item_id']}/resolve",
            json={"decision": "approve"})
        assert resp.status_code == 202, resp.text

    def submit_image(self, item: dict, asset_id: str):
        return self.client.post(
            f"/api/v1/sessions/{self.session_id}/action-items/{item['item_id']}/resolve",
            json={"asset_id": asset_id})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """ref 会话 + 固定阶段 1 + 打桩读图/改写。``readings`` 由用例通过属性改。"""
    root = tmp_path / "sessions"
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: (
            _stage1_state({
                CAT: "一只橘色短毛猫蹲在木质窗台，尾巴绕住前爪。",
                LAMP: "一盏黄铜台灯立在书桌右角，灯罩是墨绿色。",
            }), None, None))
    rewrite = _RewriteStub()
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: rewrite)

    def fake_describe(path, instruction=None):
        state.read_calls.append((str(path), str(instruction or "")))
        if not state.readings:
            raise AssertionError("用例没有给这条读图准备结论")
        return state.readings.pop(0)

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    # 必须 ``with`` 持有：TestClient 的 portal 只在上下文里活着，后台任务（job）
    # 才跑得完；裸 client 每次请求起一个临时 portal，请求一返回任务就被取消。
    with workbench_client(create_app(sessions_root=root)) as client:
        state = Harness(client, root, rewrite, [])
        yield state


def _claims(*items: tuple[str, str, str]) -> str:
    """读图结论的 JSON 文本：``(aspect, quote, observed)`` 三元组。"""
    return json.dumps({"claims": [
        {"aspect": aspect, "prompt_quote": quote, "observed": observed}
        for aspect, quote, observed in items]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 声明面（spec §5.2 / §6.3 / §7 / §11）
# ---------------------------------------------------------------------------

def test_asset_loop_is_a_declared_step_with_a_progress_stage():
    """``asset_loop`` 是步骤标签，且 ``job.progress`` 的 stage 闭集认它。"""
    from minimax_h3_prompt import step_machine
    from minimax_h3_prompt.workbench_store import _PROGRESS_STAGES

    assert "asset_loop" in step_machine.STEPS
    assert step_machine.STEP_ASSET_LOOP == "asset_loop"
    assert "asset_loop" in _PROGRESS_STAGES


def test_asset_image_todo_and_its_only_decision_are_declared():
    """待办种类 ``asset_image`` 与唯一决议 ``submit_image``（没有放行键）。"""
    from minimax_h3_prompt import step_machine
    from minimax_h3_prompt.workbench_store import _DECISIONS

    assert step_machine.ACTION_ASSET_IMAGE == "asset_image"
    assert step_machine.DECISION_SUBMIT_IMAGE == "submit_image"
    assert step_machine.DECISION_SUBMIT_IMAGE in _DECISIONS


def test_asset_prompt_artifact_is_registered_by_pattern():
    """``assets/picture-NN.md`` → kind ``asset_prompt``；同目录的素材正本不进产物。"""
    from minimax_h3_prompt.workbench_store import ARTIFACT_FINAL_FILM

    spec = artifact_spec("assets/picture-01.md")
    assert spec is not None and spec.kind == "asset_prompt"
    assert spec.media_type is None, "资产图词是文本产物"
    # 三位数/一位数编号不在这条模式里：编号是两位的件号，与台账同号。
    assert artifact_spec("assets/picture-1.md") is None
    assert artifact_spec("assets/picture-007.md") is None
    assert artifact_spec(ARTIFACT_FINAL_FILM) is not None


# ---------------------------------------------------------------------------
# 阶段 1 通过 → 第 1 件出词（spec §5.3 / §6.2 / §7）
# ---------------------------------------------------------------------------

def _events(root: Path, session_id: str) -> list[dict]:
    path = root / Path(session_id) / "events.jsonl"
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_stage1_approval_on_ref_opens_the_first_asset_card(harness):
    """ref 通过审查不直接进阶段 2：先出第 1 件的词、落产物、挂 ``asset_image`` 待办。"""
    harness.create()
    harness.register_pictures()
    harness.start_stage1()

    harness.approve_stage1()
    body = harness.wait_until(
        lambda snapshot: any(item["kind"] == "asset_image"
                             and item["status"] == "pending"
                             for item in snapshot["action_items"]))

    assert body["status"] == "awaiting_human"
    pending = [item for item in body["action_items"]
               if item["kind"] == "asset_image" and item["status"] == "pending"]
    assert len(pending) == 1, "素材循环一次只推进一件（严格串行）"
    assert pending[0]["asset_index"] == 1
    assert pending[0]["reason"] == ""

    # 产物：一件一份，正文头部写语义名/类型/编号，正文就是阶段 1 写好的那一份词。
    artifacts = {item["name"]: item for item in body["artifacts"]}
    assert "assets/picture-01.md" in artifacts
    assert artifacts["assets/picture-01.md"]["kind"] == "asset_prompt"
    text = harness.artifact_text("assets/picture-01.md")
    assert CAT in text and "第 1 件" in text and "Picture" in text
    assert "一只橘色短毛猫蹲在木质窗台" in text
    assert "一盏黄铜台灯" not in text, "第 1 件不该拿到第 2 件的词"

    # 台账：只登记、不定稿，轮次还是 0。
    entries = harness.ledger()
    assert [(entry["name"], entry["number"], entry["round"], entry["status"])
            for entry in entries] == [
        (CAT, 1, 0, "registered"), (LAMP, 2, 0, "registered")]

    # 进度事件用 ``asset_loop`` 这个阶段标签，节点是出词（阶段 1 的进度另算）。
    progress = [event for event in _events(harness.root, harness.session_id)
                if event["type"] == "job.progress"
                and str(event["payload"]["node"]).startswith("asset_")]
    assert progress, "出词 job 一条进度事件都没有"
    assert {event["payload"]["stage"] for event in progress} == {"asset_loop"}
    assert {event["payload"]["node"] for event in progress} == {"asset_write"}
    created = [event for event in _events(harness.root, harness.session_id)
               if event["type"] == "action_item.created"
               and event["payload"]["kind"] == "asset_image"]
    assert len(created) == 1
    assert created[0]["payload"]["asset_index"] == 1
    assert "reason" not in created[0]["payload"]


def test_base_session_approval_still_goes_straight_to_stage2(harness, monkeypatch):
    """base 一字不变：通过审查直接跑阶段 2，不出现素材循环。"""
    monkeypatch.setattr(
        pipeline, "run_stage2",
        lambda state, brief, config, on_node=None, checkpoint_dir=None:
        (state, "固定视频提示词"))
    resp = harness.client.post("/api/v1/sessions", json={
        "mode": "base", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信"})
    assert resp.status_code == 201, resp.text
    harness.session_id = resp.json()["session_id"]

    harness.start_stage1()
    harness.approve_stage1()
    body = harness.wait_until(lambda snapshot: snapshot["status"] == "completed")

    assert [item for item in body["action_items"] if item["kind"] == "asset_image"] == []
    assert any(item["name"] == "video-prompt.md" for item in body["artifacts"])


# ---------------------------------------------------------------------------
# 校对通过 / 不通过（spec §6 / §7）
# ---------------------------------------------------------------------------

CLEAN = json.dumps({"claims": []})

# 一条「描述说 A、图里是 B」的偏差。``prompt_quote`` 必须是锁死清单里那一句，
# 否则会被对照器当成理想描述出入忽略掉（#53 的收窄判据）。
CAT_DEVIATION = _claims(
    ("尾巴", "尾巴绕住前爪", "尾巴垂在窗台边缘，没绕住前爪"))


def _to_first_card(harness) -> dict:
    harness.create()
    harness.register_pictures()
    harness.start_stage1()
    harness.approve_stage1()
    return harness.wait_until(
        lambda snapshot: any(item["kind"] == "asset_image"
                             and item["status"] == "pending"
                             for item in snapshot["action_items"]))


def test_proofread_pass_settles_the_item_and_writes_the_next(harness):
    """通过：该件定稿、轮次 +1，下一件的词自动出、卡片换成第 2 件。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")  # 生成结果：普通素材，不带语义名
    harness.readings.append(CLEAN)

    resp = harness.submit_image(item, image["asset_id"])
    assert resp.status_code == 202, resp.text

    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    # 一次只推进一件：第 1 件的卡片已决议，未决项只剩第 2 件。
    pending = [entry for entry in body["action_items"]
               if entry["kind"] == "asset_image" and entry["status"] == "pending"]
    assert [entry["asset_index"] for entry in pending] == [2]

    entries = harness.ledger()
    assert [(entry["name"], entry["number"], entry["round"], entry["status"])
            for entry in entries] == [(CAT, 1, 1, "passed"), (LAMP, 2, 0, "registered")]
    assert "proofread_asset_id" not in entries[0], "定稿后不该留着待校对的图"

    text = harness.artifact_text("assets/picture-02.md")
    assert LAMP in text and "第 2 件" in text
    assert "一盏黄铜台灯立在书桌右角" in text

    # 读图走唯一入口，且请求里带着这一件的词（对照的是同一份产物）。
    assert len(harness.read_calls) == 1
    _path, instruction = harness.read_calls[0]
    assert "一只橘色短毛猫蹲在木质窗台" in instruction


def test_proofread_failure_reopens_the_same_item_with_the_diff(harness):
    """不通过：同一件号重开、差异清单进 reason，并按差异改词覆盖同一份产物。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(CAT_DEVIATION)

    resp = harness.submit_image(item, image["asset_id"])
    assert resp.status_code == 202, resp.text

    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    pending = [entry for entry in body["action_items"]
               if entry["kind"] == "asset_image" and entry["status"] == "pending"]
    assert len(pending) == 1, "重开的是同一张卡，不是再加一张"
    assert pending[0]["asset_index"] == 1
    assert pending[0]["item_id"] != item["item_id"], "旧项收掉、新项重开"
    assert "偏差" in pending[0]["reason"]
    assert "尾巴绕住前爪" in pending[0]["reason"]
    assert "尾巴垂在窗台边缘" in pending[0]["reason"]

    # 词被改写并覆盖同一份产物：头部还在，正文换成改写结果，没有第二份文件。
    text = harness.artifact_text("assets/picture-01.md")
    assert "第 1 件" in text and CAT in text
    assert "改写后的资产图词" in text
    assert "尾巴绕住前爪" not in text.split("\n\n", 1)[1]
    assert not (harness.root / Path(harness.session_id)
                / "assets" / "picture-02.md").exists()

    # 改写意见就是差异清单本身（不是一句「不通过」）。
    assert len(harness.rewrite.requests) == 1
    assert "尾巴垂在窗台边缘" in harness.rewrite.requests[0]

    entries = harness.ledger()
    assert [(entry["round"], entry["status"]) for entry in entries] == [
        (1, "registered"), (0, "registered")]
    assert "proofread_asset_id" not in entries[0]
    # 台账只记轮次与状态：重开的是同一件，不产生第二份产物文件。
    assert entries[0]["number"] == 1


def test_last_item_pass_chains_stage2(harness, monkeypatch):
    """两件都定稿后直接接阶段 2（注入卡是 #257 的事，本票不占步骤）。"""
    stage2_briefs: list[object] = []

    def record_stage2(state, brief, config, on_node=None, checkpoint_dir=None):
        stage2_briefs.append(brief)
        # 与真实 run_stage2 同一份契约：state 里带 final_prompt。只交提示词不回填
        # state，ref 的续跑判据（#258 起阶段 2 后接段循环）会认成「阶段 2 没跑过」。
        updated = dict(state)
        updated["final_prompt"] = "固定视频提示词"
        return (updated, "固定视频提示词")

    monkeypatch.setattr(pipeline, "run_stage2", record_stage2)
    body = _to_first_card(harness)
    first = [entry for entry in body["action_items"]
             if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    harness.readings.append(CLEAN)
    harness.submit_image(first, harness.upload(_png(9), "gen-cat.png")["asset_id"])

    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    second = [entry for entry in body["action_items"]
              if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    harness.readings.append(CLEAN)
    harness.submit_image(second, harness.upload(_png(10), "gen-lamp.png")["asset_id"])

    # #258 起 ref 阶段 2 之后还要进段循环，不再直接 completed；本用例只钉「最后
    # 一件定稿 → 阶段 2 开跑」，所以等阶段 2 的产物落盘，而不是终态。
    body = harness.wait_until(
        lambda snapshot: bool(stage2_briefs)
        and any(item["name"] == "video-prompt.md" for item in snapshot["artifacts"]))
    assert all(entry["status"] == "passed" for entry in harness.ledger())
    assert any(item["name"] == "video-prompt.md" for item in body["artifacts"])
    # 阶段 2 也看得到台账语义名（<Picture N> 编号要对得上），但那份派生 refs
    # **不跟着 brief 写回会话文件**——台账是唯一真源。
    assert [(ref.picture, ref.name, ref.path) for ref in stage2_briefs[0].refs] == [
        (1, CAT, ""), (2, LAMP, "")]
    assert harness.on_disk()["brief"]["refs"] == []
    # 「stage2」这一段进度属于阶段 2，不是素材循环。
    stages = {event["payload"]["stage"]
              for event in _events(harness.root, harness.session_id)
              if event["type"] == "job.progress"
              and str(event["payload"]["node"]).startswith(
                  ("prompt_engineer", "finalize"))}
    assert stages == {"stage2"}


# ---------------------------------------------------------------------------
# 载荷错配一律 422（spec §6.3 / §13）
# ---------------------------------------------------------------------------

def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status and body["code"] == code
    return body


def test_asset_image_todo_rejects_every_other_payload(harness):
    """kind 与载荷错配：放行键、帧槽、段视频、空载荷全部 422，会话仍可重交。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    url = (f"/api/v1/sessions/{harness.session_id}"
           f"/action-items/{item['item_id']}/resolve")

    _assert_problem(harness.client.post(url, json={"decision": "approve"}),
                    422, "asset_image_invalid")
    _assert_problem(harness.client.post(url, json={"frames": {"first": None}}),
                    422, "asset_image_invalid")
    _assert_problem(harness.client.post(
        url, json={"video_asset_id": "x/aaaaaaaaaaaaaaaa"}),
        422, "asset_image_invalid")
    # 空载荷在路由层就拦下（哪种都不给，无从分派）。
    assert harness.client.post(url, json={}).status_code == 422

    # 会话仍停在同一条待办上：错配没有把它决议掉。
    pending = harness.pending("asset_image")
    assert [entry["item_id"] for entry in pending] == [item["item_id"]]


def test_asset_image_accepts_only_this_sessions_image(harness):
    """外会话的素材、非图片素材一律 422；正确的那张才被收下。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    other = harness.client.post("/api/v1/sessions", json={
        "mode": "ref", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "另一个会话"})
    other_id = other.json()["session_id"]
    foreign = harness.client.post(
        f"/api/v1/sessions/{other_id}/assets",
        files={"file": ("x.png", _png(7), "application/octet-stream")}).json()

    _assert_problem(harness.submit_image(item, foreign["asset_id"]),
                    422, "asset_image_invalid")
    video = harness.upload(_mp4(3), "clip.mp4")
    _assert_problem(harness.submit_image(item, video["asset_id"]),
                    422, "asset_image_invalid")
    # 交错没有决议掉待办。
    assert len(harness.pending("asset_image")) == 1

    harness.readings.append(CLEAN)
    ok = harness.submit_image(item, harness.upload(_png(8), "gen.png")["asset_id"])
    assert ok.status_code == 202, ok.text


def test_unknown_asset_in_this_session_is_404_asset_not_found(harness):
    """本会话前缀但素材不存在 → 404 ``asset_not_found``（spec §13）。

    与「外会话素材」分成两条口径：前缀就是本会话，请求方本来就能读到这份素材清单，
    区分「不存在」不会泄漏别人会话里有什么；能探测别人才该被挡住（上一条用例仍 422
    ``asset_image_invalid``）。「还没交上来」与「交错了类型」出路不同——前者要重新
    上传，后者要换一张图，前端靠错误码分得开。
    """
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]

    ghost = f"{harness.session_id}/0123456789abcdef"
    _assert_problem(harness.submit_image(item, ghost), 404, "asset_not_found")
    # 404 没有把待办决议掉：用户仍能在同一位置重交。
    assert [entry["item_id"] for entry in harness.pending("asset_image")] == [item["item_id"]]


# ---------------------------------------------------------------------------
# 恢复：只读台账就能重建「在等第几件」（spec §14）
# ---------------------------------------------------------------------------

def test_recovery_rebuilds_the_current_card_from_the_ledger(harness):
    """待办丢了（快照被覆盖/崩溃补齐）、会话中断：再推进就从台账重建同一张卡。"""
    _to_first_card(harness)
    store = harness.client.app.state.session_store

    def _drop_card(detail):
        return {
            "status": "interrupted",
            # 只丢素材卡：真实崩溃形态是「收尾那次落盘没发生」，阶段 1 的审查待办还在。
            "action_items": [
                item.model_dump(mode="json") for item in detail.action_items
                if item.kind != "asset_image"],
        }

    store.mutate(harness.session_id, _drop_card)
    assert harness.pending("asset_image") == []

    harness.advance()
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             for entry in snapshot["action_items"]))
    pending = harness.pending("asset_image")
    assert [entry["asset_index"] for entry in pending] == [1]
    # 件号不是从 job 历史推的：台账里第 1 件还没定稿，重开的就是它。
    assert harness.ledger()[0]["status"] == "registered"
    assert "一只橘色短毛猫蹲在木质窗台" in harness.artifact_text("assets/picture-01.md")


def test_recovery_keeps_the_rewritten_prompt_of_a_rejected_round(harness):
    """打回一轮之后恢复：重开卡片**不许**把词悄悄换回阶段 1 的初稿。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    harness.readings.append(CAT_DEVIATION)
    harness.submit_image(item, harness.upload(_png(9), "gen-cat.png")["asset_id"])
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    assert "改写后的资产图词" in harness.artifact_text("assets/picture-01.md")

    store = harness.client.app.state.session_store

    def _lose_the_card(detail):
        return {
            "status": "interrupted",
            "action_items": [
                entry.model_dump(mode="json") for entry in detail.action_items
                if entry.kind != "asset_image"],
        }

    store.mutate(harness.session_id, _lose_the_card)
    harness.advance()
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             for entry in snapshot["action_items"]))

    text = harness.artifact_text("assets/picture-01.md")
    assert "改写后的资产图词" in text, "重开卡片把改过的词换回了初稿"
    assert "尾巴绕住前爪" not in text.split("\n\n", 1)[1]
    assert harness.ledger()[0]["round"] == 1


def test_recovery_runs_the_proofread_again_when_the_image_is_already_on_the_ledger():
    """崩在「已交图、未校对」之间：台账带着交图标识，恢复跑校对而不是要求重交。"""
    from minimax_h3_prompt.api.jobs import (
        ASSET_PROOFREAD_KEY,
        ASSET_STEP_PROOFREAD,
        ASSET_STEP_WRITE,
        _asset_step_of,
        _settle_asset,
    )
    from minimax_h3_prompt.workbench_store import ASSET_LEDGER_STATUS_PASSED

    ledger = [
        {"name": "橘猫", "kind": "picture", "number": 1, "round": 0,
         "status": "registered", ASSET_PROOFREAD_KEY: "s/aaaaaaaaaaaaaaaa"},
        {"name": "台灯", "kind": "picture", "number": 2, "round": 0,
         "status": "registered"},
        # 视频/音频不进素材循环（资产图只有 Picture 一件一件出）。
        {"name": "配乐", "kind": "audio", "number": 1, "round": 0,
         "status": "registered"},
    ]
    assert _asset_step_of(ledger) == (ASSET_STEP_PROOFREAD, 1)

    ledger[0].pop(ASSET_PROOFREAD_KEY)
    assert _asset_step_of(ledger) == (ASSET_STEP_WRITE, 1)

    settled = _settle_asset(ledger, 1, True)
    assert _asset_step_of(settled) == (ASSET_STEP_WRITE, 2)
    assert settled[0]["round"] == 1
    assert settled[0]["status"] == ASSET_LEDGER_STATUS_PASSED
    assert ledger[0]["round"] == 0, "不得就地改入参"

    done = _settle_asset(settled, 2, True)
    assert _asset_step_of(done)[0] == "done"


def test_asset_prompt_round_trips_through_the_artifact_header():
    """产物正文的头四行是给人看的标签，取回来必须还是同一份词（改写要用它当底本）。"""
    from minimax_h3_prompt.api.jobs import _parse_asset_prompt, _render_asset_prompt

    rendered = _render_asset_prompt(3, "橘猫", "一只橘色短毛猫。\n尾巴绕住前爪。")
    assert rendered.startswith("# 资产图提示词 · 第 3 件")
    assert "- 语义名：橘猫" in rendered and "- 类型：Picture" in rendered
    assert _parse_asset_prompt(rendered).strip() == "一只橘色短毛猫。\n尾巴绕住前爪。"


def test_missing_asset_prompt_fails_loudly_instead_of_borrowing_another_name():
    """阶段 1 没有这一件的词就响：拿相邻那件的词顶替会让用户按别人的描述出图。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.generation import AssetPromptMissing, asset_prompt_for

    brief = Brief(mode="ref", variant="T2VA", duration=4.0)
    state = {"prop_image_prompts": [
        {"name": "台灯", "positive_prompt": "一盏黄铜台灯。", "locked_features": ["黄铜"]}]}

    assert asset_prompt_for(brief, state, "台灯").positive_prompt == "一盏黄铜台灯。"
    with pytest.raises(AssetPromptMissing) as excinfo:
        asset_prompt_for(brief, state, "橘猫")
    assert "橘猫" in str(excinfo.value) and "台灯" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 接线：执行器交给管线的 brief.refs 来自素材台账（spec §5.3 / §10）
# ---------------------------------------------------------------------------

def test_ref_stage1_sees_the_ledger_as_brief_refs(harness, monkeypatch):
    """ref 会话的 brief.refs 由台账现造（语义名 = 分镜表 Subject 标签的来源）。

    工作台创建口不收 refs（#255），台账是唯一真源；不接这一处，真实 ref 会话的
    资产图提示词就永远与上传的语义名对不上号。
    """
    seen: list[object] = []

    def record_stage1(brief, config, **kwargs):
        seen.append(brief)
        # 与真实管线同形：在**入参 state** 之上产出，累积修订清单（user_revisions）
        # 要跟着流下去——否则修订重跑找不到自己那条，报的错与被测接线无关。
        state = dict(kwargs.get("initial_state") or {})
        state.update(_stage1_state({
            CAT: "一只橘色短毛猫蹲在木质窗台。",
            LAMP: "一盏黄铜台灯立在书桌右角。",
        }))
        return (state, None, None)

    monkeypatch.setattr(pipeline, "run_stage1", record_stage1)
    harness.create()
    harness.register_pictures()
    # 视频/音频不占 <Picture N> 槽位，不进 refs（它们的段内子集是 #258 的事）。
    harness.upload(_mp4(5), "clip.mp4", name="配乐")
    harness.start_stage1()

    refs = [(ref.picture, ref.name, ref.description, ref.path)
            for ref in seen[0].refs]
    assert refs == [(1, CAT, "", ""), (2, LAMP, "", "")]

    # 修订重跑（设定级，走 ``_rerun_stage1``）用的是同一条接线：重跑的还是阶段 1，
    # Subject 标签不能因为重跑一次就丢了。
    item = harness.pending("stage1_review")[0]
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/action-items/{item['item_id']}/resolve",
        json={"decision": "revise",
              "revisions": [{"layer": "人物", "text": "改成夜景"}]})
    assert resp.status_code == 202, resp.text
    harness.wait_until(
        lambda snapshot: len(seen) > 1 and any(
            entry["kind"] == "stage1_review" and entry["status"] == "pending"
            for entry in snapshot["action_items"]))
    assert [(ref.picture, ref.name, ref.path) for ref in seen[-1].refs] == [
        (1, CAT, ""), (2, LAMP, "")]

    # base 会话原样返回：refs 仍为空，台账不参与。
    seen.clear()
    resp = harness.client.post("/api/v1/sessions", json={
        "mode": "base", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信"})
    harness.session_id = resp.json()["session_id"]
    harness.start_stage1()
    assert seen[0].refs == []


def test_missing_asset_prompt_fails_the_write_job_and_names_the_asset(harness, monkeypatch):
    """名字对不上：出词 job 失败并点名缺哪一件，不静默拿别人的词。"""
    def mismatched(brief, config, **kwargs):
        return ({
            "script": "分场剧本：一位撑伞的女子走过街口。",
            "shot_table": "[Shot 1] At 00:00.000, a woman with an umbrella.",
            "character_image_prompts": [{
                "name": "人物", "positive_prompt": "一位撑黑伞的成年女性。",
                "negative_prompt": "", "locked_features": ["撑黑伞"]}],
        }, None, None)

    monkeypatch.setattr(pipeline, "run_stage1", mismatched)
    harness.create()
    harness.register_pictures()
    harness.start_stage1()

    harness.approve_stage1()
    body = harness.wait_until(lambda snapshot: snapshot["status"] == "failed")

    failures = [event["payload"] for event in _events(harness.root, harness.session_id)
                if event["type"] == "job.failed"]
    assert failures, "失败没有落成 job.failed 事件"
    detail = failures[-1]["detail"]
    assert f"「{CAT}」" in detail, detail
    assert "人物" in detail, "报错要点名现有的名字，人才知道该怎么改"
    assert not (harness.root / Path(harness.session_id)
                / "assets" / "picture-01.md").exists()
    assert [item for item in body["action_items"]
            if item["kind"] == "asset_image"] == []

