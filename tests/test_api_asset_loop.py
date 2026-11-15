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
import logging
import threading
import time
from pathlib import Path

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.config import config
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.image_config import IMAGE_MODEL_LABELS
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
        # 卡住模型调用用（#288「请求立即返回、作业在后台跑」）：置上之后 invoke 会等到
        # 用例放开才返回，用例因此能在作业仍在跑时读进度事件。
        self.gate: threading.Event | None = None

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        if self.gate is not None:
            self.gate.wait(10)
        for callback in (config or {}).get("callbacks") or []:
            callback.on_llm_new_token("…")
        return self.reply


def _stage1_state(prompts: dict[str, str],
                  models: dict[str, str] | None = None) -> dict:
    """阶段 1 的固定 state：剧本、分镜表与逐件资产图提示词（按语义名命名）。

    ``models`` 给某几件写下出词时的档位判定（``image_routing`` 的形状：四键对象）；
    没给的那一件就是 #292 之前的旧条目（没有 ``image_model``，读成「未知」）。
    """
    models = models or {}
    entries = []
    for name, text in prompts.items():
        entry = {
            "name": name,
            "positive_prompt": text,
            "negative_prompt": "",
            # 锁死清单必须是正文片段（对照器按字面收窄偏差）。
            "locked_features": [text],
        }
        if name in models:
            entry["image_model"] = {
                "model": models[name], "source": "auto",
                "keyword": "", "evidence": "",
            }
        entries.append(entry)
    return {
        "script": "分场剧本：橘猫蹲在窗台，台灯亮着。",
        "shot_table": "[Shot 1] At 00:00.000, a medium shot of the cat by the lamp.",
        "character_image_prompts": entries,
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

    def wait_until(self, predicate, timeout: float = 15.0) -> dict:
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

    # 台账：阶段 1 收尾就落齐、一件都还没交（``pending``），来源是流程派生、轮次 0。
    entries = harness.ledger()
    assert [(entry["name"], entry["number"], entry["round"], entry["status"],
             entry["source"]) for entry in entries] == [
        (CAT, 1, 0, "pending", "derived"), (LAMP, 2, 0, "pending", "derived")]
    assert [entry["asset_id"] for entry in entries] == ["", ""], "没交回就没有正本"
    assert harness.on_disk()["asset_manifest_derived"] is True

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


def test_derived_rows_follow_script_order_not_prompt_order(harness, monkeypatch):
    """编号＝剧本出场序（#279）：资产图词里的枚举顺序不作数。

    出词节点按类别枚举条目，叙事顺序只写在剧本里。两处各排一次，台账编号就会与分镜表
    的 ``<Subject N>``／``<Picture N>`` 错位——那种错位在段内子集里才发作。
    """
    def script_order(brief, config, **kwargs):
        return ({
            # 剧本先写台灯、后写橘猫；资产图词列表是**反的**。
            "script": "分场剧本：台灯亮着，橘猫蹲在窗台。",
            "shot_table": "[Shot 1] At 00:00.000, a desk lamp by the window.",
            "character_image_prompts": [
                {"name": LAMP, "positive_prompt": "一盏黄铜台灯。",
                 "negative_prompt": "", "locked_features": []},
                {"name": CAT, "positive_prompt": "一只橘色短毛猫。",
                 "negative_prompt": "", "locked_features": []},
            ],
        }, None, None)

    monkeypatch.setattr(pipeline, "run_stage1", script_order)
    harness.create()
    harness.start_stage1()

    assert [(entry["name"], entry["number"]) for entry in harness.ledger()] == [
        (LAMP, 1), (CAT, 2)]


def test_derived_rows_carry_their_image_model_and_display_label(harness, monkeypatch):
    """#292：每件派生素材把**这一件**的生图档位落进台账，详情响应同时给键与显示名。

    档位的唯一事实源是阶段 1 条目上的 ``image_model``（``image_routing``）；显示名由
    服务端按全仓那一份映射渲染，前端直接显示——两件分属两档，正好钉住「逐件取，
    不按类套一张表」。
    """
    monkeypatch.setattr(
        pipeline, "run_stage1",
        lambda brief, config, **kwargs: (
            _stage1_state({
                CAT: "一只橘色短毛猫蹲在木质窗台，尾巴绕住前爪。",
                LAMP: "一盏黄铜台灯立在书桌右角，灯罩是墨绿色。",
            }, {CAT: "qwen", LAMP: "moss"}), None, None))
    body = _to_first_card(harness)

    rows = {ref["number"]: ref for ref in body["brief"]["refs"]}
    assert rows[1]["image_model"] == "qwen"
    assert rows[1]["image_model_label"] == IMAGE_MODEL_LABELS["qwen"]
    assert rows[2]["image_model"] == "moss"
    assert rows[2]["image_model_label"] == IMAGE_MODEL_LABELS["moss"]
    # 台账行自己也带着键（详情是台账的摘要，不是读详情时现算的）。
    assert [entry["image_model"] for entry in harness.ledger()] == ["qwen", "moss"]


def test_ledger_rows_without_a_recorded_model_stay_unknown(harness):
    """#292：旧台账行没有档位字段时，详情给空串（界面说「未知」），不编造一档。

    判的是**台账**缺字段这一档（#292 之前落的行），不是「条目没有记录」——后者按
    ``image_prompt_variants`` 的同一条历史读法读成 Z-Image（那时只有这一档）。
    """
    _to_first_card(harness)
    path = harness.root / Path(harness.session_id) / SESSION_FILENAME
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    for entry in snapshot["asset_ledger"]:
        entry.pop("image_model", None)
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    rows = harness.detail()["brief"]["refs"]
    assert [(ref["image_model"], ref["image_model_label"])
            for ref in rows] == [(None, None), (None, None)]


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
            for entry in entries] == [(CAT, 1, 1, "passed"), (LAMP, 2, 0, "pending")]
    # 定稿的那一件才认下正本（#279）：派生时 id 是空的，交回并通过才有。
    assert entries[0]["asset_id"] == image["asset_id"]
    assert entries[1]["asset_id"] == ""
    assert "proofread_asset_id" not in entries[0], "定稿后不该留着待校对的图"

    text = harness.artifact_text("assets/picture-02.md")
    assert LAMP in text and "第 2 件" in text
    assert "一盏黄铜台灯立在书桌右角" in text

    # 读图走唯一入口，且请求里带着这一件的词（对照的是同一份产物）。
    assert len(harness.read_calls) == 1
    _path, instruction = harness.read_calls[0]
    assert "一只橘色短毛猫蹲在木质窗台" in instruction


def test_proofread_failure_reopens_the_same_item_and_leaves_the_prompt_untouched(
        harness):
    """不通过：同一件号重开、差异清单进 reason；**生图词与校对前逐字一致**（#287）。

    判完只给裁决，不替人改设计：词是指令、图是既成事实（#286 / ADR 0039 口径）。
    改写模型一次都不许叫——叫了即使不落盘也是白花钱，还说明防线只剩一层。
    """
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(CAT_DEVIATION)
    before = harness.artifact_text("assets/picture-01.md")

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
    # #292：结论行注明是谁读的图——取视觉客户端同一份配置的模型名，不另立常量。
    assert pending[0]["vision_model"] == config.vision_model.model

    # 盘上那一份一个字节都不动：头部、正文、换行全部原样。
    assert harness.artifact_text("assets/picture-01.md") == before
    assert not (harness.root / Path(harness.session_id)
                / "asset-rounds.jsonl").exists(), "校对不许落轮次账"
    # 改写模型一次都没叫：差异清单只是给人看的，不是给模型当 feedback 的。
    assert harness.rewrite.requests == []

    entries = harness.ledger()
    # 第 1 件打回后回到 pending（「交过、没过，等重交」）；第 2 件还没轮到，也是 pending。
    assert [(entry["round"], entry["status"]) for entry in entries] == [
        (1, "pending"), (0, "pending")]
    assert "proofread_asset_id" not in entries[0]
    # 台账只记轮次与状态：重开的是同一件，不产生第二份产物文件。
    assert entries[0]["number"] == 1


