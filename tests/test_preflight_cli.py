"""preflight CLI 测试。"""
from __future__ import annotations

from minimax_h3_prompt.main import build_parser, main


def test_parser_has_preflight():
    args = build_parser().parse_args(["preflight", "--input-frame", "f.png"])
    assert args.command == "preflight"
    assert args.input_frame == "f.png"


def test_cli_clean_input_returns_zero(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    tail = read_window(prev, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev)])
    assert code == 0
    assert "通过" in capsys.readouterr().out


def test_cli_mismatched_input_returns_one(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    other = tmp_video("other.mp4", base=(200, 200, 200))
    tail = read_window(other, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev)])
    assert code == 1
    assert "对不上" in capsys.readouterr().out


def test_cli_frozen_word_warns_but_passes(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    tail = read_window(prev, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())
    prompt = tmp_path / "hook.txt"
    prompt.write_text("少年双手停在巨书封面上", encoding="utf-8")

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev), "--prompt", str(prompt)])
    assert code == 0, "启发式命中不应阻断"
    assert "停" in capsys.readouterr().out


def test_cli_budget_refrain_reported(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    tail = read_window(prev, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev),
                 "--budget-tokens", "1000", "--tokens-per-shot", "500",
                 "--remaining-shots", "5"])
    assert code == 0
    assert "还能跑" in capsys.readouterr().out
