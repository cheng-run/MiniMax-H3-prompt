# MiniMax H3 量化方案调研（AMD ROCm / RX 9070 16GB 版）

调研日期：2026-09-24
目标硬件：**AMD Radeon RX 9070（RDNA4 / gfx1201），16 GB VRAM，Windows 11，32 GB 系统 RAM**
ComfyUI v0.31.0，启动参数 `--enable-dynamic-vram --disable-pinned-memory --fast-disk`

调研方法：HuggingFace 仓库 API 拉真实文件清单与字节数、comfy-kitchen / ComfyUI 一手源码与 PR、Comfy-Org 与 Comfy-Org/comfy-kitchen 的 issue、Comfy-Org/MiniMax-H3 的 HF discussion、以及本地 `D:\Comfyui\ComfyUI` 现有文件的逐字节比对。
无法验证的一律标注「**未经证实**」。低可信度来源单独标注。

---

## 0. 首要前提：MiniMax H3 真实存在，官方权重公开可下载

**真实存在，官方权重公开、未设 gate。**

| 项 | 值 |
|---|---|
| 官方仓库 | [`MiniMaxAI/MiniMax-H3`](https://huggingface.co/MiniMaxAI/MiniMax-H3) |
| ModelScope 镜像 | [modelscope.cn/models/MiniMax/MiniMax-H3](https://modelscope.cn/models/MiniMax/MiniMax-H3) |
| gated | `false`（无需申请） |
| license | `other` → `minimax-h3-community-license-agreement` |
| 创建 / 最后更新 | 2026-07-28 / 2026-08-13 |
| 下载量 / likes | 3,640,535 / 5,641 |
| ComfyUI 重打包 | [`Comfy-Org/MiniMax-H3`](https://huggingface.co/Comfy-Org/MiniMax-H3) — 21,820,807 下载 / 1,985 likes |

- 出处：https://huggingface.co/api/models/MiniMaxAI/MiniMax-H3
- 官方发布说明：https://www.minimax.io/news/minimax-h3-open-source
- **发布方是 MiniMax 官方（MiniMaxAI）**；`Comfy-Org/MiniMax-H3` 是 ComfyUI 官方的重打包仓库；其余均为第三方 repack。

**开源边界**：只开源 H3-Base，两个任务专用 checkpoint（`FL2VA`、`Ref2VA`），BF16。**Context-IR 与 Regenerate-2K 未开源 → 本地出不了真 2K，画布上限 768p 短边。**

**任务变体映射**：T2VA / I2VA / L2VA / FL2VA 全部由 **FL2VA** 承载（区别只在给不给首/尾帧）；Ref2VA 有独立 checkpoint。**没有独立的 I2VA / L2VA checkpoint。**

---

## 1. ROCm / AMD 上实际可用方案谱系

### 1.1 一手依据：comfy-kitchen 的后端与架构表

来源：[`Comfy-Org/comfy-kitchen` README](https://raw.githubusercontent.com/Comfy-Org/comfy-kitchen/main/README.md)

comfy-kitchen 有四个后端：`eager`（纯 PyTorch）、`cuda`（CUDA C 内核，仅 CUDA）、`hip`（HIP 内核）、`triton`。自动选择优先级：**`hip` → `cuda` → `triton` → `eager`**。

**GPU 架构表（官方原文）：**

| 代次 | gfx target | 矩阵核心 | 实际能跑什么 |
|---|---|---|---|
| **RDNA4** | **`gfx1200`, `gfx1201`** | **WMMA + fp8** | **全部 HIP 内核，"fp8 native"** |
| RDNA3.5 | `gfx1150`–`gfx1153` | WMMA，无 fp8 | 全部 HIP 内核；fp8 被加宽到 bf16 |
| RDNA3 | `gfx1100`–`gfx1103` | WMMA，无 fp8 | 全部 HIP 内核；fp8 被加宽到 bf16 |
| RDNA2 | `gfx1030`–`gfx1036` | 无 | 仅非 WMMA 内核（含 AWQ GEMV）；WMMA GEMM 退出 |

**`hip` 后端的算子覆盖（官方原文）：支持除 NVFP4 三件套（`quantize_nvfp4` / `dequantize_nvfp4` / `scaled_mm_nvfp4`）与 MXFP8 三件套以外的全部算子。**

官方补充：NVFP4 与 MXFP8 在 RDNA 上"stay on eager everywhere"，因为 RDNA 既无 fp4 WMMA 也无 microscaling 硬件。`COMFY_KITCHEN_DISABLE_HIP=1` 可把 hip 从调度中移除。

**RDNA4 是本世代 AMD 里唯一有硬件 fp8 的** —— 这是本次调研对你最重要的一条：`fp8_scaled` 在 gfx1201 上是**原生**的，而不是像 RDNA3 那样被加宽成 bf16。

### 1.2 一手依据：gfx1201 实机启动日志

来源：Comfy-Org/MiniMax-H3 discussion #17，用户 `doplxyz`，RX 9070 XT / gfx1201 / Linux / ROCm 7.2.4
https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/17

启动日志原文：

```
Native ops: int8_tensorwise, float8_e5m2, convrot_w4a4, float8_e4m3fn , emulated ops: nvfp4, mxfp8
```

**这是回答「AMD 上什么能跑」最直接的一条证据，且来自与目标机器同款 GPU。** 解读：

| 格式 | gfx1201 状态 | 对应文件后缀 |
|---|---|---|
| `int8_tensorwise` | **原生** | `int8_convrot` |
| `float8_e4m3fn` | **原生** | `fp8_scaled` |
| `float8_e5m2` | **原生** | （另一种 fp8 变体） |
| `convrot_w4a4` | **原生** | INT4 / W4A4 路线 |
| `nvfp4` | **模拟（emulated）** | `nvfp4_awq` |
| `mxfp8` | **模拟（emulated）** | — |

> 你提到的内核名 `quantize_int8_convrot_weight` 与 `convrot_w4a4_linear` 与 comfy-kitchen README 的能力矩阵一致（矩阵中 `TensorCoreConvRotW4A4Layout` 注册在 `convrot_w4a4` 算法名下），**内核名核对通过**。

### 1.3 ⚠ 需要修正的一条前提：NVFP4 在 AMD 上并非「不可用」

上游转述的前提是「NVFP4 是 NVIDIA Blackwell 专有格式，在 AMD 上不可用，所以项目把 text encoder 换成了 int4_convrot」。

**按一手证据，这个说法不准确：NVFP4 在 gfx1201 上是「模拟」而非「不可用」。** `doplxyz` 在同一张 RX 9070 XT 上**成功跑通了** `qwen3vl_32b_minimax_h3_nvfp4_awq` 这个 text encoder，其日志记载该文件 "loads as 14960.20 MB, full load: True"，且 "dequantizes per-op rather than expanding to bf16"。

所以从 nvfp4_awq 换到 int4_convrot 是一次**性能选择**（模拟路径每算子反量化，慢），不是被迫的兼容性选择。这个区别有意义：

- 若换 int4 是因为「nvfp4 跑不起来」→ 前提有误，值得重测 nvfp4 是否其实可用（但慢）。
- 若换 int4 是因为「nvfp4 模拟太慢」→ 前提成立，换得有道理，且 int4 走的是**原生** `convrot_w4a4` 内核，理论上比模拟的 nvfp4 快。

**结论：换 int4_convrot 的决定本身站得住，但理由应从「nvfp4 不可用」改写为「nvfp4 可用但走模拟路径」。**

### 1.4 完整可用性谱系（针对 gfx1201 / 16GB / 32GB RAM）

| 方案 | gfx1201 状态 | 16GB+32GB 可行性 | 备注 |
|---|---|---|---|
| **`int8_convrot`** | ✅ 原生 HIP | ✅ 当前在用 | Comfy-Org 的 `int8_tensorwise`；AMD 上最自然的首选 |
| **`fp8_scaled`** | ✅ **原生**（RDNA4 独有） | ⚠️ 可跑但有已知崩溃 bug | 见 §1.5 |
| **`convrot_w4a4`（INT4）** | ✅ 原生 | ✅ 体积最小 | 社区二次量化 |
| **GGUF（K-quant）** | ✅ 走 ComfyUI-GGUF，K-quant 无模拟开销 | ✅ | AMD HIP 指南推荐路线 |
| **`nvfp4_awq`** | ⚠️ **模拟**（能跑，慢） | ⚠️ 可跑但每算子反量化 | 实测已在 gfx1201 跑通 |
| **MXFP8** | ⚠️ 模拟 | — | — |
| **bf16 全量** | ✅ 原生 | ❌ 61.73 GiB 单文件 | RAM/显存都不够 |

**所以「int8_convrot 是不是唯一可行的」——不是。** 在 gfx1201 上有**三条原生路线**：`int8_convrot`、`fp8_scaled`、`convrot_w4a4`(INT4)，外加 GGUF。int8_convrot 之所以是默认，是因为它同时满足「原生 + 官方支持 + 体积可接受」。

**关于 Comfy-Org 那句 "prefer `int8_convrot`"**：原文是 "For diffusion models prefer `int8_convrot` **if you are able to use pytorch with cu130**." —— 这是**面向 NVIDIA cu130 的建议**。AMD 上没有 cu130（`quant_ops.py` 里 `if torch.version.cuda is None: ck.registry.disable("cuda")`，cuda 后端直接被禁用），走的是 hip 后端。**这条建议不适用于你的机器，不要把它当作 AMD 上的质量判据。**

### 1.5 已知的 ROCm 专属缺陷（与你的卡直接相关）

| 来源 | 内容 | 状态 |
|---|---|---|
| [comfy-kitchen issue #125](https://github.com/Comfy-Org/comfy-kitchen/issues/125) | gfx1201（R9700）+ comfy-kitchen 0.2.31：**HIP 后端 `dequantize_per_tensor_fp8` 硬段错误**（无 Python 异常，进程直接死），发生在 fp8 text encoder 首次前向。HIP **声称支持该内核**，不是回退路径；按目标架构重建后仍复现。**规避：`COMFY_KITCHEN_DISABLE_HIP=1`**（退回 eager，可正常完成） | **Open**（2026-08-21 提） |
| [comfy-kitchen issue #78](https://github.com/Comfy-Org/comfy-kitchen/issues/78) | gfx1201（RX 9070 XT）+ ComfyUI 0.28.0 + comfy-kitchen 0.2.22：INT8 ConvRot 三条路径表现分化 —— eager 稳但慢（65–80 s vs fp8 的 12.7–13.0 s）；**全局 Triton 硬崩**（"Memory access fault by GPU node-1" → "Fatal Python error: Aborted"）；ROCm 上强开 cuda 后端报 nanobind 绑定错误（`quantize_int8_rowwise_convrot64(): incompatible function arguments`）；**仅选择性调用 triton 的 `int8_linear` 既稳又快**（热跑 10.49 / 10.54 s） | **Closed**（2026-07-18 提），无维护者回复 |
| [ComfyUI PR #14869](https://github.com/Comfy-Org/ComfyUI/pull/14869) | "Only auto-enable the ROCm comfy-kitchen Triton backend on matrix-core GPUs"，liminfei-amd 提，comfyanonymous **2026-07-10 合并**（merge commit `1377a2f`）。修 #14868。PR #14862 原本"只要设了 `torch.version.hip` 且 Triton ≥ 3.7 就自动开 Triton"；本 PR 追加**架构门槛**：开 gfx11xx / gfx12xx / gfx9xx，关 gfx10xx（RDNA1/2 无矩阵核心，INT8 路径会挂死 GPU）。新增 `--disable-triton-backend`；NVIDIA 行为不变 | **Merged** |
| [ComfyUI PR #15334](https://github.com/Comfy-Org/ComfyUI/pull/15334) | "Support int8_convrot VAE for MiniMax-H3"，Kijai 提，**2026-08-06 合并**（merge commit `bbda836`）。仅改 `comfy/ldm/minimax/vae.py`。作者称"VAE quality looks fine"、约 **1.5× 加速** | **Merged** |

> **版本时间线很重要**：issue #78 的崩溃发生在 ComfyUI 0.28.0 / comfy-kitchen 0.2.22；issue #125 的段错误发生在 comfy-kitchen 0.2.31。你的 v0.31.0 比 #78 的环境新三个 minor 版本，**#78 描述的那批问题在你的版本上很可能已经被 PR #14869 一系修复**——这也与你「v0.30.0 卡 0%、v0.31.0 才通」的观察方向一致。

### 1.6 ⚠ Triton 默认开关存在版本冲突（未解决）

- PR #14869 说 ROCm 上**自动开** Triton（限矩阵核心架构，gfx1201 符合）。
- 但我实测拉取的 `comfy/master/comfy/quant_ops.py` 里写的是另一回事：**"Triton is an opt-in override, off by default on every platform."**，只在 `if args.enable_triton_backend and not args.disable_triton_backend:` 时才启用，否则 `ck.registry.disable("triton")`。

两者矛盾，可能是 master 快照与 PR 的时序差。**未经证实，不自行选边。** 但有个零成本的判定办法 —— 见 §4 的验证步骤：**你机器启动日志里的 `Native ops:` 一行就是最终事实**，它会按你的实际安装列出原生算子清单。把它贴出来即可确定 Triton 路径在你这里究竟是开是关。

### 1.7 ⚠ 一处与你的实机经验直接矛盾的第三方说法

来源：`localaimaster.com` 的 ROCm 排错文章
https://localaimaster.com/blog/comfyui-amd-rocm-image-generation

该文称：在 gfx1201（RX 9070 XT / Win11 / torch 2.9.1+rocm7.2.1）上 **`int8_convrot` 的 diffusion 模型会输出全黑图**，而同一量化格式的 text encoder 正常（用 2×2 的 UNET × text-encoder 矩阵隔离出来，只有 int8 UNET 那两行失败）；建议的修法是**换成 `fp8_scaled`**，并称该问题当时仍未解决。

**这条与两条独立证据冲突：**
1. 你自己的机器上 int8_convrot 是跑通的（v0.31.0）。
2. `doplxyz` 在 gfx1201 上用 `minimax_h3_fl2va_pruned_int8_convrot` 成功出片，日志显示 `int8_tensorwise` 为原生。

**我不自行选边。** 三种可能，需要你判断：(a) 该文早于 PR #15334（2026-08-06 合并）与 `--disable-smart-memory` 的发现，是已修复的旧状态；(b) 该文针对的是别的模型/工作流，被错误泛化到 H3；(c) 存在尚未定位的配置相关触发条件。**同时注意该来源是低可信度 SEO 站点，非一手。** 若你后续遇到黑图，第一件事是试 `--disable-smart-memory`（§3.1），而不是立刻换量化格式。

---

## 2. 同一 int8_convrot 方案内部的档位

### 2.1 diffusion 模型：**存在非剪枝的 int8_convrot**

这是本次调研对你最直接有用的发现。Comfy-Org 在 `int8_convrot` 这一格式下提供两档，**都不需要换量化方案**：

| 文件（`Comfy-Org/MiniMax-H3/diffusion_models/`） | 字节数 | GiB | 剪枝 | vs 你现状 |
|---|---|---|---|---|
| `minimax_h3_fl2va_int8_convrot.safetensors` | 34,038,892,334 | **31.70** | **否** | **+12.17 GiB** |
| `minimax_h3_ref2va_int8_convrot.safetensors` | 34,038,894,550 | **31.70** | **否** | **+12.17 GiB** |
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 20,970,379,616 | 19.53 | 是 | 你现有 |
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 20,970,379,616 | 19.53 | 是 | 你现有 |

同族更高精度的另两档（不再是 int8，仅列出以便比较）：

| 文件 | 字节数 | GiB | vs 你现状 |
|---|---|---|---|
| `minimax_h3_{fl2va,ref2va}_pruned_bf16.safetensors` | 40,225,724,176 | 37.46 | +17.93 GiB |
| `minimax_h3_{fl2va,ref2va}_bf16.safetensors` | 66,280,487,368 | 61.73 | +42.20 GiB |
| `minimax_h3_{fl2va,ref2va}_pruned_fp8_scaled.safetensors` | 20,958,205,608 | **19.52** | **−0.01 GiB（几乎同尺寸）** |

> **F2 与 R2 的两个 int8_convrot 字节数不同**（34,038,892,334 vs 34,038,894,550），说明是两个独立转换产物。F1 与 R1 的 pruned 版则完全一致。

### 2.2 VAE：有一个你还没用的、几乎免费的省显存档

| 文件（`vae/`） | 字节数 | GiB | 备注 |
|---|---|---|---|
| `minimax_h3_video_vae_fp16.safetensors` | 5,207,808,496 | 4.85 | 你现有 |
| `minimax_h3_video_vae_int8_convrot.safetensors` | 2,811,065,184 | **2.62** | PR #15334 新增，**省 2.23 GiB**；Kijai 称质量无问题、约 1.5× 加速 |
| `minimax_h3_audio_vae_fp32.safetensors` | 605,254,808 | 0.56 | 音频 VAE 必须 fp32（fp16 会导致音画不同步，社区口径**未经证实**） |

### 2.3 text encoder：int8 / int4 两档

| 文件 | 字节数 | GiB | 来源 |
|---|---|---|---|
| `text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors` | 51,506,295,256 | 47.97 | Comfy-Org 官方 |
| `text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 27,141,342,152 | 25.28 | Comfy-Org 官方 |
| `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15,687,142,551 | 14.61 | Comfy-Org 官方（AMD 上为**模拟**） |
| `qwen3vl_32b_minimax_h3_int4_convrot.safetensors` | 14,952,506,624 | 13.93 | **社区二次量化**，非官方（见 §2.4） |

### 2.4 本地文件溯源（逐字节核对）

| 本地文件 | 字节数 | 溯源 | 匹配 |
|---|---|---|---|
| `diffusion_models/MiniMax-H3/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 20,970,379,616 | Comfy-Org/MiniMax-H3 | **精确** |
| `diffusion_models/MiniMax-H3/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 20,970,379,616 | Comfy-Org/MiniMax-H3 | **精确** |
| `text_encoders/MiniMax-H3/qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 27,141,342,152 | Comfy-Org/MiniMax-H3 | **精确** |
| `text_encoders/MiniMax-H3/qwen3vl_32b_minimax_h3_int4_convrot.safetensors` | 14,952,506,624 | **`Merserk/MiniMax-H3-INT4-ConvRot`（社区）** | **精确** |
| `loras/MiniMax-H3/minimax_h3_fl2v_turbo_4step_v1.2_768p_comfyui_bf16.safetensors` | 1,956,193,000 | `lightx2v/Minimax-h3-Turbo` | **精确** |
| `loras/MiniMax-H3/minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors` | 1,956,193,000 | `lightx2v/Minimax-h3-Turbo` | **精确** |
| `loras/MiniMax-H3/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors` | 1,956,193,000 | `lightx2v/Minimax-h3-Turbo` | **精确** |
| `vae/MiniMax-H3/minimax_h3_video_vae_fp16.safetensors` | 5,207,808,496 | Comfy-Org/MiniMax-H3 | **精确** |
| `vae/MiniMax-H3/minimax_h3_audio_vae_fp32.safetensors` | 605,254,808 | Comfy-Org/MiniMax-H3 | **精确** |

**9 个文件里 7 个来自 Comfy-Org 官方，3 个 Turbo LoRA 来自 lightx2v 原仓库（注意：Comfy-Org 的 `loras/` 只有 v1.0，没有你手上的 v1.2），只有 1 个 int4 编码器来自社区。**
另一个社区副本 `Abiray/MiniMax-H3-GGUF` 也有同名文件，但字节数为 14,952,506,709（差 85 字节），故本地这份来自 Merserk。

### 2.5 术语考证

**`convrot`**（一手来源：[comfy-quants `docs/formats/int8_w8a8.md`](https://github.com/Comfy-Org/comfy-quants/blob/main/docs/formats/int8_w8a8.md)）
原文：**"Optional ConvRot rotates each weight (regular Hadamard, group 256) before quantization to spread channel outliers"**，质量目标为 **"near-GGUF-Q8 quality"**。
- 旋转矩阵：**regular Hadamard**（H4 的 Kronecker 幂），归一化 `1/sqrt(size)`，**不是** Sylvester Hadamard
- 分组大小 **256**；每层标记 `<layer>.comfy_quant`，内容 `{"convrot": true, "convrot_groupsize": 256, "per_row": true}`
- 离线 `W_rot = W R`，运行时 `X_rot = X R`；因 `R Rᵀ = I` 故 `X_rot W_rotᵀ = X Wᵀ`，线性算子等价
- 若 `in_features` 不能被 256 整除，该层跳过旋转，标记写 `{"convrot": false, ...}`

**`pruned`**（一手来源：ComfyUI 官方博客 + 官方 MiniMax README）
= AdaLN 剪枝。ComfyUI 博客：modulation weights（约 40% 参数）"could be pruned and replaced with a functionally equivalent lookup table"，声称 "no loss in output quality"。官方 MiniMax 侧印证：约 13B / 33B 参数在 AdaLN 分支，"can be precomputed and cached"，推理时无需加载。

---

## 3. 16 GB 显存 + 32 GB 内存约束下的可行性判断

### 3.1 先说一个可能立刻见效的启动参数（与你的现状相关）

**`--disable-smart-memory` 修掉了 RX 9070 XT 上 `MiniMaxH3VideoVAE` 加载报 `CUDA error: invalid argument` 的问题，Windows 与 Linux 双双确认。**

- Comfy-Org/MiniMax-H3 discussion #17，用户 `litacc`（**RX 9070 XT 16GB / 5600X / 32GB / Win11 —— 与你的配置几乎一致**）报视频 VAE 加载失败，traceback 终于 `comfy/model_patcher.py:1075` 的 `x[2].to(device_to)`，报 `CUDA error: invalid argument`（对应 `hipErrorInvalidValue`）。加 `--disable-smart-memory` 后 "work perfect"。
- 同一 discussion 里 Linux 用户 `doplxyz` 确认该 flag 在 Linux 上同样修掉这个崩溃。

**你的启动参数是 `--enable-dynamic-vram --disable-pinned-memory --fast-disk`，没有 `--disable-smart-memory`。** 你目前没撞到这个崩溃，但这条值得记下作为排错首选。

> ⚠ **`--disable-smart-memory` 与 `--enable-dynamic-vram` 是否冲突未经证实** —— 我没找到同时使用这两个 flag 的公开记录。若要试，请一次只改一个变量。

### 3.2 DynamicVRAM 在 ROCm/Windows 上的已知问题（与你的配置逐项吻合）

[ComfyUI issue #15484](https://github.com/Comfy-Org/ComfyUI/issues/15484)（**Open**，2026-08-11 提，无维护者指派）
环境：**Windows 11 / RX 9070 XT 16GB (gfx1201) / ComfyUI 0.31.0 / comfy-kitchen 0.2.28**，开 DynamicVRAM，**并明确试过 `--disable-pinned-memory`，报告"does not fix this VAE timing instability"**。

症状：DynamicVRAM 让 H3 diffusion 模型与 VideoVAE **共存驻留**，合计约 14.5 GB（日志 `Total VRAM for VBARs: 14528 MB`，卡总 16.3 GB，`Device: 0 MB / 16304 MB Free`）。**不是 OOM** —— decode 能跑完，但耗时剧烈波动：

| 配置 | 5 次 VAE-only 耗时 |
|---|---|
| 放任共存 | 43.48 / 26.57 / 47.00 / 35.22 / 52.05 s |
| 解码前强制卸载 | **16.02 / 15.91 / 15.89 / 16.01 / 16.19 s**（后续复测 16.06–16.28 s，stddev ~0.07 s） |

**规避**：在 latent 与 VAE decode 之间插一个诊断节点，调用 `mm.unload_all_models()` + `mm.soft_empty_cache()`。

**对你的意义**：这是**你这一档配置（Win11 + 16GB gfx1201 + ComfyUI 0.31.0 + DynamicVRAM + 试过 --disable-pinned-memory）的已知未解缺陷**，会造成 VAE 解码时间 1.6–3.2× 波动。另外注意报告者用的是 **64 GB** 内存，你只有 32 GB。

### 3.3 真正的瓶颈是 32 GB 系统内存，不是 16 GB 显存

来源：discussion #17 的 `doplxyz` 详细实测（gfx1201，Linux，`--lowvram --fp16-vae --disable-smart-memory`，配置为 `fl2va_pruned_int8_convrot` + nvfp4_awq TE）：

- **text encoder 被放在 CPU 上**，是主要瓶颈：**每个新提示词 90–104 s**（3900X）；提示词缓存命中则 0 s。
- **峰值系统内存约 56 / 62 GB**，并明确结论："**32GB machines will likely struggle.**"
- 存在 offload 悬崖：可用 VRAM / DiT 常驻 / 已 offload MB 在最低分辨率是 15010/14636/5360，480p 是 11601/10996/8999，1344×768 是 6582/5737/14259。采样耗时在模型装得下时约 N^1.25，一旦 offload 主导则约 N^1.59。
- 16 GB 卡的实用上限：**1344×768/5s 或 848×480/~10s**；模型最大配置（1344×768 × 362 帧 ≈ 15s）外推约 109 分钟且很可能 OOM。
- 实测耗时（20 步 T2V，含立体声）：320×192/5f 176.7 s；848×480/124f 403.4 s；1344×768/124f 1340 s（~22.3 分钟）。

**推论（基于上述内存数据，属推断）：**
- 你当前 `pruned_int8_convrot`（19.53 GiB）+ 32 GB RAM 已属紧张。#15784 系列的实测在 64 GB 上才勉强舒服。
- 换 **非剪枝 int8_convrot（31.70 GiB，+12.17 GiB）**：text encoder 已占 CPU 侧内存，再给 diffusion 加 12 GiB，**32 GB 上大概率不可行**。
- 换 **pruned_bf16（37.46 GiB，+17.93 GiB）**：更不可行。

### 3.4 所以：有没有比现状更高精度的可行路径？

**诚实回答：没有一条是「明确更高精度且确定能跑」的。** 分三种情况：

| 想要 | 候选 | 判断 |
|---|---|---|
| 同一 int8_convrot 内保留全部层 | `*_int8_convrot`（非剪枝，31.70 GiB） | **量化误差不变，只去掉剪枝**。但 +12.17 GiB，32 GB RAM 上大概率不可行。**这是唯一「方案不变、精度可能更高」的选项，也是最值得先试的。** |
| 提高数值精度 | `*_pruned_bf16`（37.46 GiB） | +17.93 GiB，**不可行** |
| 同尺寸换方案 | `*_pruned_fp8_scaled`（19.52 GiB） | **尺寸几乎完全一致（−0.01 GiB）**，且在 RDNA4 上是**原生 fp8**（RDNA3 会被加宽成 bf16，你这代不会）。Comfy-Org 称 int8_convrot 优先，但那条建议是写给 NVIDIA cu130 的。**这是唯一「零尺寸代价」的可 A/B 选项。** 但注意 comfy-kitchen issue #125 在 gfx1201 上报过 fp8 反量化段错误。 |
| 省显存/省内存 | `video_vae_int8_convrot`（2.62 GiB，**省 2.23 GiB**） | **最便宜的一步**，官方 PR 合并、Kijai 称质量无问题、约 1.5× 加速 |

**优先级建议（从零风险到有风险）：**
1. 换 `minimax_h3_video_vae_int8_convrot.safetensors` —— 省 2.23 GiB，官方支持，风险最低。
2. 用 `*_pruned_fp8_scaled`（19.52 GiB，同尺寸）与现有 int8 做 A/B —— 零尺寸代价，换一条原生内核路径。
3. 只有在内存实测有余量时，才试非剪枝 int8_convrot（31.70 GiB）。
4. 不要考虑 pruned_bf16 / bf16（+17.93 / +42.20 GiB）。

---

## 4. text encoder 取舍：int4(13.93 GiB) vs int8(25.28 GiB)

### 4.1 在你机器上的可行性

**关键事实：text encoder 是被放到 CPU 上的，所以它吃的是系统内存（32 GB），而系统内存正是这台机器的瓶颈。**

- `doplxyz` 实测：TE 在 CPU 上，每新提示词 90–104 s；峰值系统内存 56/62 GB。
- 你的 int8 TE 是 25.28 GiB，int4 TE 是 13.93 GiB，**差 11.35 GiB**。
- 32 GB 内存要同时容纳操作系统 + ComfyUI + 被 offload 的 diffusion 权重 + TE。**int8 TE(25.28 GiB) 在 32 GB 上极可能触发大量换页**；discussion #17 里 `litacc`（同为 32GB/Win11，但用 int4）报告每次运行要往 SSD 写 "10~30G"。

**所以：在你的机器上，int4 TE 很可能是内存约束下的务实必需，而不是纯粹的质量取舍。** 这一点很重要——「换回 int8 以获得更好提示词服从度」在你的内存预算下可能根本跑不动。

### 4.2 int4 是否削弱长提示词服从度：有量化证据，但格式不对口

**先说结论：没有针对 `int4_convrot` 的直接证明；但有指向同一方向的量化证据，且恰好命中你的使用场景（长结构化提示词）。**

**证据一：13 语言多语基准**（最详细）
来源：[`rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui` README](https://huggingface.co/rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui)

方法：以 BF16 为参考，同一段文本只换编码器精度。13 语言 × 2 提示词（1 长技术型 H3 提示词 + 1 短日常提示词）。
指标：「scrambled word」= cosine 相似度 < 0.95 的词；outlier rate = 每 100 词的 scrambled 词数。

**被测的 4-bit 是 `nvfp4_awq`，不是 `int4_convrot`。**

| 语言 | 4bit scrambled/100 | 最差单词 | 漂移 | → FP8 scrambled/100 | → FP8 漂移 |
|---|---|---|---|---|---|
| EN | 1.8 | 0.61 | 9.4% | 1.4 | 7.2% |
| DE | 2.1 | 0.68 | 9.3% | 0.9 | 4.8% |
| ES | 2.6 | 0.59 | 12.4% | — | — |
| FR | 2.5 | 0.68 | 12.2% | — | — |
| ZH | 3.1 | **0.48** | 10.0% | 1.6 | 5.5% |
| HI | 3.3 | 0.55 | 11.1% | — | — |
| RU | 1.8 | 0.64 | 9.4% | — | — |
| AR | 2.0 | 0.57 | **14.0%** | 0.7 | 7.6% |
| FA | 2.1 | 0.69 | 10.2% | — | — |
| KU | 2.2 | 0.67 | 12.6% | — | — |
| TR | **1.5**（最好） | 0.76 | 9.5% | 0.9 | 4.8% |
| EL | **3.4**（最差） | 0.52 | 12.2% | 1.3 | 5.7% |
| HE | 1.9 | 0.70 | 9.6% | — | — |

- **短提示词：13 种语言全部 0 个 scrambled word**（作者称 4-bit "basically perfect"）。
- **长提示词**：平均每 100 词约 2 个词义被打乱，后果是「发音错误 / 某个词音频断裂 / 角色动作错误」。

> **⚠ 该来源有利益冲突**：rzgar 自己就是 FP8 编码器的发布者，结论「FP8 是明显升级」与其利益方向一致。数字可参考，请打折。

**证据二：直接测 conditioning 漂移**（方法最严谨，但不测渲染结果）
来源：[`mudler/vllm.cpp` commit `8eeac9e`](https://github.com/mudler/vllm.cpp/commit/8eeac9ea7456acd4717b7f25e66ee4305d4b5a3d)

控制变量：同一提示词（233 tokens）、同 tokenizer、同样第 50 层截断、同 f32 激活，**只有权重字节不同**。

| 实验臂 | 相对 RMS（排除 sink） | cosine 均值 | cosine 中位 | cosine 最小 | 角度中位 |
|---|---|---|---|---|---|
| Q4_K_M vs bf16 | 0.06849 | 0.99745 | 0.99810 | 0.90916 | 3.535° |
| **bf16 改一个词** | 0.06666 | 0.99769 | 0.99963 | 0.84736 | 1.565° |

- 量化搬动 conditioning 的**总能量 ≈ 改一个词**（6.85% vs 6.67%）。
- 但**形状相反**：量化是 **DIFFUSE** 的 —— "232 of 233 tokens fall below cosine 0.999"，**每个 token 都被轻微旋转**；改词是**稀疏**的。
- 作者明确声明**未证明的事**："It does not prove the render changes."

**证据三：渲染层面配对比较**（n=16，样本偏小）
来源：[`qtum/MiniMax-H3-Qwen3-VL-NVFP4`](https://huggingface.co/qtum/MiniMax-H3-Qwen3-VL-NVFP4)：LPIPS 0.099、SSIM 0.891、闪动 13.75→14.12（+2.7%）、PSNR 24.85 dB，作者结论为与 bf16 "perceptually and temporally equivalent"；作者自陈 FVD 是 "internal relative reference only, not a fidelity verdict"（n=16 协方差欠定）。

### 4.3 ⚠ 一个对你有利的反向因素

证据一测的是 `nvfp4_awq`，而**在你的 gfx1201 上 `nvfp4` 是「模拟」的、`convrot_w4a4` 是「原生」的**（§1.2 实机日志）。也就是说：

- 被基准测出「长提示词掉 2%」的那个 4-bit 格式，在你的卡上跑的是**模拟路径**；
- 你实际在用的 `int4_convrot` 走的是**原生 `convrot_w4a4` 内核**。

**所以那份基准的结论不能直接套到你的 int4_convrot 上——你的 4-bit 在硬件路径上比被测的那个更好。** 反过来说，若你想在 AMD 上复现那份基准的 FP8 对比，`fp8_scaled` 在你的卡上也是原生的（RDNA4 独有），这个 A/B 在硬件层面是可比的。

### 4.4 结论

1. **在你 32 GB 内存的约束下，int8 TE 很可能跑不动/极慢**（25.28 GiB 常驻 CPU 侧 + 换页）。int4 TE 是务实选择。
2. `int4_convrot` 削弱长提示词服从度**没有直接证据**；间接证据（nvfp4_awq 与 Q4_K_M）都指向「短提示词无损、长提示词每百词约 2 个词义受损、且误差弥漫到全部 token」。
3. **实操建议**：你那两个提示词质量最相关的地方正是**长结构化提示词**（`integrated_multimodal_description` + `overall_soundscape` + `non_diegetic_music`），恰好是 4-bit 最先失守的场景。**但换 int8 的前提是先确认内存跑得动。** 建议先在只有 32 GB 内存的条件下实测 int8 TE 是否可用（提示词编码耗时、是否疯狂换页），再谈质量取舍——**不要为了一个跑不动的精度档牺牲可用性。**

---

## 5. AMD 侧可用工具（按可信度排序）

| 工具 | 作用 | gfx1201 支持 | 可信度 |
|---|---|---|---|
| [`Newaiguy/ComfyUI-h3_sage_amd`](https://github.com/Newaiguy/ComfyUI-h3_sage_amd) | **明确面向 AMD gfx12（RX 9070 / 9070 XT）**。seq ≤ 30000 时零开销透传；seq > 30000（如 Ref2VA 的 41414/54545）自动把 42 个 head 分 4 组、逐组 fp16 sageattn，**注意力峰值显存 ~11.3 GB → ~7.1 GB（−37%）**。作者自述"不是提速器，是 16GB 显存卡跑长序列的保险丝" | ✅ **明确列出 RX 9070 / 9070 XT** | 社区，作者给了实测表 |
| [`Zironic/H3-Optimizations`](https://github.com/Zironic/H3-Optimizations) | H3 显存优化 + 稀疏注意力，AMD Sparse Kitchen 库**预编译 gfx11/gfx12（ROCm 7.2.1）** | 声称支持 gfx12 | ⚠️ **作者自述 gfx11/gfx12 "have not yet passed a live AMD run"，"A live gfx11 or gfx12 run is still required to establish numerical correctness and performance"** —— 即**未经实机验证**。且需 ComfyUI ≥ 0.33.0（你 0.31.0 不够），稀疏注意力 "is not free acceleration" |
| [`DrBearJew/ComfyUI-INT8-Fast-ROCM-ConvRot`](https://github.com/DrBearJew/ComfyUI-INT8-Fast-ROCM-ConvRot) | `MiniMaxH3INT8FastLoader`：只把 checkpoint 的 INT8 ConvRot transformer 投影走 ROCm 快路径，精度敏感的 conditioning / AdaLN / patch / audio / video 输出层保持 BF16/FP16/FP32 | ❌ **仅验证过 RX 7900 XTX / gfx1100**，原文 "Other GPUs and ComfyUI versions are unvalidated" | 社区，ROCm 专属但**不覆盖 RDNA4** |

`Zironic/H3-Optimizations` 的 AMD 分支我在报告里标为**不建议现在上**：既未实机验证，又要求 ComfyUI ≥ 0.33.0。

**Newaiguy 那个节点值得关注** —— 它解决的问题（16 GB 卡跑 Ref2VA 长序列注意力 OOM/挂死）正好是你这块卡的典型痛点，且是唯一明确点名 RX 9070/9070 XT 的项目。附带信息：该 README 里有 gfx12 上 int8 注意力内核的实测表（seq 8192 时 int8 比 fp16 慢 1.31×），解释了为何 AMD 走 head-chunking 而非 int8——**与「gfx12 只有一个未调优的原生 int8 内核」有关**。

---

## 6. 存疑与未证实项

| 项 | 状态 |
|---|---|
| **Triton 在 ROCm 上默认开还是关** | **冲突未解决**：PR #14869（已合并）说 gfx1201 自动开；我实测的 master `quant_ops.py` 写 "off by default on every platform"。**不选边。** 用 §7 的日志验证。 |
| **int8_convrot 在 gfx1201 上会不会出黑图** | **两条独立证据矛盾**：第三方 SEO 文章称会（并建议改用 fp8_scaled），而你的实机与 `doplxyz` 的实机都跑通了。**不选边**，三种可能见 §1.7。来源可信度低。 |
| 「剪枝无损」（ComfyUI 博客 "no loss in output quality"） | **厂商声明，无任何独立复测**。整个生态都在转发，找不到第三方 A/B。 |
| 「int8_convrot 优于 fp8_scaled」 | Comfy-Org 原文只说 "prefer"，**且限定 `if you are able to use pytorch with cu130`** —— 这是 NVIDIA 条件，**对 AMD 不适用**，不能当作 AMD 上的质量判据。 |
| `--disable-smart-memory` 与 `--enable-dynamic-vram` 是否冲突 | **未经证实**，未找到同时使用的公开记录。 |
| int4_convrot 削弱长提示词服从度 | **无直接证明**。最接近的是 nvfp4_awq 与 Q4_K_M 的测量，两者都**未测渲染输出是否变化**，且**格式与你的 int4_convrot 不同**。 |
| 你的 `int4_convrot` 具体走哪个内核（w4a4？w4a8？） | **未经证实**。gfx1201 的原生日志里有 `convrot_w4a4`，但该文件的实际量化格式串未核验（需读其 `comfy_quant` 元数据）。 |
| §3.3 的非剪枝 int8_convrot「32GB 大概率不可行」 | **推断**（基于 64 GB 机器峰值 56 GB 的实测外推），未在 32 GB 机器上直接验证。 |
| Merserk `int4_convrot` 的量化方法与质量 | 社区二次量化，**无任何质量数据**。Comfy-Org 全部 35 条 commit 中从未出现 int4 文件 → 官方从未出过 int4。 |
| 其他社区量化（GGUF 各档、W4A8、hybrid 融合） | 只有体积与定性说法，**无对照测试**。`smhfacct` 等的 fl2va+ref2va 融合模型有合并失真风险，未评估。 |
| 音频 VAE 必须 fp32（fp16 导致音画不同步） | 社区口径，**未经证实**。 |
| `Zironic/H3-Optimizations` 的 AMD 路径 | 作者自己声明**未在 gfx11/gfx12 实机跑过**。 |
| Abiray 的 `nvfp4_awq` 文件名 | 实测字节数 27,141,342,223 = int8_convrot 的体积（真 nvfp4 应为 15,687,142,551），**疑为错标**。下载社区文件前务必核对字节数。 |
| 2K 本地生成 | **不可能**。Regenerate-2K 未开源，本地上限 768p 短边。 |
| 许可证 | MiniMax H3 Community License Agreement：年收入 < 2000 万美元可商用；**美/英/欧/韩为 excluded territories**，需申请。商用前请自行读 `MiniMaxAI/MiniMax-H3/LICENSE` 与 `docs/QA-about-License.md`。 |

---

## 7. 建议的验证步骤（零成本 → 有成本）

**第 0 步：确认你机器上到底哪些量化算子是原生的。**
启动一次，在日志里找 `Native ops:` 那一行。它会按你的实际安装列出原生/模拟算子清单——**这是唯一能确定 Triton 默认开关与各格式真实状态的证据**（`doplxyz` 在 gfx1201 上得到的是 `int8_tensorwise, float8_e5m2, convrot_w4a4, float8_e4m3fn` 原生、`nvfp4, mxfp8` 模拟）。这一行同时能定论 §1.6 与 §6 的前两条。

**第 1 步：换 video VAE 到 int8_convrot**（省 2.23 GiB，官方 PR、Kijai 称质量无问题、约 1.5× 加速）。
文件：`vae/minimax_h3_video_vae_int8_convrot.safetensors`（2,811,065,184 B）

**第 2 步：A/B `pruned_fp8_scaled` vs 现有 `pruned_int8_convrot`**（尺寸几乎一致，19.52 vs 19.53 GiB，零尺寸代价）。
保持其他一切不变（提示词、seed、步数、采样器、LoRA、VAE），只换 diffusion 文件。注意 comfy-kitchen issue #125 在 gfx1201 上报过 fp8 反量化段错误——若崩溃，那是已知 bug，不是你配置错。

**第 3 步：测 int8 text encoder 在你 32 GB 内存下是否可行**（提示词编码耗时 + 是否疯狂换页）。可行才谈质量取舍。

**第 4 步（仅在第 1–3 步证明内存有余量时）：试非剪枝 `int8_convrot`**（31.70 GiB，+12.17 GiB）。这是唯一「量化方案不变、仅去掉剪枝」的提精度路径。

**排错首选**：遇到 VAE 加载 `CUDA error: invalid argument` → 试 `--disable-smart-memory`；遇到 VAE 解码耗时剧烈波动（26–52 s）→ 是 issue #15484，考虑解码前 `unload_all_models()`。

---

## 8. 主要来源

**官方 / 一手**
- https://huggingface.co/MiniMaxAI/MiniMax-H3 ・ https://huggingface.co/MiniMaxAI/MiniMax-H3/raw/main/README.md
- https://www.minimax.io/news/minimax-h3-open-source
- https://huggingface.co/Comfy-Org/MiniMax-H3 ・ https://huggingface.co/Comfy-Org/MiniMax-H3/raw/main/README.md
- https://raw.githubusercontent.com/Comfy-Org/comfy-kitchen/main/README.md ← **后端/架构能力矩阵**
- https://github.com/Comfy-Org/comfy-quants/blob/main/docs/formats/int8_w8a8.md ← **ConvRot 规格**
- https://raw.githubusercontent.com/comfyanonymous/ComfyUI/master/comfy/quant_ops.py
- https://github.com/Comfy-Org/ComfyUI/pull/14869 ・ https://github.com/Comfy-Org/ComfyUI/pull/15334
- https://blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui
- https://docs.comfy.org/tutorials/video/minimax/minimax-h3
- https://github.com/Comfy-Org/comfy-kitchen/issues/78 ・ https://github.com/Comfy-Org/comfy-kitchen/issues/125
- https://github.com/Comfy-Org/ComfyUI/issues/15484
- https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/17 ← **gfx1201 实测数据**
- https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/22

**社区量化**
- https://huggingface.co/Merserk/MiniMax-H3-INT4-ConvRot ← 你的 int4 编码器出处
- https://huggingface.co/lightx2v/Minimax-h3-Turbo ← 你的 Turbo LoRA 出处
- https://huggingface.co/Abiray/MiniMax-H3-GGUF ・ https://huggingface.co/Abiray/Minimax-H3-nvfp4-INT4-INT8-Convrot
- https://huggingface.co/realrebelai/MiniMax-H3_GGUFs
- https://huggingface.co/rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui ・ https://huggingface.co/qtum/MiniMax-H3-Qwen3-VL-NVFP4

**AMD 专属工具与测量**
- https://github.com/Newaiguy/ComfyUI-h3_sage_amd
- https://github.com/Zironic/H3-Optimizations
- https://github.com/DrBearJew/ComfyUI-INT8-Fast-ROCM-ConvRot
- https://github.com/mudler/vllm.cpp/commit/8eeac9ea7456acd4717b7f25e66ee4305d4b5a3d
- https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/compatibilityrad/windows/windows_compatibility.html

**低可信度（已标注，仅作线索）**
- https://localaimaster.com/blog/comfyui-amd-rocm-image-generation ← 黑图说法，与两条独立证据矛盾
- https://comfyui-wiki.com/en/news/2026-08-03-minimax-h3-community-quants
- https://github.com/wildminder/awesome-minimax-H3/blob/main/guides/minimax-h3-performance.md
