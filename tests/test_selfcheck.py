"""#241 冻结自检的机械守卫（不真起服务、不真跑 ffmpeg）。"""
from __future__ import annotations

from pathlib import Path

from minimax_h3_prompt import bootstrap, selfcheck


def test_config_template_names_come_from_bootstrap():
    """模板清单只有一份：自检读 ``bootstrap.CONFIG_TEMPLATE_NAMES``，不复制（#243）。

    ``bootstrap`` 只依赖 ``shutil`` / ``pathlib`` / ``paths``，当初「怕 import 整条链」
    的理由不成立——而且 ``selfcheck`` 本来就 import 了 ``api.__main__``（它 import bootstrap）。
    """
    assert selfcheck.CONFIG_TEMPLATE_NAMES is bootstrap.CONFIG_TEMPLATE_NAMES


def _make_bundle(root: Path) -> Path:
    for relative in selfcheck.PACKAGE_DATA_DIRS:
        (root / "minimax_h3_prompt" / relative).mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "dist").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    for name in selfcheck.CONFIG_TEMPLATE_NAMES:
        (root / "config" / name).write_text("", encoding="utf-8")
    return root


def test_static_checks_report_missing_bundle_items_without_raising(tmp_path):
    """缺项一次列全（不修一个撞下一个），且每一项都是可读的失败行。"""
    root = _make_bundle(tmp_path / "bundle")
    (root / "config" / "image.yaml").unlink()
    (root / "frontend" / "dist" / "index.html").unlink()

    results = selfcheck.static_checks(root)

    failed = [message for ok, message in results if not ok]
    assert any("image.yaml" in message for message in failed), failed
    assert any("index.html" in message for message in failed), failed


def test_static_checks_pass_on_a_complete_bundle(tmp_path):
    root = _make_bundle(tmp_path / "bundle")

    missing = [message for ok, message in selfcheck.static_checks(root) if not ok]

    # 非冻结形态这一项在单测里必然为红（sys.frozen 未设置），其余资源项必须全绿。
    assert [message for message in missing if "sys.frozen" not in message] == [], missing


def test_upload_fixtures_are_real_png_bytes():
    for data in (selfcheck.PNG_FIRST, selfcheck.PNG_LAST):
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert selfcheck.PNG_FIRST != selfcheck.PNG_LAST


def test_request_url_percent_encodes_non_ascii_session_ids():
    """中文主题会进会话 id；HTTP 请求行只收 ASCII（冻结实跑抓到的）。"""
    url = selfcheck.request_url(
        "http://127.0.0.1:9/", "/api/v1/sessions/雨夜旧信/1/jobs")

    assert url == (
        "http://127.0.0.1:9/api/v1/sessions/"
        "%E9%9B%A8%E5%A4%9C%E6%97%A7%E4%BF%A1/1/jobs"
    )