def test_omissions_without_deviations_settle_the_item_and_stay_visible(harness):
    """只有补漏、没有偏差：这一件直接定稿，补漏条目写进**刚决议那张卡**的 reason。

    通过＝卡片不会再回来，那张卡是补漏唯一的可见面（`GET /action-items` 与详情都读
    同一个字段）。#289 会在这条 reason 之后追加「已按图补入 N 条」。
    """
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    before = harness.artifact_text("assets/picture-01.md")
    # 空引文＝「图里有而描述没有」：补漏一条，偏差零条。
    harness.readings.append(_claims(("道具", "", "窗台边还有一只毛线球")))

    resp = harness.submit_image(item, image["asset_id"])
    assert resp.status_code == 202, resp.text

    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    # 补漏不阻断：这一件定稿、轮次 +1，循环照常接下一件。
    entries = harness.ledger()
    assert [(entry["round"], entry["status"]) for entry in entries] == [
        (1, "passed"), (0, "pending")]
    assert harness.artifact_text("assets/picture-01.md") == before

    resolved = [entry for entry in body["action_items"]
                if entry["item_id"] == item["item_id"]][0]
    assert resolved["status"] == "resolved"
    assert "[补漏]" in resolved["reason"]
    assert "毛线球" in resolved["reason"]
    # #292：读图模型名随补报告落在同一张已决议卡片上。
    assert resolved["vision_model"] == config.vision_model.model

    # 待办报告接口与详情是同一份快照，都要看得到。
    listed = harness.client.get(
        f"/api/v1/sessions/{harness.session_id}/action-items").json()
    listed_item = [entry for entry in listed
                   if entry["item_id"] == item["item_id"]][0]
    assert "毛线球" in listed_item["reason"]


# ---------------------------------------------------------------------------
# 读图不可用（#290 / spec #286「读图不可用」）
# ---------------------------------------------------------------------------

def _settle_as_unproofread(harness, item: dict, before: str,
                           step: str, cause: str) -> None:
    """三种不可用共用的判据：这一件照常定稿，结论落在刚决议那张卡上。

    「没交图」与「没校对」不能混成同一个状态（spec US25），所以台账必须是
    ``passed``；通过分支本来就不改写、不写轮次账、不挂交图卡，三条都当场复核。
    """
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    entries = harness.ledger()
    assert [(entry["round"], entry["status"]) for entry in entries] == [
        (1, "passed"), (0, "pending")], "读图不可用也照常定稿，轮次与通过同路"
    assert "proofread_asset_id" not in entries[0]

    assert harness.artifact_text("assets/picture-01.md") == before, "不许改写生图词"
    assert harness.rewrite.requests == [], "读图不可用也不许叫改写模型"
    assert not (harness.root / Path(harness.session_id)
                / "asset-rounds.jsonl").exists(), "不许写资产轮次账"
    # 不挂新的交图卡：未决项只剩第 2 件（第 1 件没有被打回重开）。
    pending = [entry for entry in body["action_items"]
               if entry["kind"] == "asset_image" and entry["status"] == "pending"]
    assert [entry["asset_index"] for entry in pending] == [2]

    # 通过分支卡片不再回来，刚决议那张卡的 reason 是唯一的可见面。
    resolved = [entry for entry in body["action_items"]
                if entry["item_id"] == item["item_id"]][0]
    assert resolved["status"] == "resolved"
    reason = resolved["reason"]
    assert reason.startswith("校对未完成："), reason
    assert step in reason, "原因要点名是哪一步失败"
    assert cause in reason, "原因要带原始错误摘要"
    # #292：不可用时照报配置里的那一档——它仍是排查「该谁来读」的线索。
    assert resolved["vision_model"] == config.vision_model.model
    # 待办报告接口与详情是同一份快照，都要看得到。
    listed = harness.client.get(
        f"/api/v1/sessions/{harness.session_id}/action-items").json()
    listed_item = [entry for entry in listed
                   if entry["item_id"] == item["item_id"]][0]
    assert reason == listed_item["reason"]
    # 事件类型仍是十种闭合集合里那一条：只给既有 payload 多一个 reason 键。
    reports = [event for event in _events(harness.root, harness.session_id)
               if event["type"] == "action_item.resolved"
               and event["payload"].get("item_id") == item["item_id"]
               and event["payload"].get("reason")]
    assert len(reports) == 1 and reports[0]["payload"]["reason"] == reason


