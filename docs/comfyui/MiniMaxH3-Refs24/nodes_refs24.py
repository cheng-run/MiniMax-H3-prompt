"""MiniMaxH3-Refs24 — 24 图槽参考总线与 H3 编码节点（本机包）。

**为什么单独建包**：上游 ``ComfyUI-JZL-MiniMax-H3`` 把 9 个图槽写死在
``BUS_KEYS``（``nodes_ref2va_bus.py``）和固定端口节点里，但 9 只是官方节点的
**UI 上限**，模型侧 ``minimax_refs`` 接受任意长度列表（ADR 0017）。上游目录是
第三方代码，就地改会被升级覆盖，所以把 24 槽版本放在本机新包里。

**为什么不复制编码实现**：编码逻辑（参考图缩放、VAE 编码、Qwen 令牌化、
``minimax_refs`` 条件写入）只在上游维护。这里在执行时从 ComfyUI 的节点注册表
按公开键名取上游 ``JZL_MiniMaxH3ReferenceToVideo``，把 24 张图装成它本来就接受的
dict 再调用；上游更新编码逻辑时本包自动跟随，上游若改键名会在执行时报出明确
病因，而不是静默少喂图（issue #69）。

**为什么用独立总线类型**：上游 18 端口总线与这里 33 端口总线若共用
``JZL_REF2VA_BUS``，两端可以互插；解包端只会拿到 ``None`` 并静默少喂图，
正是本票要消灭的那种静默（issue #64 问题 3）。
"""

from __future__ import annotations

# 图槽数来自 ADR 0017 的实测需求（24 张素材），不是模型上限。
REF_IMAGE_SLOTS = 24
REF_VIDEO_SLOTS = 3
VIDEO_AUDIO_SLOTS = 3
REF_AUDIO_SLOTS = 3

IMAGE_KEYS = tuple(f"ref_image_{i}" for i in range(REF_IMAGE_SLOTS))
VIDEO_KEYS = tuple(f"ref_video_{i}" for i in range(REF_VIDEO_SLOTS))
VIDEO_AUDIO_KEYS = tuple(f"ref_video_audio_{i}" for i in range(VIDEO_AUDIO_SLOTS))
AUDIO_KEYS = tuple(f"ref_audio_{i}" for i in range(REF_AUDIO_SLOTS))

# 打包/解包两端共用同一顺序；解包端的第 N 个输出必须正好对应第 N 张图。
BUS_KEYS = IMAGE_KEYS + VIDEO_KEYS + VIDEO_AUDIO_KEYS + AUDIO_KEYS

# 独立类型名：防止 18 槽与 24 槽总线互插（互插只会静默丢图，不会报错）。
BUS_TYPE = "MINIMAX_H3_REFS24_BUS"

# 上游公开注册键：只按这个名字取类，不 import 带连字符的目录。
UPSTREAM_NODE_KEY = "JZL_MiniMaxH3ReferenceToVideo"


def _bus_type(key: str) -> str:
    """按端口名推断类型：含 audio 为 AUDIO，其余为 IMAGE。"""
    return "AUDIO" if "audio" in key else "IMAGE"


def _upstream_encode_class():
    """从 ComfyUI 节点注册表取上游编码节点类。

    延迟到执行期导入：本包加载时上游可能尚未注册（ComfyUI 按目录顺序加载）；
    而 ``execute`` 被调用时所有自定义节点都已加载完毕。
    """
    try:
        import nodes as comfy_nodes  # noqa: PLC0415 — 仅 ComfyUI 进程内存在
    except ImportError as exc:  # pragma: no cover - 只在脱离 ComfyUI 直接调用时发生
        raise RuntimeError(
            "MiniMaxH3-Refs24 必须在 ComfyUI 进程内运行：找不到 ComfyUI 的 nodes 模块。"
        ) from exc

    mapping = getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", {})
    cls = mapping.get(UPSTREAM_NODE_KEY)
    if cls is None:  # pragma: no cover - 安装缺失时才会触发
        raise RuntimeError(
            f"未找到上游节点 {UPSTREAM_NODE_KEY}。请确认 custom_nodes/ComfyUI-JZL-MiniMax-H3 "
            "已安装且加载成功；本包只借上游编码实现，不复制一份（ADR 0017）。"
        )
    return cls


