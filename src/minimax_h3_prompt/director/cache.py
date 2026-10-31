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

# 一段的**全部**缓存副产物后缀（0 基文件名 = ``seg_{idx:04d}`` + 后缀）。
# 清单真源＝插件勘察 §5.1 磁盘布局全表（``segment_cache.py`` HEAD ``4c6245e``）。
# 为什么这份清单必须住在这里：级联重跑（#106）的语义是「这段从没跑过」，删漏一个后缀
# 就等于留一条能让「重跑」无声失效的命中路径（勘察 §四 第 22 条：缓存指纹不含父段
# 身份，插件永不自行失效）。
# 与帧后缀无关的那四个固定副产物（指纹 / AV latent / 音频 / 交接）。
_SEGMENT_CACHE_FIXED_SUFFIXES = (".meta.json", ".av.pt", ".audio.pt", ".handoff.json")
# 一采那一整套：名字是在终稿名里插一段 ``.pre``（``seg_0000.pre.frames.mkv``）。
# 为什么要删一采：插件的 ``confirm_first_pass`` 命中一采缓存就跳过一采、只跑二采，
# 留着一采等于让重跑在**改段之前**的采样条件下开工（票面「一采为什么必删」）。
FIRST_PASS_MARKER = ".pre"
# 只属于一采的一个后缀：SelfLift 低分辨率 carry（勘察 §5.1 表内单独一行）。
FIRST_PASS_ONLY_SUFFIXES = (".low.pt",)

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


def _require_safe_key(key: str) -> str:
    """缓存目录名（键）的安全判据——``cache_dir`` 与 ``validate_node_id`` 共用的唯一一份。

    空键、``.``/``..`` 与含路径分隔符的键会让拼出的目录跳出缓存根（或被 Windows
    重定向）；**判据只住这里**，向导别再写一份「字符白名单」——两份口径一旦分家，
    用户会在答完问题、进到陪跑那一刻才炸（复审 H3 的 ``..`` 就是这种形态）。
    """
    if not key or key in {".", ".."} or "/" in key or "\\" in key:
        raise ValueError(f"段缓存键不安全或为空：{key!r}（缓存名与节点 id 至少要有一个）")
    return key


def validate_node_id(node_id: object) -> str:
    """导演台节点的 id 是否能用（**唯一真源**）：向导问完就地校验，别等陪跑才炸。

    为什么判据是「和 ``cache_dir`` 同一条」而不是另写一套字符白名单：节点 id 原样
    拼进缓存目录名（插件不规范化它，``resolve_segment_cache_key`` 只对缓存名归一），
    所以「能不能当目录名」正是 ``cache_dir`` 的那条判据。此前向导自己写
    ``isalnum() or "_.-"``：``..``/``.`` 全在里面，缓存名非空时拼成 ``名字_..``
    还能活，缓存名为空时直接拼成 ``..``——用户答完三道题、进陪跑才在 ``cache_dir``
    报错（复审 H3）。

    返回去空白后的 id；非法/为空抛 ``ValueError``（调用方翻成用户能读的一句提示）。
    """
    try:
        node = str(node_id or "").strip()
    except Exception:  # noqa: BLE001 - 与 normalize_cache_name 同一纪律：清洗口不裸抛
        node = ""
    # ComfyUI 节点标题栏显示的是 `#365`，而插件拿到的 ``unique_id`` 是 `365`。用户照抄
    # 标题就会多一个 `#`，我们拼的目录名与插件写盘的不是同一个——陪跑数帧时才发现
    # 「目录不存在」（2026-10-30 真实会话里存的正是 `#365`）。剥前导 `#` 是归一
    # **展示符号**，不动用户真正的 id。
    if node.startswith("#"):
        node = node[1:].strip()
    if not node:
        raise ValueError(
            "节点 id 不能为空：段缓存目录名是「缓存名_节点id」，缺了这段就找不到缓存")
    return _require_safe_key(node)


def cache_dir(comfy_output_root: str | Path, cache_name: object, node_id: object) -> Path:
    """一个缓存键的绝对目录（**不**创建目录：读缓存是只读动作）。

    键为空（既没缓存名也没节点 id）时**报错**：拼出来的路径会落在缓存根自己身上，
    后续数帧/删除就有可能在根目录上乱扫（插件 ``_sanitize_dir_token`` 同一防线）。
    """
    key = _require_safe_key(resolve_cache_key(cache_name, node_id))
    return Path(comfy_output_root) / CACHE_ROOT_DIRNAME / key


def _segment_stem(segment: object) -> str:
    """段号（1 起）→ 插件文件名词干 ``seg_NNNN``（0 基）。

    0 基换算**只住这里**：调用方一律用段号，谁也不要再写一次 ``segment - 1``
    （两套坐标混用是插件勘察反复警告的现场）。
    """
    if isinstance(segment, bool) or not isinstance(segment, int) or segment < 1:
        raise ValueError(f"段号必须是 1 起的整数，实际 {segment!r}")
    return f"seg_{segment - 1:04d}"


def frames_path(cache_directory: str | Path, segment: int) -> Path:
    """第 ``segment`` 段（**1 起**，与 plan.json / 账本同号）的帧文件。"""
    return Path(cache_directory) / f"{_segment_stem(segment)}{FRAMES_SUFFIX}"


def raw_frames_path(cache_directory: str | Path, segment: int) -> Path:
    """同段的 ``raw`` 编码帧文件（``seg_XXXX.pt``）——存在即说明编码口径不对。"""
    return Path(cache_directory) / f"{_segment_stem(segment)}{RAW_FRAMES_SUFFIX}"


