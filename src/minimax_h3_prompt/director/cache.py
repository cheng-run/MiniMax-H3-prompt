"""导演台段缓存定位（issue #105）：目录名、帧文件、实际帧数。

**只认插件的磁盘布局**，不发明第二套（设计 §3.5）。这里的每条规则都对应插件源码，
插件升级要重核：

- 缓存根 ``<ComfyUI output>/minimax_seg_cache/``（``segment_cache.py:142``）；
- 目录名 ``<缓存名>_<节点id>``，缓存名空时只有节点 id（``resolve_segment_cache_key``
  ``:180-196``）；
- 缓存名归一 ``normalize_cache_name``（``:155-177``）；
- 帧文件 ``seg_{idx:04d}.frames.mkv``，``idx`` **0 基**（``_frames_path`` ``:461-465``
  与 ``lib/frames_ffv1.py:30`` 的 ``FRAMES_SUFFIX``）。

**编码口径是硬前提**：插件控件 ``cache_frames_codec`` 默认 ``raw``，磁盘上是
``seg_XXXX.pt``（torch uint8）。本项目环境没有 torch，读不了；所以遇到 ``.pt``
一律**明确报错**并给处置，不许静默当 0 帧或跳过（硬约束第 17 条）。

根目录**不写死在模块里**：``comfy_output_root`` 是用户配置（``config/agent.yaml`` 的
``director.comfy_output_root``，``H3_COMFY_OUTPUT_ROOT`` 覆盖），由调用方传进来。
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2

from ..tools.frame_match import read_frames_gray, video_meta

# 插件常量（``segment_cache.py:141-143``）：目录名与截断长度是同一条契约，抄字面
# 就等于两份会各自漂的口径，注释标明来源版本（HEAD ``4c6245e``）。
CACHE_ROOT_DIRNAME = "minimax_seg_cache"
MAX_CACHE_NAME_LEN = 64
# ``lib/frames_ffv1.py:30``：终稿帧的后缀。
FRAMES_SUFFIX = ".frames.mkv"
# ``_legacy_frames_path``（``segment_cache.py:466-470``）：默认 ``raw`` 编码的落盘形态。
RAW_FRAMES_SUFFIX = ".pt"

# Windows 非法字符与不可见控制符（``segment_cache.py:146``）。
_INVALID_DIR_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Windows 保留设备名（``segment_cache.py:148-151``）：以它们命名的目录即使不报错，
# 在部分 API 上会被重定向到设备，属于「建得出来但读不到」的那类坑。
_RESERVED_DIR_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def normalize_cache_name(raw: object) -> str:
    """用户输入的缓存名 → 单个安全的路径片段（复刻插件 ``normalize_cache_name``）。

    全部非法/空 → 返回 ``""``，调用方回落「只用节点 id」的旧布局。**绝不抛**：
    这是个清洗口，抛异常等于让一个空格键把整条陪跑打断。
    """
    try:
        text = str(raw or "").strip()
    except Exception:  # noqa: BLE001 - 清洗口不抛，理由见 docstring
        return ""
    if not text:
        return ""
    text = _INVALID_DIR_CHARS_RE.sub("_", text)
    # 空白、点与连续下划线折成一个：``..`` / ``...`` 不能在目录名里活下来（越界片段）。
    text = re.sub(r"[\s._]+", "_", text)
    text = text.strip("_")
    if not text:
        return ""
    # 点已被全部折成下划线，所以这里 split(".") 只剩一段；照抄插件是为了将来同步。
    stem = text.split(".")[0].upper()
    if stem in _RESERVED_DIR_NAMES:
        text = f"_{text}"
    return text[:MAX_CACHE_NAME_LEN]


def resolve_cache_key(cache_name: object, node_id: object) -> str:
    """缓存目录名：``<缓存名>_<节点id>``；缓存名空 → 只有节点 id（插件同形）。"""
    try:
        node = str(node_id or "").strip()
    except Exception:  # noqa: BLE001 - 与 normalize_cache_name 同一纪律
        node = ""
    name = normalize_cache_name(cache_name)
    if name and node:
        return f"{name}_{node}"
    return node or name


def cache_dir(comfy_output_root: str | Path, cache_name: object, node_id: object) -> Path:
    """一个缓存键的绝对目录（**不**创建目录：读缓存是只读动作）。

    键为空（既没缓存名也没节点 id）时**报错**：拼出来的路径会落在缓存根自己身上，
    后续数帧/删除就有可能在根目录上乱扫（插件 ``_sanitize_dir_token`` 同一防线）。
    """
    key = resolve_cache_key(cache_name, node_id)
    if not key or key in {".", ".."} or "/" in key or "\\" in key:
        raise ValueError(f"段缓存键不安全或为空：{key!r}（缓存名与节点 id 至少要有一个）")
    return Path(comfy_output_root) / CACHE_ROOT_DIRNAME / key


def frames_path(cache_directory: str | Path, segment: int) -> Path:
    """第 ``segment`` 段（**1 起**，与 plan.json / 账本同号）的帧文件。

    0 基文件名的换算**只住这里**：调用方一律用段号，谁也不要再写一次 ``segment - 1``
    （两套坐标混用是插件勘察反复警告的现场）。
    """
    if isinstance(segment, bool) or not isinstance(segment, int) or segment < 1:
        raise ValueError(f"段号必须是 1 起的整数，实际 {segment!r}")
    return Path(cache_directory) / f"seg_{segment - 1:04d}{FRAMES_SUFFIX}"


def raw_frames_path(cache_directory: str | Path, segment: int) -> Path:
    """同段的 ``raw`` 编码帧文件（``seg_XXXX.pt``）——存在即说明编码口径不对。"""
    if isinstance(segment, bool) or not isinstance(segment, int) or segment < 1:
        raise ValueError(f"段号必须是 1 起的整数，实际 {segment!r}")
    return Path(cache_directory) / f"seg_{segment - 1:04d}{RAW_FRAMES_SUFFIX}"


def cached_segments(cache_directory: str | Path) -> tuple[int, ...]:
    """目录里**已有终稿帧**的段号（1 基，升序）。

    只认 ``seg_NNNN.frames.mkv``：一采的 ``seg_NNNN.pre.frames.mkv`` 是中间态、
    ``.pt`` 是本项目读不了的 raw 编码——两者算进「这段跑完了」都会让可证层放行
    一段没有可用产物的段。
    """
    directory = Path(cache_directory)
    if not directory.is_dir():
        # 目录不在＝用户在导演台里还没跑过任何段。这是正常现场，不是错误。
        return ()
    numbers: list[int] = []
    for path in directory.iterdir():
        matched = re.fullmatch(rf"seg_(\d{{4}}){re.escape(FRAMES_SUFFIX)}", path.name)
        if matched and path.is_file():
            numbers.append(int(matched.group(1)) + 1)  # 0 基文件名 → 1 基段号
    return tuple(sorted(numbers))


def actual_frames(frames_file: str | Path) -> int:
    """数一段帧文件的实际帧数；读不出**报错**，绝不返回 0 冒充。

    两条路径（设计 §3.5）：先问容器头 ``CAP_PROP_FRAME_COUNT``（``video_meta``），
    读到 ≤0 时**逐帧解码**计数。为什么不能只信头：matroska 里 FFV1 常常没有
    ``nb_frames``，cv2 会给出 0 或估算值；插件的 ``_ffprobe_count_frames`` 也是
    ``-count_frames`` 真解码一遍才敢报数（``lib/video_io.py:150-179``）。

    为什么读不出要抛而不降级：0 帧与「解码器不认这个文件」在可证层是两种完全不同的
    结论——前者会判「该跑未跑」，后者是环境问题。混成 0 会把后者藏起来。
    """
    path = Path(frames_file)
    if not path.is_file():
        raise ValueError(f"段缓存帧文件不存在：{path}（这一段还没在导演台里跑完）")
    meta = video_meta(path)
    frames = int(meta.get("frames", 0) or 0)
    if frames <= 0:
        frames = len(read_frames_gray(path))
    if frames <= 0:
        raise ValueError(
            f"读不出段缓存的帧数：{path}（容器头为 0、逐帧解码也拿到 0 帧；"
            "文件可能被截断，或不是 FFV1 无损 mkv）")
    return frames


def actual_frames_for_segment(cache_directory: str | Path, segment: int) -> int:
    """第 ``segment`` 段（1 起）的实际帧数；``.pt`` 缓存**报错并给处置**。

    遇到 ``.pt`` 不许降级：默认 ``cache_frames_codec="raw"`` 时磁盘上就是它，而本项目
    环境没有 torch（`.venv` 只有 cv2 / imageio_ffmpeg），任何「尽力而为」的读法都是
    在编数据。处置只有一条：改插件的缓存编码后重跑该段。
    """
    directory = Path(cache_directory)
    path = frames_path(directory, segment)
    if not path.is_file():
        raw = raw_frames_path(directory, segment)
        if raw.is_file():
            raise ValueError(
                f"第 {segment} 段的缓存是 raw 编码（{raw.name}，torch 张量），"
                "本项目没有 torch，读不出帧数。\n"
                "处置：把导演台节点的 cache_frames_codec 切成 ffv1，"
                "然后重跑该段（ffv1 会写出 "
                f"{path.name}）。")
        raise ValueError(
            f"第 {segment} 段的缓存帧文件不存在：{path}"
            "（这一段还没在导演台里跑完，或段号勾错了）")
    return actual_frames(path)


def export_last_frame(frames_file: str | Path, out_path: str | Path) -> Path:
    """把一段缓存的**最后一帧**导出成 png（#107 尾帧变换提示词的起点画面）。

    为什么这一段在 cache.py：起点画面是「上一段缓存的末帧」，取哪一帧属于段缓存的
    读取口径（设计 §3.3「输入是上一段缓存末帧」）；#107 只负责拿这张图去写词，
    不该自己再开一条视频解码路径（那会长出第二份「怎么读缓存」）。

    容器头不可信时（``CAP_PROP_FRAME_COUNT`` 报 0）整段遍历取最后一帧——与
    ``actual_frames`` 同一套怀疑：matroska 里 FFV1 常没有 ``nb_frames``。
    """
    path = Path(frames_file)
    if not path.is_file():
        raise ValueError(
            f"取不到上一段缓存的末帧：{path} 不存在（尾帧变换以它为本段起点画面）")
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"取不到上一段缓存的末帧：{path} 打不开（文件可能被截断）")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frame = None
    if total > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, total - 1)
        ok, candidate = cap.read()
        if ok:
            frame = candidate
    if frame is None:  # 头部不可信：老实解一遍
        while True:
            ok, candidate = cap.read()
            if not ok:
                break
            frame = candidate
    cap.release()
    if frame is None:
        raise ValueError(f"取不到上一段缓存的末帧：{path} 一帧都解不出来")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), frame):
        raise ValueError(f"末帧写不出来：{out}（目录不可写或扩展名不支持）")
    return out