def _submit_first_image(harness) -> tuple[dict, dict, str]:
    """交第 1 件的图，返回（那张卡、素材、交图前的生图词正文）。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    return (item, harness.upload(_png(9), "gen-cat.png"),
            harness.artifact_text("assets/picture-01.md"))


def test_a_failed_reading_call_settles_the_item_and_names_the_step(
        harness, monkeypatch, caplog):
    """读图调用失败（网络/超时/5xx）：不重试、不停循环，定稿并点名「读图调用失败」。

    捕获后必须留日志（本仓纪律：绝不静默失败）——reason 只说给用户听，完整现场进日志。
    """
    item, image, before = _submit_first_image(harness)

    def boom(path, instruction=None):
        raise RuntimeError("读图超时：首字节线内没有任何数据（ADR 0019）")

    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    with caplog.at_level(logging.WARNING,
                         logger="minimax_h3_prompt.api.jobs"):
        assert harness.submit_image(item, image["asset_id"]).status_code == 202
        _settle_as_unproofread(harness, item, before, "读图调用失败",
                               "首字节线内没有任何数据")
    assert any("校对未完成" in record.getMessage() for record in caplog.records), \
        "读图不可用被捕获了却没留日志＝静默吞异常"


def test_an_unparsable_reading_settles_the_item_and_names_the_step(
        harness, monkeypatch):
    """结论解析失败：模型回了一段不是约定的 JSON，本件照常定稿并点名这一步。"""
    item, image, before = _submit_first_image(harness)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: "模型今天只说人话，不给 JSON")
    assert harness.submit_image(item, image["asset_id"]).status_code == 202

    _settle_as_unproofread(harness, item, before, "结论解析失败",
                           "读图结果不是 JSON")


def test_an_unavailable_vision_client_settles_the_item_and_names_the_step(
        harness, monkeypatch):
    """视觉客户端不可用：建客户端这一步就失败（配置缺失/依赖没装），照常定稿。"""
    item, image, before = _submit_first_image(harness)

    def unavailable(path, instruction=None):
        raise reference_auditor.VisionClientUnavailable(
            "缺失 DASHSCOPE_API_KEY：参考图审核需要视觉模型 deepseek-v4.1-flash-1017")

    monkeypatch.setattr(reference_auditor, "describe_image", unavailable)
    assert harness.submit_image(item, image["asset_id"]).status_code == 202

    _settle_as_unproofread(harness, item, before, "视觉客户端不可用",
                           "缺失 DASHSCOPE_API_KEY")


def test_the_proofread_report_survives_snapshot_replay(harness):
    """快照落后事件流时，补漏报告与读图模型名由 ``action_item.resolved`` 重放补回。

    事件类型仍是十种闭合集合里那一条：只给既有 payload 多 ``reason`` 与
    ``vision_model`` 两个键（前一条 #287，后一条 #292）。
    """
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(_claims(("道具", "", "窗台边还有一只毛线球")))
    assert harness.submit_image(item, image["asset_id"]).status_code == 202
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))

    events = _events(harness.root, harness.session_id)
    report_event = [event for event in events
                    if event["type"] == "action_item.resolved"
                    and event["payload"].get("item_id") == item["item_id"]
                    and event["payload"].get("reason")][-1]
    assert report_event["payload"]["reason"].count("毛线球") == 1
    assert report_event["payload"]["vision_model"] == config.vision_model.model

    # 手工制造「事件已写、快照没跟上」：抹掉快照里的报告，把尾号退到那条事件之前。
    path = harness.root / Path(harness.session_id) / SESSION_FILENAME
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["last_event_seq"] = report_event["seq"] - 1
    for entry in snapshot["action_items"]:
        if entry["item_id"] == item["item_id"]:
            entry["reason"] = ""
            entry["vision_model"] = ""
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    replayed = [entry for entry in harness.detail()["action_items"]
                if entry["item_id"] == item["item_id"]][0]
    assert "毛线球" in replayed["reason"], "事件重放没有把补漏报告补回"
    assert replayed["vision_model"] == config.vision_model.model, \
        "事件重放没有把读图模型名补回（刷新后那一行会退回「未知」）"
    # 报告事件不是第二次决议：决议时间仍记第一次交图的那一刻。
    assert replayed["resolved_at"] == [entry for entry in body["action_items"]
                                       if entry["item_id"] == item["item_id"]][0][
                                           "resolved_at"]


def test_last_item_pass_opens_the_extras_gate_before_stage2(harness, monkeypatch):
    """两件都定稿后**不直接进阶段 2**：先挂补交口，选「没有」才开跑（#284）。"""
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

    # 最后一件定稿只到补交口：阶段 2 一次都没跑，待办在等「要不要补交视频/音频」。
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_extra"
                             and entry["status"] == "pending"
                             for entry in snapshot["action_items"]))
    assert body["status"] == "awaiting_human"
    assert stage2_briefs == [], "补交口未解就跑到了阶段 2"
    extra = [entry for entry in body["action_items"]
             if entry["kind"] == "asset_extra" and entry["status"] == "pending"][0]
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/action-items/"
        f"{extra['item_id']}/resolve",
        json={"decision": "no_extras"})
    assert resp.status_code == 202, resp.text

    # #258 起 ref 阶段 2 之后还要进段循环，不再直接 completed；本用例只钉「补交口
    # 放行 → 阶段 2 开跑」，所以等阶段 2 的产物落盘，而不是终态。
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
    # 件号不是从 job 历史推的：台账里第 1 件还没定稿（派生后一件都没交），重开的就是它。
    assert harness.ledger()[0]["status"] == "pending"
    assert "一只橘色短毛猫蹲在木质窗台" in harness.artifact_text("assets/picture-01.md")


def test_recovery_keeps_the_prompt_of_a_rejected_round(harness):
    """打回一轮之后恢复：重开卡片**不许**动盘上的词（#287 起它本来就没被改过）。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    before = harness.artifact_text("assets/picture-01.md")
    harness.readings.append(CAT_DEVIATION)
    harness.submit_image(item, harness.upload(_png(9), "gen-cat.png")["asset_id"])
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    assert harness.artifact_text("assets/picture-01.md") == before

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

    assert harness.artifact_text("assets/picture-01.md") == before, (
        "重开卡片动了盘上的词")
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
    from minimax_h3_prompt.workbench_store import (
        ASSET_LEDGER_STATUS_PASSED,
        ASSET_LEDGER_STATUS_PENDING,
    )

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

    # 不通过回 ``pending``（spec #279：registered 只覆盖「交回了、还没校对过」那一段）：
    # 一份已被判不通过的图不该继续挂在 registered 上。
    rejected = _settle_asset(ledger, 1, False)
    assert rejected[0]["status"] == ASSET_LEDGER_STATUS_PENDING
    assert rejected[0]["round"] == 1


def test_asset_prompt_round_trips_through_the_artifact_header():
    """产物正文的头四行是给人看的标签，取回来必须还是同一份词（改写要用它当底本）。

    解析住在 ``workbench_store``（详情读取的恢复判定也要用同一条规则），渲染仍在
    ``api.jobs``：两侧靠那个空行对齐。
    """
    from minimax_h3_prompt.api.jobs import _render_asset_prompt
    from minimax_h3_prompt.workbench_store import parse_asset_prompt

    rendered = _render_asset_prompt(3, "橘猫", "一只橘色短毛猫。\n尾巴绕住前爪。")
    assert rendered.startswith("# 资产图提示词 · 第 3 件")
    assert "- 语义名：橘猫" in rendered and "- 类型：Picture" in rendered
    assert parse_asset_prompt(rendered).strip() == "一只橘色短毛猫。\n尾巴绕住前爪。"


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
# 接线：ref 的阶段 1 **不**注入台账 refs（#279）——注入了派生就被堵回去
# ---------------------------------------------------------------------------

def test_ref_stage1_never_sees_the_ledger_as_brief_refs(harness, monkeypatch):
    """首跑与修订重跑都不注入 refs。

    ``generation.ref_asset_registry`` 有一条「``brief.refs`` 非空就以用户登记为准」的
    支路，且它排在路线守卫之前：阶段 1 一旦拿到注入的 refs，编号/名字/顺序全由台账
    决定、设计师清单被绕过——素材集合又回到「用户预置」，正是 #279 要拆的死锁。

    修订重跑必须一起钉：漏掉那两处，「审查后改一句」会重新吃回注入 refs，而首跑路径
    的用例全绿（这正是本仓「接线在别处」的典型形态）。
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
    # 台账里**已经有一件定稿的 Picture** 再跑阶段 1：这是最容易吃回注入的真实现场
    # ——审查后修订会把阶段 1 再跑一遍，而那时台账早已有 passed 行。台账空着时两种
    # 写法都返回空 refs，守卫就是摆设（变异检验会报「存活」）。
    harness.upload(_png(1), "cat.png", name=CAT)
    harness.upload(_mp4(5), "clip.mp4", name="配乐")
    harness.client.app.state.session_store.mutate(
        harness.session_id,
        lambda _detail: {"asset_ledger": [
            ({**entry, "status": "passed"} if entry["kind"] == "picture" else entry)
            for entry in _detail.asset_ledger_snapshot()]})
    harness.start_stage1()

    assert seen[0].refs == [], "阶段 1 拿到了注入的 refs：派生会被「用户登记为准」堵回去"
    # 派生落行：件数与名字来自阶段 1 的资产图词，与 refs 无关。视频/音频行不参与派生，
    # 原样留着（它们由循环之后的补交口管，#284）。
    assert [(entry["kind"], entry["name"], entry["status"])
            for entry in harness.ledger()] == [
        ("picture", CAT, "pending"), ("picture", LAMP, "pending"),
        ("video", "配乐", "registered")]

    # 修订重跑（设定级，走 ``_rerun_stage1``）：重跑的还是阶段 1，同样不许注入。
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
    assert seen[-1].refs == [], "修订重跑又把台账 refs 注入回阶段 1 了"

    # base 会话原样返回：refs 仍为空，台账不参与。
    seen.clear()
    resp = harness.client.post("/api/v1/sessions", json={
        "mode": "base", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信"})
    harness.session_id = resp.json()["session_id"]
    harness.start_stage1()
    assert seen[0].refs == []


def test_manifest_and_prompts_mismatch_fails_stage1_and_names_both_sides(
        harness, monkeypatch):
    """清单与资产图词对不上：阶段 1 收尾当场硬失败，并同时点名两边。

    「落行与对账是同一处」（#279）：清单里有、词里没有（缺件），或词里有、清单里没有
    （多件／改名），都不许落进台账——错位的词进了台账，素材循环会按名字去要一件不存在
    的资产，而错的是哪一张不会有第二个信号。文案要同时给出「现有」与「缺失」，人才能
    看出该改哪一边。
    """
    def mismatched(brief, config, **kwargs):
        return ({
            "script": "分场剧本：一位撑伞的女子走过街口。",
            "shot_table": "[Shot 1] At 00:00.000, a woman with an umbrella.",
            "character_asset_manifest": [CAT, LAMP],
            "character_image_prompts": [{
                "name": "人物", "positive_prompt": "一位撑黑伞的成年女性。",
                "negative_prompt": "", "locked_features": ["撑黑伞"]}],
        }, None, None)

    monkeypatch.setattr(pipeline, "run_stage1", mismatched)
    harness.create()
    # 零素材起跑（#279）：不再需要先上传图片。
    harness.advance()
    body = harness.wait_until(lambda snapshot: snapshot["status"] == "failed")

    failures = [event["payload"] for event in _events(harness.root, harness.session_id)
                if event["type"] == "job.failed"]
    assert failures, "失败没有落成 job.failed 事件"
    detail = failures[-1]["detail"]
    assert CAT in detail and LAMP in detail, "缺失的那两件要逐一点名"
    assert "人物" in detail, "现有的名字也要点名，人才知道该怎么改"
    # 失败时一件都不许落进台账：半份清单比没有清单更难收拾。
    assert harness.ledger() == []
    assert harness.on_disk().get("asset_manifest_derived") in (False, None)
    assert not any(item["kind"] == "stage1_review" for item in body["action_items"])
    assert not (harness.root / Path(harness.session_id)
                / "assets" / "picture-01.md").exists()


# ---------------------------------------------------------------------------
# 循环的边界动作（#279 §循环的边界动作 / #282）
# ---------------------------------------------------------------------------

def _card(harness, number: int) -> dict:
    """等第 ``number`` 件的待办卡挂出来，返回它。"""
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == number
                             for entry in snapshot["action_items"]))
    return [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]


