"""异常分类（体检 §4.1，P3）。

**为什么要有**：全仓 427 处 ``raise ValueError``，调用方无法区分「用户输入错」
「模型输出坏」「配置错」「外部服务错」，只能解析错误文案；重试/回退/人判的边界
因此都变粗。

**兼容性是硬约束**：所有新异常都继承它原来对应的内置异常，``except ValueError`` /
``except RuntimeError`` 的既有处理器一行都不用改。这不是权宜之计——本仓大量
``except ValueError`` 是**有意**的分类处理器，改它们属于单独一票的事。

**归类纪律**（新代码按此分，存量逐步迁）：

- ``UserInputError``：用户输入/brief 文件/命令行参数写错 → 用户自己能改；
- ``ModelOutputError``：模型产出的文本/JSON 不符合契约 → 修复轮该接住；
- ``ConfigError``：``config/*.yaml``、``.env``、缺失 API key → 用户改配置能修；
- ``ExternalServiceError``：网关/ffmpeg/ComfyUI 等外部依赖失败 → 重试或换环境。

``error_code`` 给日志与顶层边界用：分类过的返回稳定短码，没分类的退回异常类名
——**绝不猜**（"没分类"本身就是要能看见的信号）。
"""
from __future__ import annotations

__all__ = [
    "ConfigError",
    "ExternalServiceError",
    "H3Error",
    "ModelOutputError",
    "UserInputError",
    "error_code",
]


class H3Error(Exception):
    """本项目所有已分类异常的基类。"""

    code = "H3"


class UserInputError(ValueError, H3Error):
    """用户可自行修正的输入错误（brief 行格式、画布写法、命令行取值）。"""

    code = "USER_INPUT"


class ModelOutputError(ValueError, H3Error):
    """模型产出不符合契约（JSON 缺字段、提示词段落缺失、规划不可落位）。"""

    code = "MODEL_OUTPUT"


class ConfigError(ValueError, H3Error):
    """配置错误（YAML 未知键、类型不对、缺 API key、路径不合法）。"""

    code = "CONFIG"


class ExternalServiceError(RuntimeError, H3Error):
    """外部依赖失败（网关、ffmpeg、ComfyUI）。重试可能好，改代码不会好。"""

    code = "EXTERNAL_SERVICE"


def error_code(exc: BaseException) -> str:
    """异常的分类短码；未分类的退回异常类名，不猜。"""
    return getattr(exc, "code", None) or type(exc).__name__
