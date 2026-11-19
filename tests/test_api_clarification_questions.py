"""E1 后端（#338 / spec #332）：澄清问题端点与创建会话的问答对回填。

两条缝都在 HTTP 面上：新端点的请求/响应，以及创建会话时问答对落进
``Brief.clarifications`` 的形状。断言落在外站请求全文与响应体上，不读内部结构。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from workbench_client import workbench_client

def _runtime() -> SimpleNamespace:
    """每次一份运行期配置：用例会改 ``max_clarification_rounds``，共享对象会串味。

    澄清轮数必须由用例显式给：端点直接读它，缺键要当场红，不在这里补一份默认值
    （那会是第二处口径）。
    """
    return SimpleNamespace(
        max_clarification_rounds=2,
        roundtable_max_rounds=1,
        common_sense_qa=False,
        max_qa_iterations=0,
    )


class _Reply:
    """假模型回复：只需要 ``content``（与 ``agents.run_direct_call`` 的取法一致）。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _RecordingModel:
    """记录出站请求的桩模型；``boom`` 时模拟网关失败。"""

    def __init__(self, content: str = "", *, boom: bool = False) -> None:
        self.content = content
        self.boom = boom
        self.requests: list[str] = []

    def invoke(self, request: str, config: dict | None = None) -> _Reply:
        self.requests.append(str(request))
        if self.boom:
            raise RuntimeError("gateway boom")
        return _Reply(self.content)


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    """临时会话根 + 桩模型；``monkeypatch`` 打的是 ``api.jobs`` 的模型构造口。

    路由在调用时取 ``api.jobs.build_chat_model``（不在 import 期绑定），所以只打
    这一处就够；与 ``tests/test_api_director_planning.py`` 同一条缝。
    """
    from minimax_h3_prompt.api import jobs as jobs_module
    from minimax_h3_prompt.api.app import create_app

    model = _RecordingModel(
        "\n".join(f"问题{i}是什么？" for i in range(1, 4)))
    builder_calls: list[int] = []

    def _build() -> _RecordingModel:
        builder_calls.append(1)
        return model

    monkeypatch.setattr(jobs_module, "build_chat_model", _build)
    root = tmp_path / "sessions"
    application = create_app(sessions_root=root, runtime_config=_runtime())
    application.state.test_model = model
    application.state.test_builder_calls = builder_calls
    return application


def _client(application) -> TestClient:
    return workbench_client(application)


def _bot_body(**overrides) -> dict:
    body = {
        "mode": "base",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
    }
    body.update(overrides)
    return body


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    return body


# ---------------------------------------------------------------------------
# 只读端点：GET /api/v1/clarification-questions
# ---------------------------------------------------------------------------

def test_endpoint_returns_the_stub_questions_and_sends_the_full_request(app):
    """返回的问题＝桩模型给的那几句，且出站请求全文带着主题与条数上限。

    这条同时是**接线守卫**：摘掉路由里那一行生成调用后返回空列表，本条变红
    （变异 ``clarification_route_drops_generation``）。出站断言还带着 E2 的证据
    （spec #332「E2 的证据在这条缝上」）：模型必须被告知去问朝向与空间站位。
    """
    client = _client(app)

    resp = client.get("/api/v1/clarification-questions",
                      params={"topic": "秋日庭院里的橘猫", "limit": 2})

    assert resp.status_code == 200
    assert resp.json() == ["问题1是什么？", "问题2是什么？"]
    request = app.state.test_model.requests[0]
    assert "秋日庭院里的橘猫" in request, "出站请求里没有主题——模型无从按主题提问"
    assert "2" in request, "上限没写进请求——模型无从知道该给几个问题"
    missing = [anchor for anchor in ("朝向", "站位") if anchor not in request]
    assert not missing, f"出站请求里没有朝向/站位的问法锚词：{missing}"


def test_default_limit_comes_from_the_clarification_rounds_config(app):
    """不给上限时取 ``pipeline.max_clarification_rounds``（用例注入 2）。"""
    app.state.job_runner.config.max_clarification_rounds = 1
    client = _client(app)

    resp = client.get("/api/v1/clarification-questions",
                      params={"topic": "秋日庭院里的橘猫"})

    assert resp.status_code == 200
    assert resp.json() == ["问题1是什么？"]


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_returns_empty_without_building_a_model(app, limit):
    """上限 <= 0 ＝关闭澄清：空列表且**连模型都不建**（不该为一次跳过付构造代价）。"""
    client = _client(app)

    resp = client.get("/api/v1/clarification-questions",
                      params={"topic": "秋日庭院里的橘猫", "limit": limit})

    assert resp.status_code == 200
    assert resp.json() == []
    assert app.state.test_builder_calls == [], "上限为 0 时仍然建了模型"
    assert app.state.test_model.requests == []