def _resolve(harness, item: dict, payload: dict):
    return harness.client.post(
        f"/api/v1/sessions/{harness.session_id}"
        f"/action-items/{item['item_id']}/resolve", json=payload)


def _reopen(harness, asset_id: str):
    return harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/assets/{asset_id}/reopen")


def test_abandoning_an_item_skips_it_and_lets_the_next_one_proceed(harness):
    """放弃一件：行标 abandoned、进度跨过它，下一件照常出词；已定稿的文本不动。

    「最大连续通过编号 + 1」若不跳过放弃行，用户点了放弃反而更走不动——放弃键成了
    第二种死锁。代价要在待办文案与事件里当场讲清（#279 US10），而不许系统替用户去改
    分镜表与段词：它写的是「这个角色在这一段做什么」，删改等于悄悄改剧本。
    """
    _to_first_card(harness)
    shot_table = harness.artifact_text("shot-table.md")
    first = _card(harness, 1)

    resp = _resolve(harness, first, {"decision": "abandon_asset"})
    assert resp.status_code == 202, resp.text

    _card(harness, 2)
    entries = harness.ledger()
    assert [(entry["name"], entry["status"], entry["asset_id"])
            for entry in entries] == [(CAT, "abandoned", ""), (LAMP, "pending", "")]

    # 待办文案：决议后的那一条带着代价，件号仍是 1。
    resolved_item = next(entry for entry in harness.detail()["action_items"]
                         if entry["item_id"] == first["item_id"])
    assert resolved_item["status"] == "resolved"
    assert resolved_item["decision"] == "abandon_asset"
    assert CAT in resolved_item["reason"] and "无图指称" in resolved_item["reason"]

    # 事件流里也留一句话（同一份文案，不另写一份）。
    resolved = [event for event in _events(harness.root, harness.session_id)
                if event["type"] == "action_item.resolved"
                and event["payload"]["decision"] == "abandon_asset"]
    assert len(resolved) == 1
    assert CAT in resolved[0]["payload"]["reason"]

    # 已经定稿的文本一个字都没改：放弃的是图，不是剧本。
    assert shot_table.strip(), "分镜表本来就该在阶段 1 落盘"
    assert harness.artifact_text("shot-table.md") == shot_table


def test_abandon_cannot_carry_an_image_and_is_not_an_approval(harness):
    """放弃与交图互斥；approve/revise 仍然不是素材卡的决议。"""
    _to_first_card(harness)
    first = _card(harness, 1)
    image = harness.upload(_png(31), "gen.png")

    _assert_problem(
        _resolve(harness, first, {"decision": "abandon_asset",
                                  "asset_id": image["asset_id"]}),
        422, "asset_image_invalid")
    _assert_problem(_resolve(harness, first, {"decision": "approve"}),
                    422, "asset_image_invalid")
    # 两次错配都没有把卡片决议掉：用户还能正常交图或放弃。
    assert [entry["item_id"] for entry in harness.pending("asset_image")] == [
        first["item_id"]]


def test_position_lock_sends_every_named_picture_upload_to_the_card(harness):
    """位置锁（#279 复审 / spec §12）：循环里带语义名的 Picture 全拒，去处是待办卡。

    上一版放行「当前件」那一次，只把文件落盘、台账不动——用户以为交了一张图，系统
    却既没校对也没记账。现在连当前件自己的名字也 409，文案指向素材待办卡。
    """
    _to_first_card(harness)

    for seed, name in ((41, CAT), (42, LAMP)):
        blocked = harness.client.post(
            f"/api/v1/sessions/{harness.session_id}/assets",
            files={"file": ("named.png", _png(seed), "application/octet-stream")},
            data={"name": name})
        body = _assert_problem(blocked, 409, "asset_locked")
        assert "素材待办卡" in body["detail"] and "校对" in body["detail"]

    entries = harness.ledger()
    assert entries[0]["asset_id"] == "", "被拒的上传不许悄悄绑到台账行上"
    assert entries[0]["status"] == "pending"

    plain = harness.upload(_png(43), "gen.png")
    assert plain["number"] is None, "不带语义名的上传不进台账"


