"""环境隔离的自我检验（retro 2026-11-02；2026-11-12 反转为默认隔离）。

原先只有 ``scripts/check.sh clean`` 才摘本机 ``.env``，于是同一个用例在本机有 key
时绿、在干净检出红——2026-11-12 实测一条缺 key 用例在工作树（无 ``.env``）绿、
拆回 main 后全量红。现在 conftest **默认**隔离；``H3_TEST_USE_ENV=1`` 才放行真实
环境。这条用例守的正是「默认隔离真的在生效」：fixture 失效时它会红，而不是
悄悄放行。
"""
from __future__ import annotations

import os

import pytest

from minimax_h3_prompt.config import config
from minimax_h3_prompt.errors import ConfigError

_ALLOWED = os.getenv("H3_TEST_USE_ENV", "").strip().lower() in {"1", "true", "yes"}


@pytest.mark.skipif(_ALLOWED, reason="H3_TEST_USE_ENV=1 时放行真实环境，本用例不适用")
def test_default_mode_really_removes_the_api_keys():
    with pytest.raises(ConfigError):
        config.resolve_api_key(config.primary_model)
