"""flicker_metric 冒烟测试：固化自 output/_inspect/（issue #11 物证量尺）。

合成一段已知亮度轨迹的视频，验证去趋势量尺能区分「亮度斜坡」（合法慢变化，
不应计入 flicker_std）与「帧间闪烁」（应计入），防止 cv2/numpy 升级后量尺漂移。
"""
import cv2
import numpy as np
import pytest

from tools.flicker_metric import metrics, moving_average

W, H, FPS, N = 64, 64, 24, 48


def _write_video(path, frames):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    assert vw.isOpened()
    for f in frames:
        vw.write(f)
    vw.release()


def test_moving_average_removes_linear_ramp():
    x = np.linspace(0, 100, N, dtype=np.float32)
    resid = x - moving_average(x, 9)
    # edge 填充使序列两端各留 ~2.4 的边界残差（量尺的已知行为）；内部应近乎为零
    assert float(np.abs(resid[4:-4]).max()) < 1e-3
    assert float(np.abs(resid).max()) < 3.0


def _gray_frame(value):
    return np.full((H, W, 3), value, dtype=np.uint8)


def test_metrics_distinguishes_flicker_from_smooth_ramp(tmp_path):
    # 斜坡片：亮度每帧 +2（合法慢变化，去趋势后应几乎无残差）
    ramp = tmp_path / "ramp.mp4"
    _write_video(ramp, [_gray_frame(min(255, 40 + 2 * i)) for i in range(N)])
    # 闪烁片：亮度在 120/60 之间逐帧跳变（坏段形态）
    flicker = tmp_path / "flicker.mp4"
    _write_video(flicker, [_gray_frame(120 if i % 2 else 60) for i in range(N)])

    m_ramp = metrics(str(ramp))
    m_flicker = metrics(str(flicker))
    # 斜坡片去趋势后仅剩边界效应（~0.6）；闪烁片 >20，两者隔一个数量级以上
    assert m_ramp["flicker_std"] < 1.0
    assert m_flicker["flicker_std"] > 20.0
    assert m_flicker["flicker_std"] > 10 * m_ramp["flicker_std"]


@pytest.mark.parametrize("win", [0, 1])
def test_moving_average_trivial_windows(win):
    x = np.arange(10, dtype=np.float32)
    assert np.array_equal(moving_average(x, win), x)