def test_submitting_an_image_marks_the_row_registered_until_proofread(harness, monkeypatch):
    """交图决议即 ``registered``（spec #279「台账与进度」：交回了、还没校对过）。

    读图卡在这里等放行，是为了在校对**真的还没出结论**的那一刻读台账；不挡的话，
    这条转移会被紧接着的校对结论盖过去，「registered 存在过」就测不到了。
    """
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(91), "gen-cat.png")

    started = threading.Event()
    release = threading.Event()

    def blocked_describe(path, instruction=None):
        started.set()
        assert release.wait(10), "用例没有放行读图"
        return CLEAN

    monkeypatch.setattr(reference_auditor, "describe_image", blocked_describe)
    assert harness.submit_image(item, image["asset_id"]).status_code == 202
    assert started.wait(10), "校对任务没有读到图"
    entries = harness.ledger()
    assert entries[0]["status"] == "registered", "交回、未校对的那一段必须可见"

    release.set()
    harness.wait_until(lambda snapshot: harness.ledger()[0]["status"] == "passed")
    # 下一件（第 2 件）的出词 job 会立刻接上，所以只等这一条转移落定，不等整条链停。
    assert harness.ledger()[0]["status"] == "passed"


def test_reopening_a_passed_item_inside_the_loop_rehangs_its_card(harness):
    """循环内换一张图：该行退回待交、按这一件重挂卡片，重新交图能重新定稿。"""
    body = _to_first_card(harness)
    first = [entry for entry in body["action_items"]
             if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    old_image = harness.upload(_png(51), "gen-cat.png")
    harness.readings.append(CLEAN)
    assert harness.submit_image(first, old_image["asset_id"]).status_code == 202
    _card(harness, 2)

    resp = _reopen(harness, old_image["asset_id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["asset_index"] == 1

    # 每件同时最多一个未决待办：第 2 件那张卡片先收掉，换成第 1 件重开的这一张。
    pending = harness.pending("asset_image")
    assert [entry["asset_index"] for entry in pending] == [1]
    assert [(entry["number"], entry["status"], entry["asset_id"], entry["round"])
            for entry in harness.ledger()] == [
        (1, "pending", "", 1), (2, "pending", "", 0)]

    # 再交一张不同的图：重新读图校对、重新定稿，然后循环照常往下走。
    new_image = harness.upload(_png(52), "gen-cat-2.png")
    harness.readings.append(CLEAN)
    assert harness.submit_image(pending[0], new_image["asset_id"]).status_code == 202
    _card(harness, 2)
    entries = harness.ledger()
    assert entries[0]["status"] == "passed"
    assert entries[0]["asset_id"] == new_image["asset_id"]
    assert entries[0]["round"] == 2, "换图走的是完整的一轮校对"


def test_reopen_after_the_loop_ended_is_refused(harness, monkeypatch):
    """循环结束之后不开这个口（#279）：注入卡与段都在下游用掉了那份快照。"""
    monkeypatch.setattr(
        pipeline, "run_stage2",
        lambda state, brief, config, on_node=None, checkpoint_dir=None:
        (dict(state, final_prompt="固定视频提示词"), "固定视频提示词"))
    body = _to_first_card(harness)
    first = [entry for entry in body["action_items"]
             if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    first_image = harness.upload(_png(61), "gen-cat.png")
    harness.readings.append(CLEAN)
    assert harness.submit_image(first, first_image["asset_id"]).status_code == 202
    second = _card(harness, 2)
    harness.readings.append(CLEAN)
    assert harness.submit_image(
        second, harness.upload(_png(62), "gen-lamp.png")["asset_id"]).status_code == 202

    # 等会话停稳（阶段 2 → 段循环）：此时素材循环早已走完（两件都定稿）。
    harness.wait_idle()

    body = _assert_problem(_reopen(harness, first_image["asset_id"]),
                           409, "asset_reopen_unsupported")
    assert "循环" in body["detail"]


def test_reopening_an_abandoned_item_is_refused(harness):
    """已放弃的件没有可换的图：出路是改流程，不是再换一张。"""
    body = _to_first_card(harness)
    first = [entry for entry in body["action_items"]
             if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    # 先交一张、校对不通过（行退回 pending、asset_id 还指着那张图），再放弃。
    rejected = harness.upload(_png(71), "gen-cat.png")
    harness.readings.append(CAT_DEVIATION)
    assert harness.submit_image(first, rejected["asset_id"]).status_code == 202
    retried = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    card = [entry for entry in retried["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    assert _resolve(harness, card, {"decision": "abandon_asset"}).status_code == 202

    assert harness.ledger()[0]["status"] == "abandoned"
    _card(harness, 2)  # 放弃之后流程照常接下一件，等它停稳再试换图
    _assert_problem(_reopen(harness, rejected["asset_id"]),
                    409, "asset_not_reopenable")


def test_reopen_is_ref_only(harness):
    """导演台 r2v 与 base 都不开这个口：那不是一条「素材循环」。"""
    base = harness.client.post("/api/v1/sessions", json={
        "mode": "base", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信"})
    base_id = base.json()["session_id"]
    asset = harness.client.post(
        f"/api/v1/sessions/{base_id}/assets",
        files={"file": ("帧.png", _png(81), "application/octet-stream")}).json()

    _assert_problem(
        harness.client.post(
            f"/api/v1/sessions/{base_id}/assets/{asset['asset_id']}/reopen"),
        409, "asset_reopen_unsupported")


def test_reopen_unknown_asset_is_not_found(harness):
    """不属于本会话台账的素材标识 → 404（与素材读取同一口径，不给探测 oracle）。"""
    _to_first_card(harness)

    _assert_problem(
        _reopen(harness, f"{harness.session_id}/0123456789abcdef"),
        404, "asset_not_found")


# ---------------------------------------------------------------------------
# 校对不通过的两条出路（#288）：默认改词再掷（后台作业）与原词再掷
# ---------------------------------------------------------------------------

CAT_PROMPT = "一只橘色短毛猫蹲在木质窗台，尾巴绕住前爪。"
# 加法改写的桩回复：**逐字带上锁死条目**再补一句（守卫要能看见「原文还在」）。
REWORDED_PROMPT = CAT_PROMPT + "尾巴在前爪上方绕一圈，压住爪背。"


def test_reword_exits_are_declared_decision_values():
    """两条出路都是决议值（闭合表与常量两处），仍走既有的 resolve 端点。"""
    from minimax_h3_prompt import step_machine
    from minimax_h3_prompt.workbench_store import _DECISIONS

    assert step_machine.DECISION_REWORD_REROLL == "reword_reroll"
    assert step_machine.DECISION_REROLL == "reroll"
    assert {step_machine.DECISION_REWORD_REROLL, step_machine.DECISION_REROLL} <= _DECISIONS


def _to_failed_first_card(harness) -> tuple[dict, dict, str]:
    """走到「第 1 件校对不通过、卡片带着差异清单重开」这一步。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(CAT_DEVIATION)
    assert harness.submit_image(item, image["asset_id"]).status_code == 202
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    card = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    return body, card, harness.artifact_text("assets/picture-01.md")


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("条件没有在限时内满足")


def test_proofread_failure_carries_a_structured_conclusion(harness):
    """不通过重开的卡片带结构化偏差清单：改词作业按它驱动，不从 reason 反解析。"""
    _body, card, _before = _to_failed_first_card(harness)

    assert card["proofread"] == {
        "deviations": [{
            "aspect": "尾巴",
            "prompt_quote": "尾巴绕住前爪",
            "observed": "尾巴垂在窗台边缘，没绕住前爪",
        }],
        "omissions": [],
    }
    # 同一份结论随 ``action_item.created`` 走：重放能补回（事件类型不新增）。
    created = [event for event in _events(harness.root, harness.session_id)
               if event["type"] == "action_item.created"
               and event["payload"].get("item_id") == card["item_id"]][0]
    assert created["payload"]["proofread"]["deviations"][0]["prompt_quote"] == "尾巴绕住前爪"


def test_structured_conclusion_survives_snapshot_replay(harness):
    """快照丢掉整条待办时，结构化结论由创建事件重放补回（#288）。"""
    _body, card, _before = _to_failed_first_card(harness)
    created = [event for event in _events(harness.root, harness.session_id)
               if event["type"] == "action_item.created"
               and event["payload"].get("item_id") == card["item_id"]][0]

    path = harness.root / Path(harness.session_id) / SESSION_FILENAME
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["last_event_seq"] = created["seq"] - 1
    snapshot["action_items"] = [
        entry for entry in snapshot["action_items"]
        if entry["item_id"] != card["item_id"]]
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    replayed = [entry for entry in harness.detail()["action_items"]
                if entry["item_id"] == card["item_id"]][0]
    assert replayed["proofread"]["deviations"][0]["prompt_quote"] == "尾巴绕住前爪"


def test_reword_reroll_runs_as_a_background_job_and_only_adds(harness):
    """默认出路：请求立即返回、改词在后台跑；成功后词已更新且锁死条目逐字仍在。"""
    _body, card, before = _to_failed_first_card(harness)
    harness.rewrite.reply = REWORDED_PROMPT
    harness.rewrite.gate = threading.Event()

    resp = _resolve(harness, card, {"decision": "reword_reroll"})
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["kind"] == "asset_reword"
    assert job["status"] == "running"

    # 决议请求已返回，模型调用却还被闸着：改词确实是后台作业，不是同步跑完再回包。
    _wait_for(lambda: bool(harness.rewrite.requests))
    assert not harness.rewrite.gate.is_set()
    assert "尾巴绕住前爪" in harness.rewrite.requests[0]

    # 作业期间页面能看见进度：素材循环阶段的「素材改词」节点。
    progress = [event for event in _events(harness.root, harness.session_id)
                if event["type"] == "job.progress"
                and event["payload"]["node"] == "asset_reword"]
    assert progress, "改词作业一条进度事件都没有"
    assert {event["payload"]["stage"] for event in progress} == {"asset_loop"}
    # 作业期间台账仍是未交图，轮次不变（四态语义不变）。
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("pending", 1), ("pending", 0)]

    harness.rewrite.gate.set()
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and not entry["reason"]
                             for entry in snapshot["action_items"]))
    reopened = [entry for entry in body["action_items"]
                if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    assert reopened["asset_index"] == 1, "重开的必须是同一件（件号与语义名不变）"
    assert reopened["item_id"] != card["item_id"]

    text = harness.artifact_text("assets/picture-01.md")
    assert REWORDED_PROMPT in text, "作业成功后生图词没有更新"
    assert CAT_PROMPT in text, "锁死清单条目没有逐字保留"
    assert text != before

    # 完成后仍是未交图，轮次不变：改词不是一轮交图。
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("pending", 1), ("pending", 0)]

    # 轮次账：触发口沿用「AI 按偏差改词」，含偏差清单与改前改后全文。
    rounds = [json.loads(line) for line in (
        harness.root / Path(harness.session_id) / "asset-rounds.jsonl")
        .read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rounds) == 1
    record = rounds[0]
    assert record["trigger"] == "regen_ai_reword"
    assert record["name"] == CAT
    assert "尾巴绕住前爪" in record["feedback"]
    assert record["prompt_before"].strip() == CAT_PROMPT
    assert record["prompt_after"] == REWORDED_PROMPT


def test_reword_that_drops_a_locked_feature_is_not_written(harness):
    """加法守卫：删掉任一锁死条目即不落盘，卡片挂出「模型想删掉 X，要不要删」。"""
    _body, card, before = _to_failed_first_card(harness)
    harness.rewrite.reply = "一只橘色短毛猫蹲在木质窗台。"  # 丢了「尾巴绕住前爪」

    resp = _resolve(harness, card, {"decision": "reword_reroll"})
    assert resp.status_code == 202, resp.text
    job = resp.json()

    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and "模型想删掉" in entry["reason"]
                             for entry in snapshot["action_items"]))
    card2 = [entry for entry in body["action_items"]
             if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    assert "尾巴绕住前爪" in card2["reason"]
    # 待裁项在文案里，不是只把差异报告重放一遍。
    assert "要不要删" in card2["reason"].split("模型想删掉", 1)[1]
    # 原差异清单仍在：用户要能换另一条出路。
    assert "偏差" in card2["reason"]

    # 一个字节都没写；轮次账也没有这一轮（没改成功就不记）。
    assert harness.artifact_text("assets/picture-01.md") == before
    assert not (harness.root / Path(harness.session_id) / "asset-rounds.jsonl").exists()
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("pending", 1), ("pending", 0)]

    detail = harness.client.get(
        f"/api/v1/sessions/{harness.session_id}/jobs/{job['job_id']}").json()
    assert detail["status"] == "succeeded"
    assert "没有落盘" in detail["detail"]


def test_reroll_reopens_the_card_without_touching_any_text(harness):
    """原词再掷：生图词与描述一个字都不动，只重开同一件号的卡片。"""
    _body, card, before = _to_failed_first_card(harness)
    assert harness.rewrite.requests == [], "校对本身不许叫改写模型"

    resp = _resolve(harness, card, {"decision": "reroll"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "asset_loop"

    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and not entry["reason"]
                             for entry in snapshot["action_items"]))
    reopened = [entry for entry in body["action_items"]
                if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    assert reopened["asset_index"] == 1

    assert harness.artifact_text("assets/picture-01.md") == before
    assert harness.rewrite.requests == [], "原词再掷不许叫改写模型"
    assert not (harness.root / Path(harness.session_id) / "asset-rounds.jsonl").exists()
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("pending", 1), ("pending", 0)]



# ---------------------------------------------------------------------------
# 四选的另外两条出路（#289）：按图改文案 / 明知故犯保留原描述
# ---------------------------------------------------------------------------

CAT_FACTS = CAT_PROMPT.replace("尾巴绕住前爪", "尾巴垂在窗台边缘，没绕住前爪")
OMISSION_CLAIM = ("项圈", "", "脖子上有一圈红色项圈")


def _parse_prompt(text: str) -> str:
    """产物正文 → 词本体（与 ``workbench_store.parse_asset_prompt`` 同口径，用例独立取）。"""
    _head, separator, body = str(text).partition("\n\n")
    return body if separator else str(text)


def _facts_record(harness, name: str = CAT) -> dict:
    """会话文件里的素材画面事实记录（下游 ``proofread_descriptions`` 取的就是它）。"""
    rows = harness.on_disk().get("stage_state", {}).get("asset_proofreads") or {}
    return dict(rows.get(name) or {})


def _job(harness, job_id: str) -> dict:
    return harness.client.get(
        f"/api/v1/sessions/{harness.session_id}/jobs/{job_id}").json()


def _resolved_card(harness, number: int = 1) -> dict:
    cards = [entry for entry in harness.detail()["action_items"]
             if entry["kind"] == "asset_image" and entry["status"] == "resolved"
             and entry["asset_index"] == number]
    return cards[-1] if cards else {}


def test_settle_exits_are_declared_decision_values():
    """两条定稿出路都是决议值（常量与闭合表两处），事件类型仍是十种闭合集合。"""
    from minimax_h3_prompt import step_machine
    from minimax_h3_prompt.workbench_store import EVENT_TYPES, _DECISIONS

    assert step_machine.DECISION_ACCEPT_IMAGE_FACTS == "accept_image_facts"
    assert step_machine.DECISION_KEEP_DESCRIPTION == "keep_description"
    assert {step_machine.DECISION_ACCEPT_IMAGE_FACTS,
            step_machine.DECISION_KEEP_DESCRIPTION} <= _DECISIONS
    assert len(EVENT_TYPES) == 10


def test_accept_image_facts_rewrites_only_the_description(harness):
    """按图改文案：描述里那处换成图中实见、本件定稿、生图词逐字不动。"""
    _body, card, before = _to_failed_first_card(harness)

    resp = _resolve(harness, card, {"decision": "accept_image_facts"})
    assert resp.status_code == 202, resp.text
    job = resp.json()

    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))

    # 文本落点：描述被定点替换，生图词一个字节都没动。
    record = _facts_record(harness)
    assert record["status"] == "done"
    assert record["description"] == CAT_FACTS
    assert record["deviations"][0]["prompt_quote"] == "尾巴绕住前爪"
    assert harness.artifact_text("assets/picture-01.md") == before
    # 下游取材口读到的就是它（不是把词换了源）。
    assert record["description"] != _parse_prompt(before)

    # 台账：本件定稿、轮次 +1（不通过那一轮已经记过一次，所以是 2）；下一件的卡已经出现。
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("passed", 2), ("pending", 0)]

    # 「已按图补入 N 条」在卡片消失后仍读得到：已决议卡片的 reason 与这次任务的详情。
    resolved = _resolved_card(harness)
    assert resolved["decision"] == "accept_image_facts"
    assert "已按图补入 0 条" in resolved["reason"]
    assert "生图词逐字未改" in resolved["reason"]
    assert "已按图补入 0 条" in _job(harness, job["job_id"])["detail"]
    # 结论随事件走：重放能把这段话补回已决议的卡片（事件类型不新增）。
    resolved_event = [event for event in _events(harness.root, harness.session_id)
                      if event["type"] == "action_item.resolved"
                      and event["payload"].get("item_id") == card["item_id"]][0]
    assert "已按图补入 0 条" in resolved_event["payload"]["reason"]

    # 校对与定稿都不叫改写模型，也不写轮次账（词没被改过）。
    assert harness.rewrite.requests == []
    assert not (harness.root / Path(harness.session_id) / "asset-rounds.jsonl").exists()


def test_keep_description_keeps_the_wording_and_only_adds_the_facts(harness):
    """明知故犯保留原描述：偏差一处不动、只并入补漏，本件定稿。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(_claims(("尾巴", "尾巴绕住前爪", "尾巴垂在窗台边缘，没绕住前爪"),
                                    OMISSION_CLAIM))
    assert harness.submit_image(item, image["asset_id"]).status_code == 202
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    card = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    assert card["proofread"]["omissions"] == [
        {"aspect": "项圈", "fact": "脖子上有一圈红色项圈"}]

    resp = _resolve(harness, card, {"decision": "keep_description"})
    assert resp.status_code == 202, resp.text

    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    record = _facts_record(harness)
    # 原描述逐字保留（偏差没被改），补漏以「；」并进尾部。
    assert record["description"] == f"{CAT_PROMPT}；脖子上有一圈红色项圈"
    assert CAT_PROMPT in record["description"]
    assert "尾巴垂在窗台边缘" not in record["description"]
    assert record["omissions"] == ["脖子上有一圈红色项圈"]
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("passed", 2), ("pending", 0)]

    resolved = _resolved_card(harness)
    assert resolved["decision"] == "keep_description"
    assert "已按图补入 1 条" in resolved["reason"]


def test_a_fact_observed_again_is_not_merged_twice(harness):
    """补漏按观察去重：同一句第二次定稿不重复并进描述（同一句不出现两次）。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    first = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(_claims(OMISSION_CLAIM))
    assert harness.submit_image(item, first["asset_id"]).status_code == 202
    # 只有补漏、没有偏差 → 这一件已经定稿（补漏不阻挡），描述里已并入那条事实。
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    assert _facts_record(harness)["description"] == f"{CAT_PROMPT}；脖子上有一圈红色项圈"
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("passed", 1), ("pending", 0)]

    # 换一张图重走一遍同一件：同一句补漏再出现一次，描述里仍只出现一次。
    asset_id = next(entry["asset_id"] for entry in harness.ledger()
                    if entry["number"] == 1)
    reopen = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/assets/{asset_id}/reopen")
    assert reopen.status_code == 200, reopen.text
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             for entry in snapshot["action_items"]))
    card = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    second = harness.upload(_png(10), "gen-cat-2.png")
    harness.readings.append(_claims(OMISSION_CLAIM))
    assert harness.submit_image(card, second["asset_id"]).status_code == 202
    # 这一轮只有补漏（无偏差）→ 直接定稿，描述里那条事实仍然只有一份。
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 2
                             for entry in snapshot["action_items"]))
    assert _facts_record(harness)["description"].count("脖子上有一圈红色项圈") == 1


