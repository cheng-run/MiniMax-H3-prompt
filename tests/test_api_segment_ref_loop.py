"""D7-6（#258）：ref 段循环按路线分叉。

钉住六件（spec #252 §5.4 / §10 / §11 / §13）：

1. 段规划从 brief 读路线：ref 走 ``prepare_ref_segmentation``（mode="ref"），base 一字不变。
2. 分段门槛是路线属性：ref ≤ ``MAX_SEG_S`` 是「1 段」直出（整片提示词即段词），
   不是长片。
3. 段词落 ``segments/segment-NN.md``（kind ``segment_prompt``），与 base 的
   ``shot-NN.md`` 并列；多参专属产物（顺序卡、分镜表）登记进产物表，plan.json 不进。
4. 写段 job **不重新规划**（规划是慢速模型调用）：RefSegmentJob 从盘上重建。
5. ref 段校验不建桥接帧、不剥尾帧、不读图：段落定只走 resolve 的技术档 + 段文本仍在。
6. 已通过段能「换一段」（ref 专属端点），base 会话被拒；base 专属载荷（frames）打 ref
   会话 → ``route_mismatch``。

模型、读图、规划、写段全部打桩——本文件绝不真发网络请求。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, ref_segments
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.asset_proofread import ProofreadReport
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.session_store import SESSION_FILENAME
from minimax_h3_prompt.tools import frame_auditor, reference_auditor
from minimax_h3_prompt.workbench_store import artifact_spec
from conftest import write_h3_tier_mp4
from workbench_client import workbench_client
from workbench_deterministic import DeterministicChatModel

RUNTIME = SimpleNamespace(
    roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=0)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CAT = "橘猫"
LAMP = "台灯"

# ref 整片六段式：两张参考图分别只在各自那一段出场，段内子集因此可分辨。
WHOLE_PROMPT = """subject_definitions:
<Subject 1> 橘猫:
a photo of <Picture 1>, an orange cat sitting on the windowsill.
<Subject 2> 台灯:
a photo of <Picture 2>, a warm desk lamp.

summary:
reference generation: two shots of the cat and the lamp.

retention_analysis:
橘猫 fully_preserved; 台灯 fully_preserved.

detailed_description:
[Shot 1] At 00:00.000, a medium shot of 橘猫 by the window.
[Shot 2] At 00:09.000, a closer shot of 台灯 flickering.

overall_soundscape:
rain against the glass