def test_zero_configured_rounds_returns_empty_without_a_model(app):
    """配置把澄清关掉（0）时，省略上限同样返回空列表且不调模型。"""
    app.state.job_runner.config.max_clarification_rounds = 0
    client = _client(app)

    resp = client.get("/api/v1/clarification-questions",
                      params={"topic": "秋日庭院里的橘猫"})

    assert resp.status_code == 200
    assert resp.json() == []
    assert app.state.test_builder_calls == []
    assert app.state.test_model.requests == []


def test_generation_failure_returns_502_problem_json(app):
    """生成失败不吞异常：502 problem+json，前端据此显示「生成失败，可直接开始」。"""
    app.state.test_model.boom = True
    client = _client(app)

    resp = client.get("/api/v1/clarification-questions",
                      params={"topic": "秋日庭院里的橘猫"})

    body = _assert_problem(resp, 502, "clarification_generation_failed")
    assert "gateway boom" in body["detail"], "失败响应里没带病因，排障时看不出是哪一层"


def test_endpoint_requires_the_local_token(app):
    """新端点不申请鉴权豁免：没凭据就是 401，与其余 ``/api/v1`` 端点同一条边界。"""
    unauthorized = TestClient(app, base_url="http://127.0.0.1")

    resp = unauthorized.get("/api/v1/clarification-questions",
                            params={"topic": "秋日庭院里的橘猫"})

    assert resp.status_code == 401
    assert resp.json()["code"] == "auth_required"


# ---------------------------------------------------------------------------
# 创建会话：澄清问答对回填（逐字同形，且渲染只在后端一处）
# ---------------------------------------------------------------------------

PAIRS = [("庭院是哪个朝代？", "北宋"), ("人物服装按哪个年代？", "现代汉服")]

# 与 CLI 的澄清块逐字同形：编号 + ``问题 → 回答`` + 效力说明。写死整段而不是调
# ``render_clarification_block`` 现算——用同一个函数当预期值等于自己证自己。
EXPECTED_BLOCK = (
    "1. 庭院是哪个朝代？ → 北宋\n"
    "2. 人物服装按哪个年代？ → 现代汉服\n"
    "（以上是用户在生成前给出的澄清结论，属于用户原始要求的一部分："
    "必须落实，不得当成无中生有删掉。）"
)


def test_creating_a_session_with_pairs_renders_the_cli_identical_block(app):
    client = _client(app)

    resp = client.post("/api/v1/sessions", json=_bot_body(
        clarification_pairs=[{"question": q, "answer": a} for q, a in PAIRS]))

    assert resp.status_code == 201
    assert resp.json()["brief"]["clarifications"] == EXPECTED_BLOCK


def test_pairs_and_free_text_together_are_rejected(app):
    """两种澄清答案并存时 422：系统不猜哪个优先（spec #332 E1「答案回传」）。"""
    client = _client(app)

    resp = client.post("/api/v1/sessions", json=_bot_body(
        clarifications="1. 庭院是哪个朝代？ → 北宋",
        clarification_pairs=[{"question": q, "answer": a} for q, a in PAIRS]))

    body = _assert_problem(resp, 422, "clarification_conflict")
    assert "clarifications" in body["detail"], "错误文案没点名是哪个字段冲突"


def test_blank_free_text_plus_pairs_is_accepted(app):
    """空串与「没提供」同义：空自由文本 + 问答对照常创建。"""
    client = _client(app)

    resp = client.post("/api/v1/sessions", json=_bot_body(
        clarifications="",
        clarification_pairs=[{"question": q, "answer": a} for q, a in PAIRS]))

    assert resp.status_code == 201
    assert resp.json()["brief"]["clarifications"] == EXPECTED_BLOCK


def test_pairs_and_whitespace_only_free_text_are_accepted(app):
    """只有空白字符的自由文本按「没提供」处理——判据是「非空自由文本 + 非空问答对」。"""
    client = _client(app)

    resp = client.post("/api/v1/sessions", json=_bot_body(
        clarifications="   \n  ",
        clarification_pairs=[{"question": q, "answer": a} for q, a in PAIRS]))

    assert resp.status_code == 201
    assert resp.json()["brief"]["clarifications"] == EXPECTED_BLOCK


def test_blank_answers_do_not_become_a_clarification_block(app):
    """只答了问题没填答案＝没有澄清结论：落空串，不落一段空壳清单。"""
    client = _client(app)

    resp = client.post("/api/v1/sessions", json=_bot_body(
        clarification_pairs=[{"question": "庭院是哪个朝代？", "answer": "   "}]))

    assert resp.status_code == 201
    assert resp.json()["brief"]["clarifications"] == ""


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------

def test_contract_declares_the_questions_endpoint(app):
    """契约里要有这条路径、502 分支与鉴权声明（前端 #339 靠生成类型对接）。"""
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/clarification-questions"]["get"]
    responses = operation["responses"]
    for code in ("200", "401", "403", "422", "502"):
        assert code in responses, f"契约里缺 {code} 分支"