def test_rerolling_does_not_merge_the_facts(harness):
    """重掷中途不并入补漏：原词再掷之后描述仍是空的（图还没定下来）。"""
    body = _to_first_card(harness)
    item = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]
    image = harness.upload(_png(9), "gen-cat.png")
    harness.readings.append(_claims(("尾巴", "尾巴绕住前爪", "尾巴垂在窗台边缘，没绕住前爪"),
                                    OMISSION_CLAIM))
    assert harness.submit_image(item, image["asset_id"]).status_code == 202
    body = harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and entry["reason"]
                             for entry in snapshot["action_items"]))
    card = [entry for entry in body["action_items"]
            if entry["kind"] == "asset_image" and entry["status"] == "pending"][0]

    resp = _resolve(harness, card, {"decision": "reroll"})
    assert resp.status_code == 202, resp.text
    harness.wait_until(
        lambda snapshot: any(entry["kind"] == "asset_image"
                             and entry["status"] == "pending"
                             and entry["asset_index"] == 1
                             and not entry["reason"]
                             for entry in snapshot["action_items"]))

    assert _facts_record(harness) == {}, "重掷中途把补漏写进了描述"
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("pending", 1), ("pending", 0)]


def test_settle_exits_reject_a_submitted_image(harness):
    """定稿出路与交图互斥：一次决议只能是其中一件事（422）。"""
    _body, card, _before = _to_failed_first_card(harness)
    image = harness.upload(_png(11), "gen-cat-3.png")

    _assert_problem(
        _resolve(harness, card, {"decision": "accept_image_facts",
                                 "asset_id": image["asset_id"]}),
        422, "asset_image_invalid")