class MiniMaxH3Refs24BusOut:
    """24 图槽参考总线（打包）：33 个固定接口 → 1 条总线。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            # 顺序即槽位号：ref_image_0..23，随后视频/音轨/音频（与上游一致）。
            "optional": {key: (_bus_type(key),) for key in BUS_KEYS},
        }

    RETURN_TYPES = (BUS_TYPE,)
    RETURN_NAMES = ("refs24参考总线",)
    FUNCTION = "pack"
    CATEGORY = "MiniMaxH3-Refs24"

    def pack(self, **kwargs):
        # 未接线的槽位保留 None：下游解包后由编码节点跳过（与上游行为一致）。
        return ({key: kwargs.get(key) for key in BUS_KEYS},)


class MiniMaxH3Refs24BusIn:
    """24 图槽参考总线（解包）：1 条总线 → 33 个固定接口。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"bus": (BUS_TYPE, {"force_input": True})}}

    RETURN_TYPES = tuple(_bus_type(key) for key in BUS_KEYS)
    RETURN_NAMES = BUS_KEYS
    FUNCTION = "unpack"
    CATEGORY = "MiniMaxH3-Refs24"

    def unpack(self, bus):
        # 防御旧工作流/手改总线：缺键按 None 处理，不伪造端口。
        if not isinstance(bus, dict):
            bus = {}
        return tuple(bus.get(key) for key in BUS_KEYS)


class MiniMaxH3Refs24ReferenceToVideo:
    """24 图槽版参考编码：参数与上游 ``JZL_MiniMaxH3ReferenceToVideo`` 对齐。

    端口顺序刻意与 V2 的固定端口版一致（clip/vae/audio_vae → 图 → 视频 →
    视频音轨 → 独立音频 → 提示词/尺寸/长度/参考图尺寸/放大），这样 V3 工作流
    只需扩图槽，其余连线与参数不用重新理解。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "vae": ("VAE",),
                "audio_vae": ("VAE",),
                "prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
                # 上限与上游一致：MAX_RESOLUTION 在本仓不引用，写死同值 16384。
                "width": ("INT", {"default": 1344, "min": 32, "max": 16384, "step": 32}),
                "height": ("INT", {"default": 768, "min": 32, "max": 16384, "step": 32}),
                "length": (
                    "INT",
                    {"default": 124, "min": 5, "max": 3600, "step": 17},
                ),
                "ref_image_size": (["match", "max"], {"default": "match"}),
                "ref_scale": (
                    "FLOAT",
                    {"default": 1.0, "min": 1.0, "max": 5.0, "step": 0.1},
                ),
            },
            "optional": {key: (_bus_type(key),) for key in BUS_KEYS},
        }

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "LATENT")
    FUNCTION = "execute"
    CATEGORY = "MiniMaxH3-Refs24"

    def execute(
        self,
        clip,
        vae,
        audio_vae,
        prompt,
        width,
        height,
        length,
        ref_image_size="match",
        ref_scale=1.0,
        **kwargs,
    ):
        # 只做「把固定端口装回上游本就接受的 dict」，编码细节全部交给上游。
        ref_images = {
            key: kwargs[key] for key in IMAGE_KEYS if kwargs.get(key) is not None
        }
        ref_videos = {
            key: kwargs[key] for key in VIDEO_KEYS if kwargs.get(key) is not None
        }
        ref_video_audios = {
            key: kwargs[key] for key in VIDEO_AUDIO_KEYS if kwargs.get(key) is not None
        }
        ref_audios = {
            key: kwargs[key] for key in AUDIO_KEYS if kwargs.get(key) is not None
        }

        upstream = _upstream_encode_class()
        result = upstream.execute(
            clip=clip,
            vae=vae,
            audio_vae=audio_vae,
            prompt=prompt,
            width=width,
            height=height,
            length=length,
            ref_image_size=ref_image_size,
            ref_scale=ref_scale,
            ref_images=ref_images,
            ref_videos=ref_videos,
            ref_video_audios=ref_video_audios,
            ref_audios=ref_audios,
        )
        # 上游是 V3 节点，返回 io.NodeOutput；V1 节点需要普通元组。
        return tuple(getattr(result, "args", result))
