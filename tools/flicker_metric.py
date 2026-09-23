"""诊断量尺：把「闪动」变成可比较的标量。诊断工具，不属于 src/ 产品代码。

分离两类时间信号：
- 相机/主体的正常运动 -> 帧间差（diff_mean），会被合法快摇镜抬高，只作参考
- 曝光/纹理闪烁 -> 亮度序列去掉慢趋势后的残差 std（flicker_std），
  以及开局 0.5s 相对第 0 帧的漂移（opening_hold），后者直接检验
  「模型是否被文本逼着立刻离开输入帧」

实测基线与噪声底见 issue #11（2026-09-22，736×1280，24fps，I2VA，MiniMax H3）：
稳 0.09–0.17 / 坏 3.6–4.9；seed 噪声底 ~6%，只信 >2× 的效应。
物证 arm 文本固化在 tests/fixtures/flicker_arms/。

用法（仓库根，venv python）：
    ./.venv/Scripts/python.exe tools/flicker_metric.py <a.mp4> <b.mp4> ...
"""
import sys

import cv2
import numpy as np


def load_gray(path, w=160, h=284):
    cap = cv2.VideoCapture(path)
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (w, h)).astype(np.float32))
    cap.release()
    if not out:
        raise SystemExit("no frames: %s" % path)
    return np.stack(out)


def moving_average(x, win):
    if win <= 1:
        return x.copy()
    pad = win // 2
    xp = np.pad(x, pad, mode="edge")
    k = np.ones(win) / win
    return np.convolve(xp, k, mode="valid")[: len(x)]


def metrics(path, trend_win=9):
    a = load_gray(path)
    n = len(a)
    bright = a.mean(axis=(1, 2))
    resid = bright - moving_average(bright, trend_win)
    d = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    # 开局漂移：开局 0.5s（24fps -> 12 帧）相对第 0 帧的整体偏离
    k = min(12, n - 1)
    opening_hold = float(np.abs(a[1:k + 1] - a[0]).mean()) if k > 0 else 0.0
    return {
        "path": path,
        "frames": n,
        "dur_s": round(n / 24.0, 2),
        "flicker_std": round(float(resid.std()), 3),      # 去趋势后的闪烁强度
        "bright_range": round(float(bright.max() - bright.min()), 1),
        "opening_hold": round(opening_hold, 2),           # 开局是否守住输入帧
        "diff_mean": round(float(d.mean()), 2),
        "diff_max": round(float(d.max()), 2),
    }


if __name__ == "__main__":
    rows = [metrics(p) for p in sys.argv[1:]]
    keys = ["dur_s", "flicker_std", "opening_hold", "diff_mean", "diff_max", "bright_range"]
    print("%-34s %s" % ("video", "  ".join("%13s" % k for k in keys)))
    for r in rows:
        name = r["path"].split("/")[-1]
        print("%-34s %s" % (name[:34], "  ".join("%13s" % r[k] for k in keys)))