non_diegetic_music:
low strings
"""


def _png(seed: int = 0) -> bytes:
    return PNG_MAGIC + bytes([seed % 256]) * 2048


def _stage1_state(names: tuple[str, ...]) -> dict:
    return {
        "script": "分场剧本：橘猫蹲在窗台，台灯亮着。",
        "shot_table": (
            "[Shot 1] At 00:00.000, a medium shot of the cat by the window.\n"
            "[Shot 2] At 00:09.000, a closer shot of the lamp flickering."),
        "character_image_prompts": [
            {"name": name, "positive_prompt": f"{name} 的画面",
             "negative_prompt": "", "locked_features": []}
            for name in names
        ],
    }


def _two_plans(total: int = 18) -> list[SegmentPlan]:
    return [
        SegmentPlan(index=0, start_s=0, end_s=9, shots_in_segment=(1,),
                    summary="开场", end_hook="猫停在窗台",
                    start_state="猫在窗台边"),
        SegmentPlan(index=1, start_s=9, end_s=total, shots_in_segment=(2,),
                    summary="收束", end_hook="台灯亮起",
                    start_state="猫离开窗台"),
    ]


class Harness:
    def __init__(self, client, root: Path, tmp_path: Path) -> None:
        self.client = client
        self.root = root
        self.tmp_path = tmp_path
        self.session_id = ""
        self.plan_calls: list[dict] = []
        self.write_calls: list[int] = []

    # -- HTTP 小抄 ---------------------------------------------------
    def detail(self) -> dict:
        return self.client.get(f"/api/v1/sessions/{self.session_id}").json()

    def wait_until(self, predicate, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.detail()
            if predicate(body):
                return body
            time.sleep(0.02)
        raise AssertionError(f"详情没有在 {timeout}s 内满足条件：{self.session_id}")

    def wait_job(self, job_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.client.get(
                f"/api/v1/sessions/{self.session_id}/jobs/{job_id}").json()
            if body["status"] != "running":
                return body
            time.sleep(0.02)
        raise AssertionError(f"任务没有在 {timeout}s 内结束：{job_id}")

    def wait_idle(self) -> dict:
        return self.wait_until(
            lambda body: body["status"] != "running"
            and body.get("current_job") is None)

    def wait_pending(self, kind: str) -> dict:
        return self.wait_until(
            lambda body: bool([item for item in body["action_items"]
                               if item["kind"] == kind
                               and item["status"] == "pending"]))

    def pending(self, kind: str) -> list[dict]:
        return [item for item in self.detail()["action_items"]
                if item["kind"] == kind and item["status"] == "pending"]

    def on_disk(self) -> dict:
        path = self.root / Path(self.session_id) / SESSION_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))

    def events(self) -> list[dict]:
        path = self.root / Path(self.session_id) / "events.jsonl"
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def artifact_text(self, name: str) -> str:
        return (self.root / Path(self.session_id) / name).read_text(encoding="utf-8")

    def artifact_names(self) -> list[str]:
        return [item["name"] for item in self.detail()["artifacts"]]

    # -- 流程 --------------------------------------------------------
    def create(self, duration: float, mode: str = "ref") -> str:
        resp = self.client.post("/api/v1/sessions", json={
            "mode": mode, "variant": "T2VA", "duration": duration,
            "style": "写实", "language": "Chinese", "plot": "雨夜旧信"})
        assert resp.status_code == 201, resp.text
        self.session_id = resp.json()["session_id"]
        return self.session_id

    def upload_picture(self, name: str, seed: int) -> str:
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/assets",
            files={"file": (f"{seed}.png", _png(seed), "image/png")},
            data={"name": name})
        assert resp.status_code == 201, resp.text
        return resp.json()["asset_id"]

    def upload_plain_png(self, seed: int) -> str:
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/assets",
            files={"file": (f"proof-{seed}.png", _png(seed), "image/png")})
        assert resp.status_code == 201, resp.text
        return resp.json()["asset_id"]

    def start(self) -> dict:
        resp = self.client.post(f"/api/v1/sessions/{self.session_id}/jobs")
        assert resp.status_code == 202, resp.text
        return self.wait_job(resp.json()["job_id"])

    def approve_review(self) -> dict:
        review = self.pending("stage1_review")[0]
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/action-items/"
            f"{review['item_id']}/resolve",
            json={"decision": "approve"})
        assert resp.status_code == 202, resp.text
        return self.wait_job(resp.json()["job_id"])

    def submit_asset_image(self, seed: int = 7) -> dict:
        item = self.pending("asset_image")[0]
        asset_id = self.upload_plain_png(seed)
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/action-items/"
            f"{item['item_id']}/resolve",
            json={"asset_id": asset_id})
        assert resp.status_code == 202, resp.text
        return self.wait_job(resp.json()["job_id"])

    def to_segment_todo(self, duration: float, pictures: tuple[str, ...]) -> dict:
        """跑完阶段 1 → 素材循环 → 阶段 2 → 分段规划 → 写第 1 段。"""
        self.create(duration)
        for index, name in enumerate(pictures):
            self.upload_picture(name, index + 1)
        self.start()
        self.approve_review()
        for index in range(len(pictures)):
            self.wait_pending("asset_image")
            self.submit_asset_image(seed=index + 7)
        return self.wait_until(
            lambda body: body["status"] != "running"
            and bool([item for item in body["action_items"]
                      if item["kind"] == "segment_video"
                      and item["status"] == "pending"]))

    def submit_segment_video(self, number: int, seconds: float,
                             seed: int = 0) -> dict:
        todo = [item for item in self.pending("segment_video")
                if item["segment_index"] == number][0]
        video = write_h3_tier_mp4(
            self.tmp_path / f"seg-{number}-{seed}.mp4", seconds)
        resp = self.client.post(
            f"/api/v1/sessions/{self.session_id}/assets",
            files={"file": (video.name, video.read_bytes(), "video/mp4")})
        assert resp.status_code == 201, resp.text
        asset_id = resp.json()["asset_id"]
        resolved = self.client.post(
            f"/api/v1/sessions/{self.session_id}/action-items/"
            f"{todo['item_id']}/resolve",
            json={"video_asset_id": asset_id})
        assert resolved.status_code == 202, resolved.text
        return self.wait_job(resolved.json()["job_id"])


@pytest.fixture
def ref_app(tmp_path, monkeypatch):
    """ref 会跑到的每一处慢速调用都打桩；其余（状态机、落盘、产物）走真实代码。"""
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    monkeypatch.setattr(
        model_factory, "build_chat_model", lambda: DeterministicChatModel())
    root = tmp_path / "sessions"
    harness = Harness(None, root, tmp_path)

    def fake_stage1(brief, config, **kwargs):
        return _stage1_state((CAT, LAMP)), None, None

    def fake_stage2(state, brief, config, **kwargs):
        updated = dict(state)
        updated["final_prompt"] = WHOLE_PROMPT
        return updated, WHOLE_PROMPT

    def fake_plan_segments(shot_table, total_s, llm, *, mode="base", **kwargs):
        harness.plan_calls.append({"mode": mode, "kwargs": kwargs})
        return _two_plans(int(round(total_s)))

    def fake_write_ref_segment(plan, all_plans, **kwargs):
        harness.write_calls.append(plan.index + 1)
        return (
            "subject_definitions:\n<Picture 1> 段内参考素材\n\n"
            "summary:\nsegment\n\n"
            "retention_analysis:\nN/A\n\n"
            "detailed_description:\n"
            f"[Shot 1] At 00:00.000, segment {plan.index + 1} stub.\n\n"
            "overall_soundscape:\nrain\n\nnon_diegetic_music:\nN/A\n")

    monkeypatch.setattr(pipeline, "run_stage1", fake_stage1)
    monkeypatch.setattr(pipeline, "run_stage2", fake_stage2)
    # ref_segments 在模块导入时绑定了 plan_segments，必须打在它那份引用上
    # （打 ``segment_planner.plan_segments`` 不会影响已绑定的名字）。
    monkeypatch.setattr(ref_segments, "plan_segments", fake_plan_segments)
    monkeypatch.setattr(ref_segments, "write_ref_segment_v2", fake_write_ref_segment)
    # 素材校对不在本票范围：一律判干净（#256 的判据由它自己的用例咬）。
    monkeypatch.setattr(
        reference_auditor, "describe_image",
        lambda path, request: '{"claims": []}')
    monkeypatch.setattr(
        "minimax_h3_prompt.asset_proofread.compare_descriptions",
        lambda *a, **k: ProofreadReport())
    app = create_app(sessions_root=root, runtime_config=RUNTIME)
    with workbench_client(app) as client:
        harness.client = client
        yield harness, app


# ---------------------------------------------------------------------------
# 规划：路线从 brief 读
# ---------------------------------------------------------------------------
def test_ref_planning_uses_ref_route_and_writes_segment_product(ref_app):
    harness, _app = ref_app
    detail = harness.to_segment_todo(18.0, (CAT, LAMP))

    assert [segment["index"] for segment in detail["segments"]] == [1, 2]
    assert [call["mode"] for call in harness.plan_calls] == ["ref"]

    names = harness.artifact_names()
    assert "segments/segment-01.md" in names
    assert "segments/order-card.md" in names
    assert "segments/ref-shot-table.md" in names
    assert "segments/plan.json" not in names
    assert artifact_spec("segments/segment-01.md").kind == "segment_prompt"
    assert artifact_spec("segments/order-card.md").kind == "concat_order_card"
    assert artifact_spec("segments/ref-shot-table.md").kind == "ref_shot_table"

    # 段词落 segment-NN.md（与 base 的 shot-NN.md 并列），不是改名。
    assert harness.artifact_text("segments/segment-01.md").strip()
    assert not (harness.root / Path(harness.session_id)
                / "segments" / "shot-01.md").exists()


def test_ref_short_film_is_a_single_segment_direct_output(ref_app):
    """12 秒 ref 走「1 段」：规划不拆段，段词就是整片提示词原样。"""
    harness, _app = ref_app
    detail = harness.to_segment_todo(12.0, (CAT, LAMP))

    assert len(detail["segments"]) == 1
    segment = detail["segments"][0]
    assert (segment["start_s"], segment["end_s"]) == (0, 12)
    assert harness.artifact_text("segments/segment-01.md") == WHOLE_PROMPT
    assert harness.plan_calls == []


def test_ref_segment_rows_expose_readonly_asset_subset(ref_app):
    harness, _app = ref_app
    detail = harness.to_segment_todo(18.0, (CAT, LAMP))

    first, second = detail["segments"]
    # 段内重编号从 1 连续；原号保留供溯源。两张图分属两段，子集可分辨。
    assert [(item["number"], item["name"]) for item in first["assets"]] == [(1, CAT)]
    assert [(item["number"], item["name"]) for item in second["assets"]] == [(1, LAMP)]
    assert first["assets"][0]["original_number"] == 1
    assert second["assets"][0]["original_number"] == 2


def test_ref_write_job_never_replans(ref_app):
    """两段都写完之后，规划调用仍只有一次：写段从盘上的 plan.json 重建。"""
    harness, _app = ref_app
    harness.to_segment_todo(18.0, (CAT, LAMP))
    harness.submit_segment_video(1, 9.0)
    harness.wait_until(
        lambda body: body["status"] != "running"
        and bool([item for item in body["action_items"]
                  if item["kind"] == "segment_video"
                  and item["status"] == "pending"
                  and item["segment_index"] == 2]))

    assert len(harness.plan_calls) == 1
    assert harness.write_calls == [1, 2]


def test_ref_segment_write_emits_segment_injection_card(ref_app):
    harness, _app = ref_app
    detail = harness.to_segment_todo(18.0, (CAT, LAMP))

    card = next((item for item in detail["artifacts"]
                 if item["name"] == "segments/injection-card-01.md"), None)
    assert card is not None
    assert card["kind"] == "segment_injection_card"


def test_ref_planning_hard_fails_when_segment_subset_overflows(
        ref_app, tmp_path, monkeypatch):
    """段内子集超上限在**规划期**硬失败，并点名超限素材（不跑到一半才炸）。"""
    harness, _app = ref_app
    # 一段里同时用两张图，而每段上限被压到 1：闭包必然超限——判据与官方 9 图同一条。
    monkeypatch.setattr(
        ref_segments, "plan_segments",
        lambda *a, **k: [
            SegmentPlan(index=0, start_s=0, end_s=18, shots_in_segment=(1, 2),
                        summary="整片", end_hook="台灯亮起",
                        start_state="猫在窗台边")])
    monkeypatch.setattr(
        ref_segments, "workflow_settings",
        lambda: SimpleNamespace(refs_per_segment=1))

    harness.create(18.0)
    harness.upload_picture(CAT, 1)
    harness.upload_picture(LAMP, 2)
    harness.start()
    harness.approve_review()
    for index in range(2):
        harness.wait_pending("asset_image")
        harness.submit_asset_image(seed=index + 7)
    detail = harness.wait_until(lambda body: body["status"] == "failed")

    assert detail["segments"] == []
    failure = [event for event in harness.events()
               if event["type"] == "job.failed"][-1]
    message = str(failure["payload"].get("detail") or "")
    assert "超过" in message
    assert CAT in message and LAMP in message


# ---------------------------------------------------------------------------
# 校验：ref 不剥尾帧、不读图
# ---------------------------------------------------------------------------
def test_ref_segment_validation_skips_bridge_frame_and_vision(
        ref_app, monkeypatch):
    harness, _app = ref_app
    harness.to_segment_todo(18.0, (CAT, LAMP))

    def _boom(*args, **kwargs):
        raise AssertionError("ref 段校验不许建桥接帧或读图（ADR 0011）")

    monkeypatch.setattr(frame_auditor, "extract_last_frame", _boom)
    monkeypatch.setattr(frame_auditor, "describe_bridge_frame", _boom)

    harness.submit_segment_video(1, 9.0)
    detail = harness.wait_until(
        lambda body: body["status"] != "running"
        and bool([item for item in body["action_items"]
                  if item["kind"] == "segment_video"
                  and item["status"] == "pending"
                  and item["segment_index"] == 2]))
    assert detail["segments"][0]["status"] == "passed"
    # 没有桥接帧锚：段尾语义靠写段时的段内注记，不靠读图回填。
    assert "bridge_frame_descriptions" not in harness.on_disk()["stage_state"]


# ---------------------------------------------------------------------------
# 换一段（ref 专属端点）
# ---------------------------------------------------------------------------
def test_ref_passed_segment_can_be_reopened(ref_app):
    harness, _app = ref_app
    harness.to_segment_todo(18.0, (CAT, LAMP))
    harness.submit_segment_video(1, 9.0)
    detail = harness.wait_until(
        lambda body: body["status"] != "running"
        and bool([segment for segment in body["segments"]
                  if segment["index"] == 1
                  and segment["status"] == "passed"]))
    before = detail["segments"][0]["assets"]

    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/segments/1/reopen")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "segment_video"
    assert resp.json()["segment_index"] == 1

    after = harness.detail()
    assert after["status"] == "awaiting_human"
    segment = after["segments"][0]
    assert segment["status"] == "waiting_video"
    assert segment["video_asset_id"] == ""
    assert segment["prompt_artifact_id"]
    # 换段只换视频：段内参考素材子集一个字都不变。
    assert segment["assets"] == before
    todos = [item for item in after["action_items"]
             if item["kind"] == "segment_video" and item["status"] == "pending"]
    assert [item["segment_index"] for item in todos] == [1]

    # 重交后重过闸门：段回到 passed。
    harness.submit_segment_video(1, 9.0, seed=1)
    final = harness.wait_until(
        lambda state: state["segments"][0]["status"] == "passed")
    assert final["segments"][0]["status"] == "passed"


def test_reopen_is_rejected_on_base_sessions(ref_app):
    harness, _app = ref_app
    harness.create(4.0, mode="base")
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/segments/1/reopen")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "segment_resubmit_unsupported"


def test_reopen_unknown_segment_is_not_found(ref_app):
    harness, _app = ref_app
    harness.to_segment_todo(18.0, (CAT, LAMP))
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/segments/99/reopen")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "segment_not_found"


def test_reopen_pending_segment_is_conflict(ref_app):
    harness, _app = ref_app
    harness.to_segment_todo(18.0, (CAT, LAMP))
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/segments/1/reopen")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "segment_not_passed"


# ---------------------------------------------------------------------------
# 路线错配
# ---------------------------------------------------------------------------
def test_frames_payload_on_ref_session_is_route_mismatch(ref_app):
    harness, _app = ref_app
    detail = harness.to_segment_todo(18.0, (CAT, LAMP))
    todo = [item for item in detail["action_items"]
            if item["kind"] == "segment_video" and item["status"] == "pending"][0]
    resp = harness.client.post(
        f"/api/v1/sessions/{harness.session_id}/action-items/"
        f"{todo['item_id']}/resolve",
        json={"frames": {"first": "", "last": ""}})
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "route_mismatch"


# ---------------------------------------------------------------------------
# 重建：不调规划模型
# ---------------------------------------------------------------------------
def test_rebuild_ref_job_reads_plan_from_disk_without_replanning(
        tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        ref_segments, "plan_segments",
        lambda *a, **k: calls.append(1) or [])

    prepared = ref_segments.prepare_ref_segmentation(
        whole_prompt=WHOLE_PROMPT, total_s=18, out_dir=tmp_path, llm=None)
    (tmp_path / "video-prompt.md").write_text(WHOLE_PROMPT, encoding="utf-8")
    rebuilt = ref_segments.load_ref_segments_for_resume(
        tmp_path, total_s=18, llm=None)

    assert [plan.to_dict() for plan in rebuilt.plans] == [
        plan.to_dict() for plan in prepared.plans]
    assert calls == []
