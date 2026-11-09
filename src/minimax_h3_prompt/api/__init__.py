"""Headless HTTP 服务（FastAPI）入口包。

D1（#205）只建立最小纵向切片：健康接口、统一的 problem+json 错误契约、同源托管
前端构建产物。生成会话、执行任务等业务状态机留给后续子 spec。
"""
from __future__ import annotations

# 服务名是稳定常量：健康响应的 service 字段、包元数据查询、日志都用同一个值。
SERVICE_NAME = "minimax-h3-prompt"

__all__ = ["SERVICE_NAME"]
