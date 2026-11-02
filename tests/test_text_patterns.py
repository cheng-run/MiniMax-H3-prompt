"""判据单一来源（体检 P0-4／P0-5 的第一条守卫，2026-11-02）。

同一语义在多个模块各写一份正则，改一处漏一处**不会报错**；本仓已经出现过同名不同义：
``segment_prompts._BARE_TIMESTAMP_LINE`` 不允前导空白且捕获三组，而
``h3_validator._BARE_TIMESTAMP_LINE`` 允许前导空白且不捕获。

这里用**对象身份**（``is``）钉住：消费模块拿到的必须是 ``text_patterns`` 里那一个
对象。任何人重新写一份字面量，本用例当场红——比对比 ``.pattern`` 字符串更硬，因为
两份字面量即使当前完全相同也逃不过身份检查。
"""
from __future__ import annotations

import importlib

from minimax_h3_prompt import text_patterns

# (模块, 模块里的名字, text_patterns 里的真源)
_SHARED = [
    ("minimax_h3_prompt.generation", "_SHA256", "SHA256_HEX"),
    ("minimax_h3_prompt.asset_ledger", "_SHA256_RE", "SHA256_HEX"),
    ("minimax_h3_prompt.project_models", "_SHA256", "SHA256_HEX"),
    ("minimax_h3_prompt.task_package", "_SHA256_RE", "SHA256_HEX"),
    ("minimax_h3_prompt.segment_prompts", "_SHOT_BLOCK", "SHOT_TAG"),
    ("minimax_h3_prompt.segment_refs", "_SHOT_TAG_RE", "SHOT_TAG"),
    ("minimax_h3_prompt.tools.h3_validator", "_SHOT_RE", "SHOT_TAG"),
    ("minimax_h3_prompt.segment_refs", "_PICTURE_RE", "PICTURE_TAG"),
    ("minimax_h3_prompt.tools.h3_validator", "_PICTURE_RE", "PICTURE_TAG"),
    ("minimax_h3_prompt.director.pack", "_SLOT_TOKEN_RE", "PICTURE_TAG"),
    ("minimax_h3_prompt.segment_refs", "_SUBJECT_RE", "SUBJECT_TAG"),
    ("minimax_h3_prompt.segment_prompts", "_AT_TIME_RE", "AT_TIMESTAMP"),
    ("minimax_h3_prompt.tools.h3_validator", "_TIME_RE", "AT_TIMESTAMP"),
    ("minimax_h3_prompt.director.firstframe", "_SHOT_TIME", "AT_TIMESTAMP"),
    ("minimax_h3_prompt.director.prompts", "_AT_TIME_RE", "AT_TIMESTAMP"),
    ("minimax_h3_prompt.segment_prompts", "_BARE_TIMESTAMP_LINE", "LEADING_TIMESTAMP"),
    ("minimax_h3_prompt.tools.h3_validator", "_BARE_TIMESTAMP_LINE", "TIMESTAMP_ONLY_LINE"),
]


def test_every_consumer_imports_the_single_source_object():
    for module_name, attribute, shared_name in _SHARED:
        module = importlib.import_module(module_name)
        shared = getattr(text_patterns, shared_name)
        assert getattr(module, attribute) is shared, (
            f"{module_name}.{attribute} 又抄了一份 {shared_name}；"
            "改 text_patterns 不会同时改它")


def test_the_two_bare_timestamp_predicates_stay_distinct():
    """同名不同义的两种「裸时间戳行」必须继续分开：合并会静默改掉一条路径。

    - ``segment_prompts`` 的那条用来**剥段落前缀**：不允许前导空白，捕获三组；
    - ``h3_validator`` 的那条用来判「整行只有时间戳」：允许前导空白，不捕获。

    变异检验：把两者换成同一对象，本用例红。
    """
    from minimax_h3_prompt.segment_prompts import _BARE_TIMESTAMP_LINE as leading
    from minimax_h3_prompt.tools.h3_validator import _BARE_TIMESTAMP_LINE as only_line

    assert leading is not only_line
    assert leading.match("  At 00:01.000") is None, "剥前缀的那条不该接受前导空白"
    assert only_line.match("  At 00:01.000") is not None, "判整行的那条应当接受前导空白"