# 恢复阶段 1 原文（#291 / spec #286「恢复动作」）
# ---------------------------------------------------------------------------

# 旧逻辑按图改写后的样子：锁死条目「尾巴绕住前爪」被换成了图里的实况。
BROKEN_PROMPT = "一只橘色短毛猫蹲在木质窗台，尾巴垂在窗台边缘。"


def _simulate_old_rewrite(harness, prompt: str = BROKEN_PROMPT,
                          number: int = 1, name: str = CAT) -> None:
    """模拟旧逻辑那次覆盖改写：盘上的词被改了，却没落轮次账（#286 事故 5）。"""
    from minimax_h3_prompt.api.jobs import _render_asset_prompt

    path = (harness.root / Path(harness.session_id)
            / "assets" / f"picture-{number:02d}.md")
    path.write_text(_render_asset_prompt(number, name, prompt), encoding="utf-8")


def _pending_card(harness) -> dict:
    cards = [item for item in harness.detail()["action_items"]
             if item["kind"] == "asset_image" and item["status"] == "pending"]
    assert len(cards) == 1, f"未决素材卡不是恰好一张：{cards}"
    return cards[0]


def _disk_prompt(harness, number: int = 1) -> str:
    from minimax_h3_prompt.workbench_store import parse_asset_prompt

    return parse_asset_prompt(
        harness.artifact_text(f"assets/picture-{number:02d}.md")).strip()


