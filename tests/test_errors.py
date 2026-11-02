"""异常分类（体检 §4.1，P3）。

三条不变式：

1. **兼容**：新异常仍能被 ``except ValueError`` / ``except RuntimeError`` 接住——
   存量的分类处理器一行都不用改，这是能安全落地的前提；
2. **可辨**：``error_code`` 给classified异常稳定短码，未分类的退回类名，绝不猜；
3. **接线**：配置层与 brief 解析层真的抛新类型（只写类不算数，本仓「规则在、
   接线不在」的纪律）。
"""
from __future__ import annotations

import pytest

import minimax_h3_prompt.main as main_module
from minimax_h3_prompt import config as config_module
from minimax_h3_prompt.brief_parser import validate_director_mode
from minimax_h3_prompt.errors import (
    ConfigError,
    ExternalServiceError,
    H3Error,
    ModelOutputError,
    UserInputError,
    error_code,
)


def test_classified_errors_stay_catchable_as_builtin_types():
    """变异检验：把 ``UserInputError(ValueError, H3Error)`` 改成只继承 H3Error，
    本用例红——存量 ``except ValueError`` 会漏掉它，等于静默改变成百处控制流。"""
    for cls in (UserInputError, ModelOutputError, ConfigError):
        assert issubclass(cls, ValueError)
        assert issubclass(cls, H3Error)
    assert issubclass(ExternalServiceError, RuntimeError)
    assert issubclass(ExternalServiceError, H3Error)

    with pytest.raises(ValueError):
        raise ConfigError("配置写错")


def test_error_codes_are_stable_short_codes():
    assert error_code(UserInputError("x")) == "USER_INPUT"
    assert error_code(ModelOutputError("x")) == "MODEL_OUTPUT"
    assert error_code(ConfigError("x")) == "CONFIG"
    assert error_code(ExternalServiceError("x")) == "EXTERNAL_SERVICE"


def test_unclassified_errors_report_their_class_name():
    """没分类不等于没有码：返回类名，日志里一眼能看出「这里还没归类」。"""
    assert error_code(KeyError("k")) == "KeyError"
    assert error_code(RuntimeError("boom")) == "RuntimeError"


def test_config_layer_raises_config_error(tmp_path, monkeypatch):
    """配置层真接线：画布非 32 对齐 → ConfigError（且仍是 ValueError）。"""
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("director:\n  width: 700\n  height: 1280\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)

    cfg = config_module.Config()
    with pytest.raises(ConfigError):
        cfg.director_settings()
    with pytest.raises(ValueError):
        cfg.director_settings()


def test_brief_parser_raises_user_input_error():
    with pytest.raises(UserInputError, match="导演台模式无效"):
        validate_director_mode("T2V")


def test_cli_boundary_prints_the_error_code(monkeypatch, capsys):
    """顶层边界带上分类码：用户拿到的第一行就能判断该改配置还是该重试。"""

    def boom(argv=None):
        raise ConfigError("config/agent.yaml 的 director.width 必须是 32 的倍数")

    monkeypatch.setattr(main_module, "_dispatch", boom)
    assert main_module.main([]) == 1
    err = capsys.readouterr().err
    assert "CONFIG" in err and "ConfigError" in err
