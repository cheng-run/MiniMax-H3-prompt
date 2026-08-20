# Windows PowerShell
# Set-Location D:\项目\MiniMax-H3-prompt
# uv sync
# uv run minimax-h3-prompt

# MiniMax-H3 多智能体提示词生成系统

为 **MiniMax-H3** 生成高质量视频提示词的多智能体系统。用一个模仿**电影制作 + AIGC 双流程**的 12 角色流水线，把一段创意 brief 打磨成符合 MiniMax 官方规范（`h3-prompt-writing`）的提示词，复制粘贴即可出片。

- 技术栈：**LangChain（`create_agent`）+ LangGraph（StateGraph 管线 + 有界圆桌子图）**
- 输出双模式：**Ref2VA 全参考六段式** + **基础模式**（T2VA / I2VA / FL2VA / L2VA）三段式
- 格式唯一依据：仓库内 `references/` 的官方规范文档（`base-en.txt` / `ref-en.txt`）

## ✅ 当前状态（2026-08-14）

| 项 | 状态 |
|---|---|
| 12 角色 agent 装配 | ✅ `create_agent` 构建通过 |
| LangGraph 管线编译 | ✅ 14 节点线性 DAG + 3 圆桌子图，桩模型验证每节点仅执行 1 次 |
| H3 校验器单测 | ✅ 18 个用例全绿 |
| T2VA 端到端 | ✅ 实测通过：耗时 ~342s，产出 3542 字符完整三段式提示词，**终检 0 错误** |
| Ref2VA 端到端 | ✅ 实测通过：耗时 477s，产出 7954 字符六段式提示词，**终检 0 错误 0 警告**；`<Picture 1/2/3>` 正确映射三个角色/场景 |

> T2VA 实测产物示例见文末附录。

## 快速开始

```bash
uv sync                          # 安装依赖
uv run minimax-h3-prompt         # 统一入口：交互菜单（选出片方式/brief/参数，实时进度）
uv run minimax-h3-prompt --brief examples/brief_t2va.md   # 非交互快路径（脚本用）
uv run minimax-h3-prompt --brief your.md --dry-run        # 只解析 brief，不跑 LLM
uv run pytest tests/             # 单测（29 个）
```

## 交互与观测（不黑箱）

`uv run minimax-h3-prompt` 进交互菜单：**① 纯文字出片 ② 多参考图出片 ③ 润色草稿 ④ 仅解析自检 ⑤ 退出** → 选 brief（examples 列表或自定义路径）→ 参数（变体/时长/风格/语言，回车用默认）→ 确认后开跑。

运行期间：
- **实时进度面板**：每个角色/圆桌一行（状态 ✓/运行中、耗时、产出长度），顶部累计耗时与 token 费用。
- **中间产物落盘**：每个阶段的产物写 `output/stages/<节点>.txt`，跑完可翻看每一步。
- **token/费用统计**：按角色累计 input/output token，按 `config/agent.yaml` 的 `cost` 单价估算费用（DeepSeek 估算价，可改）。

### LLM 后端与统一配置

模型的 provider、名称和 base URL 统一配置在 `config/agent.yaml` 的 `models.primary`（主文本模型）与 `models.vision`（参考图视觉模型）中；API key 仍只放项目根 `.env`，不会写入 YAML 或入库。当前默认配置为 DeepSeek 主模型和 DashScope `qwen3.7-plus` 视觉模型：

```yaml
models:
  primary:
    provider: deepseek
    model: deepseek-v4-flash
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
  vision:
    provider: dashscope
    model: qwen3.7-plus
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
```

`.env` 示例：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=xxx
DASHSCOPE_API_KEY=xxx
```

`LLM_PROVIDER`、各 provider 的 `*_MODEL`/`*_MODEL_NAME`/`*_BASE_URL` 仍作为旧配置兼容覆盖项；未设置时才使用 `agent.yaml` 的值。不要把真实 key 写进仓库。

## brief 格式

`examples/` 里有示例（模板 `examples/brief_template.md`）。**`输入` 键对应你喂工作流的参考方式**：

```markdown
输入: 首尾帧     # 首帧（工作流62·1张图）| 尾帧（工作流62·1张图）| 首尾帧（工作流62·2张图）| 多参考（工作流63·N张图）
时长: 6          # 秒
风格: Cinematic 武侠电影质感
语言: Chinese

