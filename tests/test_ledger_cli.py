"""ledger CLI 子命令测试。"""
from __future__ import annotations

import json

from minimax_h3_prompt.main import build_parser, main


def test_parser_has_ledger_rebuild():
    args = build_parser().parse_args(["ledger", "rebuild", "S", "--output-dir", "O"])
    assert args.command == "ledger"
    assert args.ledger_command == "rebuild"
    assert args.session_dir == "S"
    assert args.output_dir == ["O"]


def test_ledger_rebuild_reports_missing_session(tmp_path, capsys):
    code = main(["ledger", "rebuild", str(tmp_path / "不存在"),
                 "--output-dir", str(tmp_path)])
    assert code == 1
    assert "不存在" in capsys.readouterr().out


def test_ledger_rebuild_prints_shot_count(tmp_path, tmp_video, capsys):
    from tests.test_ledger import _make_session  # 复用已有构造器

    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    code = main(["ledger", "rebuild", str(session), "--output-dir", str(out)])
    assert code == 0
    assert "3" in capsys.readouterr().out


def test_ledger_rebuild_json_flag_emits_json(tmp_path, tmp_video, capsys):
    from tests.test_ledger import _make_session

    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    code = main(["ledger", "rebuild", str(session), "--output-dir", str(out), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "h3-ledger/1"
    assert len(payload["shots"]) == 2
