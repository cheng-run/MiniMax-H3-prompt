"""合成素材 fixture：不依赖真实 ComfyUI 输出，秒级可跑。

另外集中放**机器专属路径**——真实素材在仓库外（`output/` 已 gitignore），
换机器时需要覆盖。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import cv2
import numpy as np
import pytest

# 运行日志默认**不落盘**（#239 / spec #235 §8.1）：``H3_LOG_DIR=""`` 是显式关闭开关，
# 全量用例因此不会往仓库 ``logs/`` 里塞文件（干净检出上那些是垃圾）。需要验证真落盘的
# 用例在自己的 tmp_path 上覆盖这个变量；要临时看真日志就手工 export 一个目录。
os.environ.setdefault("H3_LOG_DIR", "")

# 本机便利默认值（这台机器上 ComfyUI 的 H3 视频输出目录）。
# 换机器请设 H3_COMFY_OUTPUT；未设且该目录不存在时，真实素材模块整体 SKIP。
DEFAULT_COMFY_OUTPUT = Path(
    r"D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18")

# 真实旧会话样本目录（阶段 1 产物，仓库外；`output/` 已 gitignore）。
# 说话的是**用户机器**上的现场：清洁检出与换机器时整类用例 SKIP，不伪造样本。
DEFAULT_SESSION_SAMPLES = Path(r"D:/项目/python/MiniMax-H3-prompt/output/sessions")


def session_samples_dir() -> Path:
    """真实旧会话样本目录：``H3_SESSION_SAMPLES`` 优先，空字符串视为未设。

    统一入口的理由与 ``comfy_output_dir`` 同款（issue #77 复审 G）：机器专属路径
    散在几个测试文件里各写一份默认值，换机器时总有一处忘改、那条用例就悄悄空转。
    """
    return Path(os.environ.get("H3_SESSION_SAMPLES") or DEFAULT_SESSION_SAMPLES)


def comfy_output_dir() -> Path:
    """真实素材的输出目录：``H3_COMFY_OUTPUT`` 优先，空字符串视为未设。"""
    return Path(os.environ.get("H3_COMFY_OUTPUT") or DEFAULT_COMFY_OUTPUT)


def write_test_video(
    path: Path,
    *,
    frames: int = 24,
    fps: int = 24,
    width: int = 128,
    height: int = 72,
    base: tuple[int, int, int] = (30, 30, 30),
    moving: bool = True,
    step: int = 3,
    offset: int = 0,
) -> Path:
    """写一段小 mp4：纯色背景 + 一个逐帧移动的白色方块。

    moving=False 时整段完全静止（用于测 sanity check）。

    ``step`` / ``offset`` 用来构造**尾帧接力**的相邻片段：给第 k 段的
    ``offset`` 取 ``(k-1) * step * (frames - 1)``，它的首帧方块位置就
    恰好等于上一段的尾帧，从而"某帧等于某段尾帧"这件事在磁盘上真实成立。
    默认值 3 / 0 与历史行为完全一致。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    assert writer.isOpened(), f"VideoWriter 打不开 {path}"
    for i in range(frames):
        frame = np.full((height, width, 3), base, dtype=np.uint8)
        x = 8 + offset + (i * step if moving else 0)
        cv2.rectangle(frame, (x, 20), (x + 20, 40), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()
    return path


def write_h3_tier_video(path: Path, seconds: float, *, fps: int = 24) -> Path:
    """写一段**按 H3 实测帧数档**的真视频（736×1280），供可证层判档的用例用。

    与 ``write_test_video`` 的分工：那条是通用小视频（128×72，尺寸随便）；这一条必须
    **帧数落在 ``H3_TIER_FRAMES`` 档位上、尺寸是 H3 输出尺寸**——可证层
    （``preflight.check_segment_video``）按帧数档表判「交回来的是不是本段、用没用错时长
    档」，96 帧的「假 4s」落不进任何档位、只报「无法确证档位」，那两条 AC 就测不出来
    （issue #55 的用例 docstring 记着这个坑）。档位外的秒数（如 10/11s）按 ``fps`` 折算，
    落进「区间内但不在帧数表」的 warning 档。
    """
    from minimax_h3_prompt.tools.frame_match import video_meta
    from minimax_h3_prompt.tools.preflight import EXPECTED_VIDEO_SIZE, H3_TIER_FRAMES

    frames = H3_TIER_FRAMES.get(int(round(seconds)), int(round(seconds * fps)))
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                             EXPECTED_VIDEO_SIZE)
    assert writer.isOpened(), "测试环境缺 MJPG 编码器"
    image = np.full((EXPECTED_VIDEO_SIZE[1], EXPECTED_VIDEO_SIZE[0], 3), 128, np.uint8)
    for i in range(frames):
        image[:] = 128 + (i % 8)
        writer.write(image)
    writer.release()
    actual = video_meta(path).get("duration_s", 0.0)
    assert abs(actual - frames / fps) < 0.05, f"测试视频时长不可控：{actual}"
    return path