## 剧情
一段话描述创意……（核心输入）

## 参考图          # 张数随输入：首帧/尾帧=1张；首尾帧=2张（Picture1=首帧 Picture2=尾帧）；多参考=N张（角色/场景）
                  # 格式：Picture N: 名称 — 描述  (图片路径)；有路径 → 运行前用 qwen3.7-plus 读图自动出描述，人工确认后进管线
- Picture 1: 首帧 — 白发仙师在竹林缓抬手  (D:\Comfyui\ComfyUI\output\图片\你的图.png)
- Picture 2: 尾帧 — 青衫青年落地向仙师行礼

## 草稿提示词      # 可选；写了即进入 polish（润色）流程
```

命令行：`--model fl2va|ref2va|i2va|l2va` 覆盖（或旧写法 `--mode/--variant`）；`--polish` 润色；`--dry-run` 只解析。最终提示词粘到你工作流的 `PrimitiveStringMultiline` 节点（纯净文本）。

> **参考图审核**：在 Windows 下，brief 参考图行末尾填写本机图片路径（例如 `D:\Comfyui\ComfyUI\output\图片\你的图.png`）；在 WSL 下才使用 `/mnt/d/...` 路径。运行前会用阿里云 `qwen3.7-plus`（多模态，读取 `.env` 中的 `DASHSCOPE_API_KEY`）自动读图生成准确描述 → 菜单里你确认/修改后才进管线。图片自动压缩（≤1024px）避免大图超限。

## 架构

### 12 角色（电影班底 + AIGC 特色）

| 阶段 | 角色 | 产出物 |
|---|---|---|
| 前期 | 制片 Producer | 制作计划（模式/时长/风格/任务类型） |
| 前期 | 导演 Director | 导演阐述（情绪基调/视觉风格/节奏） |
| 编剧 | 编剧 Screenwriter | 分场剧本（对白保留原文） |
| 美术 | 美术指导 Art Director | 角色造型卡/场景/色彩/美术风格 |
| 视觉 | 分镜师 Storyboard | `[Shot N]` 镜头表（景别/机位/切点） |
| 视觉 | 摄影指导 Cinematographer | 每镜画面细化（英文，供最终提示词正文） |
| 声音 | 声音设计师 Sound Designer | 对白表 `(Sx)`/`<d>[语言]` + 环境声 |
| 声音 | 配乐师 Composer | `non_diegetic_music` 素材 |
| AIGC | 参考资产与一致性经理 Reference & Consistency | `subject_definitions`/`retention_analysis` 素材（ref 模式） |
| AIGC | 可生成性审查员 Feasibility Reviewer | 每镜可生成性风险评估（参与镜头评审会） |
| AIGC | 提示词工程师 Prompt Engineer | 按官方规范组装最终提示词 |
| 质检 | 质检员 QA | 质检报告 + 修正建议（Python 精修循环里用） |

### 管线（LangGraph 线性链 + 组合节点内并发 + 3 有界圆桌）

```
START → [制片] → [导演] → [创意会·圆桌] → [编剧] → [美术指导] → [分镜]
     → [并行决策：镜头评审会‖一致性对齐会]
     → [并行视觉：摄影指导‖参考资产与一致性](ref)
     → [并行声音：声音设计‖配乐师]
     → [提示词工程师] → [组装器/终检] → END
