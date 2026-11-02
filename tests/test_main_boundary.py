"""顶层异常边界（体检 P2-6，2026-11-02）。

为什么要有：``main()`` 之外的失败不能被一句裸 traceback 带走——CLI 需要稳定的
exit code 与一行上下文，同时保留堆栈供报障复制。变异检验：删掉 ``main()`` 里的
``try/except``，``RuntimeError`` 会直接穿出本用例。
"""
from __future__ import annotations

import minimax_h3_prompt.main as main_module


def test_main_reports_unexpected_failure_and_returns_one(monkeypatch, capsys):
    def boom(argv=None):
        raise RuntimeError("网关 404")

    monkeypatch.setattr(main_module, "_dispatch", boom)

    code = main_module.main([])
    captured = capsys.readouterr()

    assert code == 1
    assert "未处理的失败" in captured.err and "RuntimeError" in captured.err
    assert "Traceback" in captured.err, "边界把堆栈吞了：报障需要原文"
