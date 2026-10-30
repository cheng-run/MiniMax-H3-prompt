"""合成素材 fixture：不依赖真实 ComfyUI 输出，秒级可跑。

另外集中放**机器专属路径**——真实素材在仓库外（`output/` 已 gitignore），
换机器时需要覆盖。
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

import cv2
import numpy as np
import pytest

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


def _refuse_network(*args, **kwargs):
    """把真实的 socket 建连变成一条指名道姓的失败。

    默认拒绝而不是默认放行，是因为这类用例的危险恰恰在于**它照样能过**：本机 `.env`
    里躺真 key、真网关可达时它就是绿的，换到干净检出或别人机器上才炸（#33/#56 都撞过
    「只在本机能过」）。默认拒绝让漏打桩的那条在**写下的当天**就变红，并直接点名目标。
    """
    target = next((a for a in args if isinstance(a, tuple)), kwargs.get("address"))
    raise RuntimeError(
        "测试禁止真实网络连接（retro 2026-10-30）："
        f"目标 {target!r}。请桩掉客户端，确需联网的用例加 @pytest.mark.allow_network。"
    )


@pytest.fixture(autouse=True)
def no_real_network(request, monkeypatch):
    """所有用例默认断开真实网络（retro 候选 2 落到守卫）。

    这条规矩原本只写在 ``CODING_STANDARDS.md``（「会真建 LLM 客户端的用例一律打桩」），
    靠人记：漏打桩的用例不会报错，只会在换机器时挂掉，因此是判断题里代价最高的一类。
    做成 autouse 守卫后它变成确定性失败。

    只挡**建连**、不挡建客户端对象：``create_agent`` / ``ChatOpenAI(...)`` 本身不连网，
    真正出网在第一次请求，挡在那里误报最少（同款判断见 #33 复审）。
    """
    if request.node.get_closest_marker("allow_network") is not None:
        return
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
