"""干净环境模式的自我检验（retro 2026-11-02）。

``scripts/check.sh clean`` 会把本机 ``.env`` 从测试进程里摘掉，模拟 CI 的干净
检出——2026-11-02 首跑 CI 红了三次，其中一次就是本机有 key、远端没有。

这条用例守的是**模式本身**：如果 conftest 的 fixture 哪天失效（改坏、被删、
条件写错），clean 跑起来会「全绿」但什么也没模拟，等于这道防线不存在。
其他时候（没开 clean）它自己跳过。
"""
from __future__ import annotations

import os

import pytest

from minimax_h3_prompt.config import config
from minimax_h3_prompt.errors import ConfigError

_ENABLED = os.getenv("H3_TEST_NO_ENV", "").strip().lower() in {"1", "true", "yes"}


@pytest.mark.skipif(not _ENABLED, reason="只在 scripts/check.sh clean 下有意义")
def test_clean_mode_really_removes_the_api_keys():
    with pytest.raises(ConfigError):
        config.resolve_api_key(config.primary_model)
