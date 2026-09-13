"""中文摘要模块单测：LLM 容错、JSON 解析、渲染与按 Shot 切取。"""
from __future__ import annotations

import json

from minimax_h3_prompt.summary import (
    PromptSummary,
    render_summary_zh,
    summarize_prompt_zh,
)


class _StubLLM:
    """invoke() 返回预设文本；异常用 _BoomLLM。"""

    def __init__(self, text: str):
        self._text = text

    def invoke(self, _request: str):
        return self._text


class _BoomLLM:
    def invoke(self, _request: str):
        raise RuntimeError("network down")


_GOOD_PAYLOAD = json.dumps({
    "overall": "叶舟在雨夜赶路，最终在桥边停下回望。",
    "shots": [
        {"shot": 1, "visual": "雨夜街头，叶舟撑伞前行", "audio": "雨声与脚步"},
        {"shot": 2, "visual": "桥边停下，回头凝望", "audio": "配乐渐强"},
    ],
}, ensure_ascii=False)


def test_summarize_ok():
    summary = summarize_prompt_zh("irrelevant prompt", _StubLLM(_GOOD_PAYLOAD))
    assert summary is not None
    assert "雨夜" in summary.overall
    assert len(summary.shots) == 2
    assert summary.shot_view(2)["audio"] == "配乐渐强"
    assert summary.shot_view(99) is None


def test_summarize_tolerates_code_fence():
    fenced = "```json\n" + _GOOD_PAYLOAD + "\n```"
    summary = summarize_prompt_zh("p", _StubLLM(fenced))
    assert summary is not None
    assert len(summary.shots) == 2


def test_summarize_returns_none_on_bad_json():
    assert summarize_prompt_zh("p", _StubLLM("不是 JSON")) is None
    assert summarize_prompt_zh("p", _StubLLM('{"shots": []}')) is None  # 缺 overall
    assert summarize_prompt_zh("p", _StubLLM("")) is None


def test_summarize_returns_none_on_llm_exception():
    assert summarize_prompt_zh("p", _BoomLLM()) is None


def test_render_full_includes_all_shots():
    summary = PromptSummary(overall="走向文字", shots=[
        {"shot": 1, "visual": "画面一", "audio": "声音一"},
        {"shot": 2, "visual": "画面二", "audio": "声音二"},
    ])
    text = render_summary_zh(summary)
    assert "【整条视频走向】" in text
    assert "镜头1" in text and "镜头2" in text
    assert "画面" in text and "声音" in text


def test_render_single_shot():
    summary = PromptSummary(overall="走向", shots=[
        {"shot": 1, "visual": "画面一", "audio": "声音一"},
    ])
    text = render_summary_zh(summary, shot_number=1)
    assert "画面一" in text and "走向" not in text
    assert "未找到" in render_summary_zh(summary, shot_number=7)