```

**3 个有界圆桌**（LangGraph 子图）：创意会（制片+导演+编剧）、镜头评审会（分镜+摄影+可生成性审查）、一致性对齐会（美术+参考资产，仅 ref 模式）。参与角色以"讨论 persona"互相发言 ≤2 轮，主持人收束成锁定决策——既拿到多 agent 协商的质量红利，又锁死成本与不收敛风险。

**质检精修在 Python 层做有界循环**：确定性校验驱动 → 有 error 级问题则 `qa` 角色给建议、`prompt_engineer` 重出，最多 `max_qa_iterations`（默认 2）轮。`h3_validator` 规则校验是硬门槛，无论 agent 怎么讨论都要过。

### 实现要点（与最初方案的两处偏差，均为实测后修正）

1. **角色 agent 用 `langchain.agents.create_agent`，不是 `deepagents.create_deep_agent`**。原因：deepagents 基础栈**无条件注入文件系统/执行工具**（`ls`/`write_file`/`execute`/`task` 等），单次产出文本的角色会去调 `write_file`/`ls` 而不是直接返回文本，导致空产出（实测确认）。create_agent 是同一 LangChain 栈、无默认工具、轻量。"deep-agent"式多 agent 讨论思想已用 LangGraph 有界圆桌实现。
2. **管线 = 线性链 + 组合节点内并发**。并行 fan-in 深度不对齐时 LangGraph 会重复触发汇合节点（实测确认），故整图保持线性、每节点恰好执行一次；把互不依赖的子任务（镜头评审会‖一致性对齐会、摄影‖参考资产、声音设计‖配乐师）折叠进「并行组合节点」，内部用 `ThreadPoolExecutor` 并发，墙钟省约 30%。

## 输出格式

- **ref（Ref2VA）六段式**：`subject_definitions` → `summary`（`[任务类型]` 前缀）→ `retention_analysis`（fully_preserved 等标记）→ `detailed_description`（350–500 英文词）→ `overall_soundscape` → `non_diegetic_music`
- **base 三段式**：`integrated_multimodal_description` → `overall_soundscape` → `non_diegetic_music`（I2VA/FL2VA/L2VA 带图片对齐指令）
- 对白/歌词保留原文放 `<d>[语言]…</d>`；说话人 `(Sx)` 全局连续；切点 `[Shot N] At MM:SS.mmm` 严格递增；声音段无声统一 `N/A`。

## 配置

`config/agent.yaml`：`max_qa_iterations`（质检精修上限）、`roundtable_max_rounds`（圆桌轮数上限）、`default_duration`、`output_path`（默认 `output/final_prompt.txt`）。

## 目录结构

```
src/minimax_h3_prompt/
├── main.py              # CLI
├── config.py            # YAML + `.env` 统一配置（API key 仅从 `.env` 读取）
├── model_factory.py     # LLM 后端工厂（自动探测）
├── brief_parser.py      # brief 解析
├── references/          # H3 官方规范（唯一格式依据）
├── prompts/             # 12 个角色 system prompt
├── agents/              # create_agent 装配 + run_agent
├── graph/               # state / roundtable / nodes / pipeline
├── tools/               # h3_validator + ref_metadata
└── output/              # assembler（确定性格式保障）+ renderer
```

## 已知限制与后续优化

- **耗时**：T2VA 一次全跑约 6 分钟（~30 次 LLM 调用）；ref 模式更重（约 35+ 次调用，实测超 40 分钟）。想提速：roundtable 限 1 轮、跳过非必要角色、换更快模型、或给独立环节做并行。
- **成本**：DeepSeek Flash 单次全流程 token 消耗中等；切换 Pro 质量更高但更贵。
- **Ref2VA 已端到端跑通**：待贴回 ComfyUI 验证 `<Picture N>` 角色绑定是否真的对得上参考图（视觉验收）。

---

## 附录：T2VA 实测产物节选

输入（`examples/brief_t2va.md`）：雨夜女子公交站台等车。产出（校验 0 错误）：

```text
integrated_multimodal_description: Cinematic live-action film look, ... [Shot 1] A medium shot,
side-rear handheld follow with a slight natural vertical bob. A lone man ... strides quickly
through heavy rain across wet asphalt. ... [Shot 2] At 00:01.500 Medium close-up, side angle,
shallow depth of field locked on the chrome door handle ... [Shot 3] At 00:03.500 ...

overall_soundscape: Steady medium rain falls across the wet asphalt, with a fine hiss ...
non_diegetic_music: N/A
```