def test_restore_prompt_is_a_declared_decision_value():
    """恢复动作是决议值（常量与闭合表两处），仍走既有的 resolve 端点。"""
    from minimax_h3_prompt import step_machine
    from minimax_h3_prompt.workbench_store import _DECISIONS

    assert step_machine.DECISION_RESTORE_PROMPT == "restore_prompt"
    assert step_machine.DECISION_RESTORE_PROMPT in _DECISIONS


def test_restore_action_appears_only_for_an_untracked_diff(harness):
    """三条件同时成立才出现：未定稿 ＋ 盘上词与原文不一致 ＋ 轮次账里没有记录。"""
    _to_first_card(harness)
    # 第一轮：盘上的词就是阶段 1 原文——没有差异，不该出现恢复动作。
    assert not _pending_card(harness)["restore_available"]

    _simulate_old_rewrite(harness)
    card = _pending_card(harness)
    assert card["restore_available"] is True
    # diff 是「改前（盘上）→ 改后（阶段 1 原文）」，两边都要看得见。
    assert BROKEN_PROMPT in card["restore_diff"]
    assert CAT_PROMPT in card["restore_diff"]
    assert "-" in card["restore_diff"] and "+" in card["restore_diff"]
    # 它不是校对四选之一：卡片的 reason / 结构化结论一个字都不因它变化。
    assert card["reason"] == ""
    assert card["proofread"] is None


def test_restore_writes_back_the_stage1_original_without_touching_the_ledger(harness):
    """点击后：盘上词与阶段 1 原文逐字一致；台账状态、轮次与轮次账都不动。"""
    _body = _to_first_card(harness)
    _simulate_old_rewrite(harness)
    card = _pending_card(harness)
    assert card["restore_available"] is True

    resp = _resolve(harness, card, {"decision": "restore_prompt"})
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["kind"] == "asset_restore"
    assert job["status"] == "running"

    harness.wait_until(
        lambda snapshot: _disk_prompt(harness) == CAT_PROMPT
        and any(entry["kind"] == "asset_image"
                and entry["status"] == "pending"
                and entry["asset_index"] == 1
                for entry in snapshot["action_items"]))

    # 盘上的词逐字回到阶段 1 原文；产物头部仍在（同一份产物覆盖写）。
    assert _disk_prompt(harness) == CAT_PROMPT
    assert "第 1 件" in harness.artifact_text("assets/picture-01.md")

    # 台账状态与轮次都不动；恢复不是一轮交图，也不落轮次账。
    assert [(entry["status"], entry["round"]) for entry in harness.ledger()] == [
        ("pending", 0), ("pending", 0)]
    assert not (harness.root / Path(harness.session_id)
                / "asset-rounds.jsonl").exists()
    # 不调视觉模型、不调改写模型。
    assert harness.read_calls == []
    assert harness.rewrite.requests == []

    # job 的 detail 就是改前改后 diff（作业做了什么，事后查得到）。
    detail = harness.client.get(
        f"/api/v1/sessions/{harness.session_id}/jobs/{job['job_id']}").json()
    assert detail["status"] == "succeeded"
    assert BROKEN_PROMPT in detail["detail"] and CAT_PROMPT in detail["detail"]

    # 恢复之后盘上词与原文一致：恢复动作自然不再出现；旧差异报告属于被改坏的那版词，
    # 也跟着收走（新卡不带 reason）。
    reopened = _pending_card(harness)
    assert not reopened["restore_available"]
    assert reopened["reason"] == ""
    assert reopened["asset_index"] == 1


def test_restore_action_hides_when_the_round_log_has_a_record(harness):
    """有轮次账记录＝合法改写，恢复动作不出现；硬发决议也不写盘。"""
    from minimax_h3_prompt.asset_loop import TRIGGER_REGEN_AI_REWORD, log_asset_round

    _to_first_card(harness)
    _simulate_old_rewrite(harness)
    directory = harness.root / Path(harness.session_id)
    log_asset_round(
        directory, name=CAT, kind="picture", trigger=TRIGGER_REGEN_AI_REWORD,
        feedback="按偏差改词", prompt_before=CAT_PROMPT,
        prompt_after=BROKEN_PROMPT)

    card = _pending_card(harness)
    assert not card["restore_available"], "有账的差异不该给一键抹掉的入口"

    # 绕过界面直接发决议：作业照常起，但一个字节都不写（判据同一份）。
    resp = _resolve(harness, card, {"decision": "restore_prompt"})
    assert resp.status_code == 202, resp.text
    job = resp.json()
    harness.wait_idle()
    assert _disk_prompt(harness) == BROKEN_PROMPT
    detail = harness.client.get(
        f"/api/v1/sessions/{harness.session_id}/jobs/{job['job_id']}").json()
    assert detail["status"] == "succeeded"
    assert "没有可恢复的差异" in detail["detail"]

    # 一张轮次账记录还是那一张：恢复没有往账本里写第二行。
    rounds = [json.loads(line) for line in (directory / "asset-rounds.jsonl")
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rounds) == 1


def test_restore_action_never_appears_on_settled_items(harness):
    """已定稿（passed）或已放弃（abandoned）的件不给恢复动作，哪怕盘上有差异。"""
    from minimax_h3_prompt.workbench_store import (
        ASSET_LEDGER_STATUS_ABANDONED,
        ASSET_LEDGER_STATUS_PASSED,
    )

    _to_first_card(harness)
    _simulate_old_rewrite(harness)
    store = harness.client.app.state.session_store

    for status in (ASSET_LEDGER_STATUS_PASSED, ASSET_LEDGER_STATUS_ABANDONED):
        store.mutate(
            harness.session_id,
            lambda detail, _status=status: {"asset_ledger": [
                ({**entry, "status": _status}
                 if str(entry.get("kind")) == "picture"
                 and int(entry.get("number", 0)) == 1 else entry)
                for entry in detail.asset_ledger_snapshot()]})
        pending = [item for item in harness.detail()["action_items"]
                   if item["kind"] == "asset_image" and item["status"] == "pending"]
        assert pending, "这一步只是把台账标成已结算，卡片仍在"
        assert not pending[0]["restore_available"]


def test_restore_prompt_rejects_a_submitted_image(harness):
    """恢复原文与交图互斥：同时提交一律 422，且没有把待办决议掉。"""
    _to_first_card(harness)
    _simulate_old_rewrite(harness)
    card = _pending_card(harness)
    image = harness.upload(_png(9), "gen-cat.png")

    body = _assert_problem(
        _resolve(harness, card, {"decision": "restore_prompt",
                                 "asset_id": image["asset_id"]}),
        422, "asset_image_invalid")
    # 载荷互斥在路由层就拦下（asset_id 只能单独提交或配它自己那几档 decision）：
    # 恢复原文不认图，带上 asset_id 一定是发错了。
    assert "asset_id" in body["detail"]
    assert [entry["item_id"] for entry in harness.pending("asset_image")] == [
        card["item_id"]]
