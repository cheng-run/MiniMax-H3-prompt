"""节点包导入级检查（不启动 ComfyUI、不跑生成）。

**用途**：换机或改完节点包后，先确认「模块能加载、三个节点已注册、24 个图槽
名字齐全、上游依赖在位」，再打开 ComfyUI。真正的生成必须由人在 ComfyUI 里跑
（issue #69 的验收项在 #73），本脚本刻意不碰 torch / 模型。

用法::

    "<ComfyUI>/python_embeded/python.exe" docs/comfyui/verify_nodes_import.py \
        --node-dir "<ComfyUI>/custom_nodes/MiniMaxH3-Refs24"

不传 ``--node-dir`` 时检查仓库内副本 ``docs/comfyui/MiniMaxH3-Refs24``。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _load_slot_constants():
    """槽位数从节点包**动态取**，不在这里另抄一份（复审 T1）。

    校验脚本若自己写 24/3/3，改节点包后脚本仍按旧数核对：要么把正确的包判成
    「槽位不符」，要么（更坏）漏掉真实的槽位漂移——判据与被判物必须同源。
    """
    path = Path(__file__).resolve().parent / "MiniMaxH3-Refs24" / "nodes_refs24.py"
    spec = importlib.util.spec_from_file_location("refs24_slots_for_verifier", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 仓库副本缺失才发生
        raise SystemExit(f"[fail] 无法加载节点槽位常量：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SLOTS = _load_slot_constants()
IMAGE_SLOTS = _SLOTS.REF_IMAGE_SLOTS
VIDEO_SLOTS = _SLOTS.REF_VIDEO_SLOTS
VIDEO_AUDIO_SLOTS = _SLOTS.VIDEO_AUDIO_SLOTS
AUDIO_SLOTS = _SLOTS.REF_AUDIO_SLOTS
ALL_SLOTS = (
    tuple(f"ref_image_{i}" for i in range(IMAGE_SLOTS))
    + tuple(f"ref_video_{i}" for i in range(VIDEO_SLOTS))
    + tuple(f"ref_video_audio_{i}" for i in range(VIDEO_AUDIO_SLOTS))
    + tuple(f"ref_audio_{i}" for i in range(AUDIO_SLOTS))
)
EXPECTED_NODES = {
    "MiniMaxH3Refs24BusOut",
    "MiniMaxH3Refs24BusIn",
    "MiniMaxH3Refs24ReferenceToVideo",
}
UPSTREAM_DIR_NAME = "ComfyUI-JZL-MiniMax-H3"
UPSTREAM_KEY = "JZL_MiniMaxH3ReferenceToVideo"


def _load_package(node_dir: Path):
    """按 ComfyUI 的加载方式 exec 包（spec + 包搜索路径），不启动服务。"""
    init_path = node_dir / "__init__.py"
    if not init_path.exists():
        raise SystemExit(f"[fail] 找不到 {init_path}")
    name = "minimaxh3_refs24_import_check"
    spec = importlib.util.spec_from_file_location(
        name, init_path, submodule_search_locations=[str(node_dir)]
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"[fail] 无法为 {init_path} 建立模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _check_upstream(node_dir: Path) -> None:
    """静态确认上游编码节点在位；不在位时本包执行期会报错。

    仓库副本（docs/comfyui/）旁边没有上游目录，属于正常情况：只警告。
    装在 custom_nodes/ 下时上游是硬依赖，缺了直接判失败。
    """
    upstream = node_dir.parent / UPSTREAM_DIR_NAME
    nodes_py = upstream / "nodes.py"
    init_py = upstream / "__init__.py"
    installed = node_dir.parent.name == "custom_nodes"
    if not nodes_py.exists() or not init_py.exists():
        if installed:
            raise SystemExit(
                f"[fail] 未找到上游节点包 {upstream}；本包只借它的编码实现（ADR 0017）"
            )
        print(f"[warn] 仓库副本旁没有上游包 {upstream}（换机安装后再查）")
        return
    if f"class {UPSTREAM_KEY}" not in nodes_py.read_text(encoding="utf-8"):
        raise SystemExit(f"[fail] 上游 nodes.py 里没有 class {UPSTREAM_KEY}")
    if f'"{UPSTREAM_KEY}"' not in init_py.read_text(encoding="utf-8"):
        raise SystemExit(f"[fail] 上游未注册节点键 {UPSTREAM_KEY}")
    print(f"[ok] 上游编码节点在位：{upstream}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniMaxH3-Refs24 导入级检查")
    default_dir = Path(__file__).resolve().parent / "MiniMaxH3-Refs24"
    parser.add_argument("--node-dir", default=str(default_dir), help="节点包目录")
    args = parser.parse_args(argv)
    node_dir = Path(args.node_dir).resolve()

    module = _load_package(node_dir)
    print(f"[ok] 节点包加载成功：{node_dir}")

    mappings = module.NODE_CLASS_MAPPINGS
    if set(mappings) != EXPECTED_NODES:
        raise SystemExit(f"[fail] 注册节点不符：{sorted(mappings)}")
    print(f"[ok] 已注册节点：{', '.join(sorted(mappings))}")

    bus_out = mappings["MiniMaxH3Refs24BusOut"]
    bus_in = mappings["MiniMaxH3Refs24BusIn"]
    encoder = mappings["MiniMaxH3Refs24ReferenceToVideo"]

    if tuple(bus_out.INPUT_TYPES()["optional"]) != ALL_SLOTS:
        raise SystemExit("[fail] 总线打包端槽位不是 24 图 + 3 视频 + 3 音轨 + 3 音频")
    if tuple(bus_in.RETURN_NAMES) != ALL_SLOTS:
        raise SystemExit("[fail] 总线解包端槽位顺序与打包端不一致")
    if tuple(encoder.INPUT_TYPES()["optional"]) != ALL_SLOTS:
        raise SystemExit("[fail] 编码节点槽位不是 24 图 + 3 视频 + 3 音轨 + 3 音频")
    print(f"[ok] 图槽 {IMAGE_SLOTS}：ref_image_0..ref_image_{IMAGE_SLOTS - 1}")

    _check_upstream(node_dir)
    print("[ok] 导入级检查全部通过（未启动 ComfyUI、未跑生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
