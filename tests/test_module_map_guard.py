"""模块地图守卫：`api/routes/` 下每个路由模块都得在 `docs/agents/workbench.md` 里有一行。

为什么要有：2026-11-26 复审（spec #363）抓到新增的 `api/routes/canvas.py` 没进模块
地图，而同类端点 `routes/clarification.py` 有专门一行——惯例已经成文，只是没人检查。
模块地图是「这个新端点该住哪、改动该连带动谁」的导航面；漏一行不会报错，只会在下次
有人问「这个端点是谁管的」时多绕一圈。

判据是**文件名词干出现在文档里**，不是严格的行格式：文档是给人读的，写成表格还是
列表由写的人定。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = ROOT / "src" / "minimax_h3_prompt" / "api" / "routes"
MODULE_MAP = ROOT / "docs" / "agents" / "workbench.md"

# 刻意不进模块地图的模块，每条写清理由。加一项之前先问：读地图的人会不会需要它。
EXEMPT: dict[str, str] = {
    "__init__.py": "包的入口文件，没有端点",
    "health.py": "免鉴权的健康探针，启动就绪判断用；不承载业务端点",
    "validation_probe.py": "测试专用探针（include_test_routes=True 才挂载），不进生产",
}


def undocumented_routes(
    routes_dir: Path = ROUTES_DIR, module_map: Path = MODULE_MAP,
) -> list[str]:
    """路由目录下有、模块地图里没提的模块（按名排序）。"""
    text = module_map.read_text(encoding="utf-8")
    missing: list[str] = []
    for path in sorted(routes_dir.glob("*.py")):
        if path.name in EXEMPT:
            continue
        # 判据是词干出现在文档里：`canvas.py` → 文档应出现 `canvas`。
        if path.stem not in text:
            missing.append(path.name)
    return missing


def test_every_route_module_is_in_the_module_map() -> None:
    missing = undocumented_routes()
    assert not missing, (
        "这些路由模块没进 `docs/agents/workbench.md` 的模块地图："
        + "、".join(missing)
        + "。补一行（端点路径 + 职责 + 关键约束）；若它刻意不该被读到，"
        "加进本文件的 EXEMPT 并写明理由。")


def test_undocumented_routes_reports_a_new_module(tmp_path: Path) -> None:
    """反例自检：新建一个没登记的路由，守卫必须报出来（永不为红＝没有）。"""
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "documented.py").write_text("", encoding="utf-8")
    (routes / "brand_new.py").write_text("", encoding="utf-8")
    (routes / "__init__.py").write_text("", encoding="utf-8")
    module_map = tmp_path / "map.md"
    module_map.write_text("| `routes/documented.py` | 已经登记 |\n", encoding="utf-8")

    assert undocumented_routes(routes, module_map) == ["brand_new.py"]


def test_exempt_modules_are_still_real_files() -> None:
    """豁免名单不许留死条目：写了豁免的模块必须真的存在，否则是过期的挡箭牌。"""
    existing = {path.name for path in ROUTES_DIR.glob("*.py")}
    stale = sorted(set(EXEMPT) - existing)
    assert not stale, f"EXEMPT 里有已不存在的模块：{'、'.join(stale)}"
