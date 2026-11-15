"""启动脚本的前端构建新鲜度守卫（#275 / spec #274 §A）。

现场：拉下前端改动后直接一键启动，浏览器里还是上一次构建的旧产物，脚本一声不吭
（#251 的 18 秒 FL2V 验收因此对着旧界面排查）。这里只钉「给定文件时间戳 → 是否调用
构建」这一层外部可观察的行为：不真跑 Vite，构建器是注入的假实现。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START_WEB = ROOT / "scripts" / "start_web.py"
BUILD_EXE = ROOT / "scripts" / "build_exe.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 模块级 dataclass/函数按 __module__ 回查 sys.modules，不登记会拿到 None。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


start_web = _load(START_WEB, "start_web_under_test")


class RecordingBuild:
    """假构建器：只数自己被调用几次，不碰 Vite。"""

    def __init__(self, code: int = 0) -> None:
        self.calls = 0
        self.code = code

    def __call__(self) -> int:
        self.calls += 1
        return self.code


def _make_tree(
    tmp_path: Path,
    *,
    dist_mtime: float | None,
    source_mtime: float,
    node_modules: bool = True,
) -> tuple[Path, Path, Path]:
    """造一棵最小前端树：``src/App.vue`` + ``package.json`` + ``dist/index.html``。

    mtime 用 ``os.utime`` 显式设定，不靠「先写谁」——文件系统时间戳精度与写入顺序
    会让「比谁新」的用例偶发翻车。
    """
    root = tmp_path / "frontend"
    (root / "src").mkdir(parents=True)
    sources = [root / "src" / "App.vue", root / "package.json"]
    sources[0].write_text("<template />\n", encoding="utf-8")
    sources[1].write_text("{}\n", encoding="utf-8")
    for path in sources:
        os.utime(path, (source_mtime, source_mtime))

    dist_index = root / "dist" / "index.html"
    if dist_mtime is not None:
        dist_index.parent.mkdir()
        dist_index.write_text("<!doctype html>\n", encoding="utf-8")
        os.utime(dist_index, (dist_mtime, dist_mtime))

    if node_modules:
        (root / "node_modules").mkdir()
    return root, dist_index, root / "node_modules"


def _build(tmp_path: Path, *, dist_mtime, source_mtime, rebuild=False,
           node_modules=True) -> RecordingBuild:
    root, dist_index, node_modules_dir = _make_tree(
        tmp_path, dist_mtime=dist_mtime, source_mtime=source_mtime,
        node_modules=node_modules)
    build = RecordingBuild()
    start_web._build_frontend(
        rebuild, frontend_root=root, dist_index=dist_index,
        node_modules=node_modules_dir, build=build)
    return build


def test_source_newer_than_the_build_triggers_a_rebuild(tmp_path, capsys):
    build = _build(tmp_path, dist_mtime=1000.0, source_mtime=2000.0)

    assert build.calls == 1
    out = capsys.readouterr().out
    assert start_web.BUILD_REASON_SOURCE_NEWER in out
    assert "已重新构建" in out


def test_product_newer_than_sources_is_not_rebuilt(tmp_path, capsys):
    build = _build(tmp_path, dist_mtime=3000.0, source_mtime=1000.0)

    assert build.calls == 0
    # 「没变」不该有噪音：一行提示都不许打。
    assert capsys.readouterr().out == ""


def test_missing_product_enters_the_build_path(tmp_path, capsys):
    build = _build(tmp_path, dist_mtime=None, source_mtime=1000.0)

    assert build.calls == 1
    assert start_web.BUILD_REASON_DIST_MISSING in capsys.readouterr().out


def test_missing_node_modules_exits_nonzero_with_an_actionable_hint(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        _build(tmp_path, dist_mtime=1000.0, source_mtime=2000.0,
               node_modules=False)

    message = str(excinfo.value.code)
    assert excinfo.value.code  # 非零：真实进程会以失败码退出
    assert "npx pnpm@11.7.0 install" in message


def test_missing_dependencies_does_not_run_the_build(tmp_path):
    root, dist_index, node_modules_dir = _make_tree(
        tmp_path, dist_mtime=1000.0, source_mtime=2000.0, node_modules=False)
    build = RecordingBuild()
    with pytest.raises(SystemExit):
        start_web._build_frontend(
            False, frontend_root=root, dist_index=dist_index,
            node_modules=node_modules_dir, build=build)

    assert build.calls == 0


def test_explicit_rebuild_forces_a_build_even_when_fresh(tmp_path, capsys):
    build = _build(tmp_path, dist_mtime=3000.0, source_mtime=1000.0,
                   rebuild=True)

    assert build.calls == 1
    assert start_web.BUILD_REASON_FORCED in capsys.readouterr().out


@pytest.mark.parametrize("manifest", ["pnpm-lock.yaml", "pnpm-workspace.yaml"])
def test_only_a_dependency_manifest_newer_than_the_build_still_rebuilds(
        tmp_path, capsys, manifest):
    """依赖清单比 dist 新也必须重建（spec #274 §A：包清单含依赖清单）。

    `src/` 与 `package.json` 都比 dist 旧、dist 本身完好，唯一变新的只有依赖清单。
    漏收它时，`pnpm install` 换过依赖树而产物仍是按旧依赖构建的，浏览器跑在与磁盘
    代码不匹配的运行时上，脚本却判「新鲜」。
    """
    root, dist_index, node_modules_dir = _make_tree(
        tmp_path, dist_mtime=1500.0, source_mtime=1000.0)
    manifest_path = root / manifest
    manifest_path.write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    os.utime(manifest_path, (2000.0, 2000.0))

    build = RecordingBuild()
    start_web._build_frontend(
        False, frontend_root=root, dist_index=dist_index,
        node_modules=node_modules_dir, build=build)

    assert build.calls == 1
    assert start_web.BUILD_REASON_SOURCE_NEWER in capsys.readouterr().out


def test_frozen_packaging_never_consults_the_freshness_check():
    """冻结打包流程无条件从当前源码构建，不接入新鲜度判断（spec #274 §A）。

    只要有人把 ``build_exe`` 接到 start_web 的新鲜度判据上（例如 import 它的
    ``build_reason``），这条就红——否则冻结版会按 mtime 跳过构建，交付的 EXE 带着
    旧前端。
    """
    source = BUILD_EXE.read_text(encoding="utf-8")

    assert "start_web" not in source
    assert "build_reason" not in source
    assert "_newest_source_mtime" not in source
