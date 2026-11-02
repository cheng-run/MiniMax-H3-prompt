"""audit CLI 子命令测试。"""
from __future__ import annotations

from minimax_h3_prompt.main import build_parser, main


def test_parser_has_audit_run():
    args = build_parser().parse_args(["audit", "run", "S", "--output-dir", "O"])
    assert args.command == "audit"
    assert args.audit_command == "run"
    assert args.session_dir == "S"


def test_parser_has_audit_promote():
    args = build_parser().parse_args(["audit", "promote", "S", "--label", "修复前"])
    assert args.audit_command == "promote"
    assert args.label == "修复前"


def test_audit_run_missing_session_returns_one(tmp_path, capsys):
    code = main(["audit", "run", str(tmp_path / "无"), "--output-dir", str(tmp_path)])
    assert code == 1
    assert "不存在" in capsys.readouterr().out


def test_audit_run_writes_report(tmp_path, tmp_video, capsys):
    from tests.test_ledger import _make_session

    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    code = main(["audit", "run", str(session), "--output-dir", str(out), "--no-merge"])
    assert code == 0
    assert (session / "audit").is_dir()
    output = capsys.readouterr().out
    assert "每缝均值" in output
