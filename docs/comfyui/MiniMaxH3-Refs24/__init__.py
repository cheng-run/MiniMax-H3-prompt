"""MiniMaxH3-Refs24 节点注册入口。

ComfyUI 按 ``custom_nodes/<包名>/__init__.py`` 加载；这里只导出 V1 映射，
避免在加载期 import torch / 上游包（上游在注册表里按需取用，见 nodes_refs24.py）。
"""

from .nodes_refs24 import (
    MiniMaxH3Refs24BusIn,
    MiniMaxH3Refs24BusOut,
    MiniMaxH3Refs24ReferenceToVideo,
)

NODE_CLASS_MAPPINGS = {
    "MiniMaxH3Refs24BusOut": MiniMaxH3Refs24BusOut,
    "MiniMaxH3Refs24BusIn": MiniMaxH3Refs24BusIn,
    "MiniMaxH3Refs24ReferenceToVideo": MiniMaxH3Refs24ReferenceToVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3Refs24BusOut": "Refs24 - 🔗 24图槽参考总线（打包）",
    "MiniMaxH3Refs24BusIn": "Refs24 - 🔗 24图槽参考总线（解包）",
    "MiniMaxH3Refs24ReferenceToVideo": "Refs24 - 🎬 MiniMax H3 参考编码（24图槽）",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