@pytest.fixture(autouse=True)
def no_real_network(request, monkeypatch):
    """默认禁止用例真实建连；确需联网的用例标 ``@pytest.mark.allow_network``。

    为什么必须有它（retro 2026-10-31）：``tests/`` 自 2026-10-30 起不入库，这份 conftest
    在搬出仓库时把守卫丢了——但 ``tests/test_network_guard.py`` 与 ``pyproject.toml`` 的
    marker 注释仍在按「守卫存在」写。后果有三层：

    1. 基线上两条守卫用例恒红，真正的新回归会被这 3 条红淹没（本次 #111 运行实测）；
    2. 任何忘了桩掉的用例都会**真的出网**：真实 LLM/读图调用既烧钱又拖慢全量；
    3. 「永不为红＝没有」：守卫不咬人时，依赖它的用例不会变红，问题被静默藏住。

    只拦「真正建连」这一层（``connect`` / ``create_connection``）：造视频走的是 ffmpeg
    子进程、读盘走文件系统，都不受影响。``connect_ex`` 一并拦，因为轮询式客户端常用它。

    **回环地址放行（retro 2026-11-09）**：Starlette 的 ``TestClient`` 会在本机回环开真实
    socket（127.0.0.1 随机端口）。原实现连回环也一并拦，逼得每个 FastAPI 用例整文件标
    ``allow_network``——而那把**外部**保护也一起关了，最该拦的地方反而不拦。现在只拦非回环
    地址：``TestClient``/进程内服务的回环连接照常，外部 LLM/网关仍被挡。
    """
    if request.node.get_closest_marker("allow_network") is not None:
        return

    def _is_loopback(address: object) -> bool:
        host = address[0] if isinstance(address, tuple) and address else address
        if not isinstance(host, str) or not host:
            return False
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host == "localhost"

    def _refuse() -> RuntimeError:
        return RuntimeError(
            "禁止真实网络连接：测试默认不得出网（回环地址已放行）。"
            "确需外部联网的用例请加 @pytest.mark.allow_network"
            "（见 pyproject.toml 的 markers 说明）。")

    original_create_connection = socket.create_connection
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _guarded_create_connection(address, *args, **kwargs):
        if not _is_loopback(address):
            raise _refuse()
        return original_create_connection(address, *args, **kwargs)

    def _guarded_connect(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise _refuse()
        return original_connect(self, address, *args, **kwargs)

    def _guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise _refuse()
        return original_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


@pytest.fixture(scope="session", autouse=True)
def isolate_host_env():
    """**默认**把本机 ``.env`` 从测试进程里摘掉（2026-11-12 retro）。

    背景：原先只有 ``H3_TEST_NO_ENV=1``（``check.sh clean``）才摘，于是同一个用例
    在本机 ``.env`` 有 key 时绿、在干净检出红——2026-11-12 实测一条缺 key 用例在
    工作树绿、拆回 main 后全量红，正是这个方向。现在反转为「默认隔离」：有真实
    环境需求时用 ``H3_TEST_USE_ENV=1`` 显式放行（与 ``no_real_network`` 的
    opt-in 同形），``H3_TEST_NO_ENV=1`` 保留为兼容别名、效果相同。

    做法：停掉 ``load_dotenv`` 并把环境里已加载的模型相关变量摘掉（保存后还原）。
    不碰用户的 ``.env`` 文件本身——只影响本进程。
    """
    allowed = os.getenv("H3_TEST_USE_ENV", "").strip().lower() in {"1", "true", "yes"}
    if allowed:
        yield
        return
    from minimax_h3_prompt import config as config_module

    original = config_module.load_dotenv
    config_module.load_dotenv = lambda *args, **kwargs: None
    saved = {
        name: os.environ.pop(name)
        for name in list(os.environ)
        if name.endswith(("_API_KEY", "_AUTH_TOKEN", "_BASE_URL", "_MODEL", "_MODEL_NAME"))
        or name == "LLM_PROVIDER"
    }
    try:
        yield
    finally:
        config_module.load_dotenv = original
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def no_run_log(monkeypatch):
    """默认关掉结构化运行日志（体检 P4）。

    它的默认落点是 ``stage_saver.base.parent/logs/``：多数用例没有把 ``stage_saver``
    指到临时目录，跑一次全量就会在仓里留下几百份 JSONL——那是「测试污染用户目录」
    的老形态。要验证日志本身的用例（``tests/test_run_log.py``）自己打开。
    """
    monkeypatch.setenv("H3_RUN_LOG", "0")


@pytest.fixture(autouse=True)
def no_interaction_log_leak():
    """用例之间不残留交互日志句柄（#171）。

    日志是全局单例：一条用例里 ``run_wizard`` 打开了临时路径上的文件，下一条用例
    不关就还指向那个 tmp 目录——Windows 上句柄悬着，写入也落到上一轮的目录里。
    """
    from minimax_h3_prompt.observability import interactions

    interactions.stop()
    yield
    interactions.stop()


@pytest.fixture(autouse=True)
def isolated_asset_library(tmp_path_factory, monkeypatch):
    """所有用例默认把素材库根指到临时目录（issue #85）。

    默认库根是用户机器上的真实目录（``D:\\素材库``）：不隔离的话，任何走生产入库
    路径的用例（向导 / CLI / brief 路径）都会把测试图写进真实素材库——收尾清不干净，
    而且这种污染**不会让任何断言变红**（「本机全绿」不能当反证的老教训）。
    要验证配置读取本身的用例自己传路径或覆盖 ``LIBRARY_CONFIG_PATH``。
    """
    root = tmp_path_factory.mktemp("asset-library")
    config = root.parent / f"{root.name}-library.yaml"
    config.write_text(f"root: '{root.as_posix()}'\n", encoding="utf-8")
    from minimax_h3_prompt import library_config

    monkeypatch.setattr(library_config, "LIBRARY_CONFIG_PATH", config)
    return root


@pytest.fixture
def tmp_video(tmp_path: Path):
    """返回一个可调用对象：make(name="a.mp4", **kwargs) -> Path"""
    def make(name: str = "clip.mp4", **kwargs) -> Path:
        return write_test_video(tmp_path / name, **kwargs)
    return make


@pytest.fixture
def h3_tier_video(tmp_path: Path):
    """返回一个可调用对象：make(seconds, name="seg.avi") -> Path（H3 档位帧数的真视频）。"""
    def make(seconds: float, name: str = "clip.avi") -> Path:
        return write_h3_tier_video(tmp_path / name, seconds)
    return make