def segment_cache_suffixes() -> tuple[str, ...]:
    """一段的全部副产物后缀：终稿 6 个 ＋ 一采 7 个（含 ``.pre.low.pt``）。

    **调用时现算**而不是做成模块级元组：元组在 import 那一刻就把 ``FRAMES_SUFFIX`` 的
    值烤进去了，此后改常量清单不跟着变——守卫测试会永不为红（本仓
    「守卫形态要从常量派生」的教训）。价钱是每次几十个字符串，可忽略。
    """
    final = (FRAMES_SUFFIX, RAW_FRAMES_SUFFIX) + _SEGMENT_CACHE_FIXED_SUFFIXES
    first_pass = final + FIRST_PASS_ONLY_SUFFIXES
    return final + tuple(f"{FIRST_PASS_MARKER}{suffix}" for suffix in first_pass)


def segment_cache_files(cache_directory: str | Path, segment: int) -> tuple[Path, ...]:
    """第 ``segment`` 段（1 起）的**全部**缓存副产物路径（终稿 6 个 ＋ 一采 7 个）。

    清单按 ``segment_cache_suffixes()`` 派生，不另抄一份字面：插件升级只改常量。
    """
    stem = _segment_stem(segment)
    directory = Path(cache_directory)
    return tuple(directory / f"{stem}{suffix}" for suffix in segment_cache_suffixes())


def assert_files_inside(root: str | Path, paths, *, what: str) -> None:
    """先**全量**校验每个路径解析后仍在 ``root`` 之下，越界就报错（不碰磁盘）。

    为什么单独有这一步：删到一半再抛会留下「半删」状态，调用方（级联）没法判断到底
    该不该重跑。越界的现实来源是目录里的符号链接指向别处（用户或插件留下的），路径
    本身由固定名字拼成。

    ``what`` 是给用户看的根名字（「缓存目录」／「会话目录」）：两个调用方各有各的根，
    报错要能直接指向该核的那一项。
    """
    base = Path(root).resolve()
    for path in paths:
        try:
            Path(path).resolve().relative_to(base)
        except ValueError:
            raise ValueError(
                f"拒绝删除{what}之外的文件：{path}"
                f"（解析后不在 {base} 之下；路径可能被符号链接引到别处）"
            ) from None


def delete_files_under(root: str | Path, paths, *, what: str) -> tuple[Path, ...]:
    """删 ``root`` 之下的一批文件，返回**实际删掉**的路径。

    越界先报错、且一个都不删（见 ``assert_files_inside``）；文件本来就不在不算错——
    级联会对整条下游重复执行，幂等是硬要求。
    """
    paths = tuple(paths)
    assert_files_inside(root, paths, what=what)
    deleted: list[Path] = []
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            candidate.unlink()
            deleted.append(candidate)
    return tuple(deleted)


def delete_segment_cache(cache_directory: str | Path, segment: int) -> tuple[Path, ...]:
    """删第 ``segment`` 段（1 起）的全部缓存副产物，返回**实际删掉**的路径（#106）。

    为什么删「整套」而不是只删帧文件：插件的一采命中、``meta.json`` 指纹命中都是独立
    的命中路径（勘察 §5.3），只删帧文件等于让重跑在残留条件下开工——票面点名的
    「静默接错」正是这个形态。

    为什么**不**删整个缓存目录：同目录里别的段的产物还在用（设计 §3.5）。

    删除纪律（校验先行、越界报错不删、幂等）与派生件删除共用 ``delete_files_under``：
    两处各写一遍就会对「报错时删没删」各执一词。
    """
    return delete_files_under(
        cache_directory,
        segment_cache_files(cache_directory, segment),
        what="缓存目录")


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
        # #112 起这一口也服务用户贴回来的成片视频，文案不能写死「段缓存」。
        raise ValueError(f"帧文件不存在：{path}（这一段还没跑完，或路径贴错了）")
    meta = video_meta(path)
    frames = int(meta.get("frames", 0) or 0)
    if frames <= 0:
        frames = len(read_frames_gray(path))
    if frames <= 0:
        raise ValueError(
            f"读不出帧数：{path}（容器头为 0、逐帧解码也拿到 0 帧；"
            "文件可能被截断，或不是能解码的视频）")
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
    """把一段视频/帧文件的**最后一帧**导出成 png（#107 尾帧变换提示词的起点画面）。

    为什么这一段在 cache.py：起点画面沿用「上一段的末帧」这份读取口径（设计 §3.3）；
    #114 起来源是账内上一段的成片视频，但这个函数不关心来源是谁——它只做「打开、
    取末帧、写 png」，判据留在调用方，避免这里长出第二份「起点画面该取哪条片」的口径。

    容器头不可信时（``CAP_PROP_FRAME_COUNT`` 报 0）整段遍历取最后一帧——与
    ``actual_frames`` 同一套怀疑：matroska 里 FFV1 常没有 ``nb_frames``。

    #114：异常文案不再叫「缓存」——同一口也服务成片视频，指错东西会让用户去查错文件。
    """
    path = Path(frames_file)
    if not path.is_file():
        raise ValueError(
            f"取不到末帧：{path} 不存在（尾帧变换以它为本段起点画面）")
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"取不到末帧：{path} 打不开（文件可能被截断）")
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
        raise ValueError(f"取不到末帧：{path} 一帧都解不出来")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), frame):
        raise ValueError(f"末帧写不出来：{out}（目录不可写或扩展名不支持）")
    return out
