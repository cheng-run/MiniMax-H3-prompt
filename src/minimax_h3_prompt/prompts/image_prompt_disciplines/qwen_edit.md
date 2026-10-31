## 图生图编辑提示词纪律（Qwen-Image 2.1）

本档是 Qwen-Image 2.1 **参考图编辑**的写词口径，与 `qwen.md` 那份文生图口径**分开用**：文生图要求「写完整画面」，塞进编辑请求会让模型把参考图当草稿重画，人物、服装、陈设全部漂移（2026-10-31 尾帧实跑事故）。官方单图编辑示例的写法是「从输入图生成结果 + 写明机位 + 保留句」（模型卡：`Create a photorealistic photograph of ... from the input sketch ... captured in full side profile with the camera at water level`）。

1. **正文用英文**：一整段自然语言，句与句衔成段，不写标签串。
2. **第一句绑定输入图并写终点机位**：用 `from the input image` 指明结果来自参考图；机位写成静态结果（`with the camera now at an eye-level medium-wide view ...`），不写运动过程。
3. **只写变了的东西**：机位与取景、主体位置与姿态、哪些物体出画、哪些物体进画。没变的内容不重述。
4. **保留写成保留句**：身份特征（脸、发型、眼镜、服装、肩章、徽章、腰带）与光源方向写成 `Keep ... unchanged`，逐字点名；不要列成整幅画面的清单。
5. **空间关系自洽**：说了人物走到街角，就不能再说近景的车还停在身后；被镜头带出画面的物体写成 `now behind them and out of frame`。
6. **不写视频语言**：不写声音、脚步声、时间码、剪辑与镜头运动过程。
7. **不写负面与权重**：这一档是 CFG=1，负向词是空动作；要排除的东西改用正向可见描述。
8. **不写质量套话**：`masterpiece`、`best quality`、`8K` 之类。
