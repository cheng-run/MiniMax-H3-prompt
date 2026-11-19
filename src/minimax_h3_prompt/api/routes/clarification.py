"""起步澄清问题生成端点：``GET /api/v1/clarification-questions``（#338 / spec #332 E1）。

**为什么是独立路径而不是会话子资源**：提问发生在**创建会话之前**——会话还没有标识，
挂不进 ``/sessions/{id}/...``。放独立段也顺带避开 ``{session_id:path}`` 吞斜杠那条
路由顺序规矩（本仓在 ``/assets``、``/media``、``/jobs`` 上已经吃过一次）。

**为什么走 ``api.jobs.build_chat_model``**：那是工作台侧的模型构造缝（测试在这一层
打桩，见 ``tests/test_api_clarification_questions.py``）。这里按模块属性取值、不在
import 期绑定，桩才换得掉。

**为什么失败落 502 而不是空列表**：空列表的语义是「这个主题不需要澄清」，与「网关
挂了」是两件事；前端要靠状态码区别「没有问题」与「生成失败，可直接开始」（spec #332
User Story 4 / 17）。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...clarification import generate_clarifying_questions
from ...errors import ConfigError
from ..problems import AUTH_ERROR_RESPONSES, COMMON_ERROR_RESPONSES, ProblemDetail, ProblemError

router = APIRouter(prefix="/api/v1", tags=["clarification"],
                   responses=AUTH_ERROR_RESPONSES)


class ClarificationGenerationFailed(ProblemError):
    """澄清问题生成失败（网关/超时/空输出）：上游故障，不是请求写错。"""

    status_code = 502
    code = "clarification_generation_failed"


_RESPONSES = {
    **COMMON_ERROR_RESPONSES,
    502: {"model": ProblemDetail,
          "description": "澄清问题生成失败：网关/超时/空输出（problem+json）"},
}


@router.get(
    "/clarification-questions",
    response_model=list[str],
    responses=_RESPONSES,
)
def list_clarification_questions(
    request: Request,
    topic: str = Query(min_length=1, description="视频主题；问题按它现生成"),
    limit: int | None = Query(
        default=None,
        description="条数上限；省略时取 pipeline.max_clarification_rounds，<= 0 表示不问"),
) -> list[str]:
    """按主题现生成起步澄清问题（只读，不建会话、不落盘）。

    上限 <= 0 时**连模型都不建**：关闭澄清的路由不该为一次跳过付客户端构造代价，
    这条也由 ``clarification.generate_clarifying_questions`` 自己再守一道
    （``limit <= 0`` 直接返回空列表）。
    """
    from .. import jobs

    if limit is None:
        # 缺省取配置（pipeline.max_clarification_rounds）：与 CLI 向导读的是同一个
        # 键，两处入口的条数因此不会分家。**不写 getattr 兜底**——配置对象缺这个键
        # 时宁可 500（接线断了要看得见），也不回落成「这个主题不需要澄清」的空列表。
        limit = int(request.app.state.job_runner.config.max_clarification_rounds)
    if limit <= 0:
        return []
    if request.app.state.job_runner.fake_pipeline:
        # 桩管线（D4 §11）：澄清与阶段 1/2 同属「桩下不建任何模型」这条判据。创建表单
        # 每次提交前都会问一次，桩若不接管，这里就会去建真模型——CI 无密钥当场 502、
        # 本机有密钥则真调网关（#339 实测）。
        from ..fake_pipeline import clarification_questions

        return clarification_questions(topic, limit)
    try:
        return generate_clarifying_questions(topic, jobs.build_chat_model(), limit)
    except (RuntimeError, ConfigError, OSError) as exc:
        # 模型构造（缺 key → ConfigError）与生成失败（RuntimeError）同属「这一次拿不到
        # 问题」，对前端是同一条出路；只把病因写进 detail，不让它变成 500 堆栈。
        raise ClarificationGenerationFailed(
            f"澄清问题生成失败：{exc}") from exc
