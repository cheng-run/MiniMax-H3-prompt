# MiniMax-H3 Prompt

为 **MiniMax-H3** 生成高质量视频提示词、并把长视频拆成可逐段执行的工具。用户从一句创意出发，经分镜与关键帧（或参考素材）得到最终**英文** H3 提示词（官方 `base-en.txt` / `ref-en.txt` 格式），再按段交回视频完成验收与拼接。

三条生成路线**并存**，在向导里选路线（导演台再选模式）即可：

| 路线 | 一句话 | 跨段连续靠什么 |
|---|---|---|
| **首尾帧链（base）** | 关键帧图锚定首/尾，逐段剥尾帧接力 | 尾帧桥接（剥末帧当下一段首帧） |
| **多参（ref）** | 参考素材 + 文本驱动，参考槽接线 | 每段自带的参考素材 + 起止状态声明 |
| **导演台（director）** | 项目出导演包，由 ComfyUI 导演台执行 | 段间引导（上一段 AV latent 尾部） |

三条路线的逐阶段流程、产物落点、代码入口与「改动影响面清单」**置顶在 [CONTEXT.md](CONTEXT.md)**——开发前先读那一节。

**两种入口形态、同一套业务语义**：浏览器里的 **Web 工作台**（`start_web.cmd` 一键启动，日常主入口）和终端里的**两阶段向导**（`uv run launch.py`）。工作台把会话状态、审查与修订、素材与帧提交、分段陪跑、交付归档做成网页，桌面 EXE 复用同一套页面；终端向导保留全部路线能力，也是脚本化与排障的入口。二者的分叉点、产物落点与判据一致，差别只在交互形态（分区页 vs 终端问答）。

## 当前状态（2026-11-22）

| 项 | 状态 |
|---|---|
| 三条路线 | `base` / `ref` / `director` 都已接进两阶段向导与 Web 工作台；`--brief` 快路径支持 base 与 ref |
| Web 工作台 | 列表页（导轨 + 波长轴）与详情页（步骤带 + 「等待你操作」+ 分区）跑通三条路线全流程：建会话（含建前澄清）、审查与修订、素材循环与读图校对、帧/段视频提交、SSE 实时进度、诊断包下载；另有桌面 EXE 形态 |
| 首尾帧链 | 阶段 1/2、长片拆段（4–10s）、逐段陪跑（两道闸）、段产出账、系统拼接 `final.mp4` |
| 多参 | 素材循环、素材库/台账、注入卡、ref 六段式、按需写段、逐段陪跑、拼接与 `ref-concat` |
| 导演台 | 帧网格段规划、初始导演包、逐段数帧验收、级联重跑、最终包归档；`fl2v` 按给图分支（首＋尾／仅首／仅尾／无图）写段并写明本段起点，切镜段有按需首帧词、首帧路径必填，尾帧参考图按段型分流；`ending` 有全片尾帧时优先；`r2v` 每段只给参考素材、`camera_cut` 不产首帧词；关键帧语言与工作流按出图档（Qwen／MOSS 英文、Z-Image 中文；三档工作流名／节点／画布已登记）；人物参考图默认一张综合板（四种模式：`t2v`/`i2v`/`fl2v`/`r2v`） |
| 工程守卫 | `scripts/check.sh fast` / `full` / `clean`、`CONTEXT.md` 结构守卫、ADR 引用守卫、提交信息守卫；`full` 另跑前端 eslint/prettier/vue-tsc/vitest、契约漂移与守卫变异检验（前端单独档：`scripts/check.sh frontend`） |

## 快速开始

**日常用：Web 工作台。** Windows 双击根目录 `start_web.cmd`，或 `uv run python scripts/start_web.py`。
缺前端产物会按源码 mtime 自动重建，服务起来后自动开浏览器；已在跑就直接开页面。

**脚本化用：终端向导。**

```powershell
uv sync

uv run launch.py                          # 两阶段向导（三条路线都从这里起步）
uv run launch.py --brief examples/brief_fl2va.md
uv run launch.py --brief your.md --dry-run
uv run launch.py --brief your.md --variant FL2VA

uv run python -m pytest tests -q          # 全量测试（Python）
bash scripts/check.sh fast                # 结构守卫 + 改动文件 lint + 测试
bash scripts/check.sh full                # 另跑 mypy、全量测试、前端守卫、契约漂移与变异检验
bash scripts/check.sh clean               # 模拟干净检出（摘掉本机 .env）
```

**前端工程单独跑**（只在改 `frontend/` 时需要）：

```bash
cd frontend
npx pnpm@11.7.0 install                   # 装依赖（下载由你本人执行；一律用这个版本）
npx --no-install vitest run               # 组件单测
npx --no-install vite build               # 构建产物 → frontend/dist（工作台同源托管）
PW_CHANNEL=chrome npx --no-install playwright test   # E2E，复用系统 Chrome 免下载
```

Node 版本见 `frontend/.node-version`（24）。`PW_CHANNEL`、`E2E_PORT`、工作树借 `.venv` 等本地坑见 [docs/agents/frontend.md](docs/agents/frontend.md)。

项目唯一**命令行**启动入口是 [launch.py](launch.py)，包内 `main()` 只由它调用；不再提供 `uv run minimax-h3-prompt` 或 `python -m minimax_h3_prompt`。

## Web 工作台

Vue 3 + TypeScript + Vite 前端，由 FastAPI 同源托管构建产物（`frontend/dist`）；桌面 EXE 用同一个本地服务加载同一套页面（`python -m minimax_h3_prompt.api` 起服务，`-m minimax_h3_prompt.desktop` 带窗口）。**业务状态全部在服务端**，前端只持有服务端视图、SSE 进度与局部 UI 状态。

**列表页**是「谱线导轨」：每条会话在左侧导轨上刻一道横线，状态靠**线型**表达——实线在跑、双线钠黄是「等待人工」、半高是已完成、斜划是中断/取消/失败。上方一条**波长轴**把本页会话压成一行刻度，扫一眼就知道这一页有几条等你动手；页眉那句「N 条记录 · M 条要你动手」是同一事实的文字版。

**详情页**顶上是**步骤带**（本路线一共几步、现在第几步、当前节点与已耗时）和一行**「等待你操作」**（有若干件就直说哪一件可点，点过去落在能处理它的分区）；下面分区按路线出现（短视频 base 四个，长片与 ref/director 多「分段」，导演台再加「交付」）：

| 分区 | 内容 |
|---|---|
| 概览与进度 | 会话元信息、步骤带、当前状态与中断原因 |
| 审查与待办 | 阶段 1 产物审查与分层修订；**所有人工待办卡**：帧提交、素材交图/校对四选/放弃/换一张图、补交口、导演台开场首帧与段首帧 |
| 素材 | 素材台账（编号、语义名、四态、生图档位）与素材视频/音频的字节数与时长；已定稿的 Picture 可「换一张图」，起跑前可删行 |
| 分段 | 段表、段词、按需词（导演台首帧词/尾帧词）、段级注入卡、桥接帧；段视频回交与「换一段」 |
| 产物 | 登记表里的全部产物，按 kind 分组；文本按 Markdown 排版（`<Picture N>` 等占位符保持字面），媒体可播放（Range）、包可下载 |
| 交付 | 仅导演台：交回成片与最终包，归档交付记录 |

长片分段与导演台交付都在页面里闭环：段视频交回时同步判技术档（帧数/时长/尺寸），判过才落 `passed`，全部通过后服务端自动拼接（base/ref）或挂交付闸门（导演台）。**没有 URL 里的分区状态**（刷新回概览），深链 `/sessions/<id>` 可分享给自己另开一个标签页。

设计系统见 [DESIGN.md](DESIGN.md)（令牌、线型语言、动效与无障碍约束），产品事实见 [PRODUCT.md](PRODUCT.md)，后端会话/事件/鉴权/冻结形态见 [docs/agents/workbench.md](docs/agents/workbench.md)。

## 三条路线怎么走

### 首尾帧链（base）

```text
起步澄清 → 阶段 1（剧本/设计/分镜/关键帧生图词）→ 人机修改循环
→ 提交关键帧图 → 读图 → 组装 base 三段式 → 质检精修 → video-prompt.md
   ├─ ≤10 秒：只出整片提示词（不产成片）
   └─ >10 秒：4–10s 整秒拆段 → 逐段写段 → ComfyUI 出片 → 回传视频
        → 交接校验（可证层）→ 剥尾帧 → 锚帧语义人判 → 落段账 → 下一段
        → 全段通过 → final.mp4
```

变体：`I2VA`（仅首帧）/ `L2VA`（仅尾帧）/ `FL2VA`（首尾帧）/ `T2VA`（纯文字）。长片续跑从段产出账的通过行接着走，已通过的段不重跑。

### 多参（ref）

```text
起步澄清 → 阶段 1（剧本/设计/资产图提示词/分镜）→ 素材循环
→ 视频/音频挂账 → 注入卡 → 组装 ref 六段式 → 整片提示词（存档）
   ├─ ≤15 秒：整片即唯一执行段
   └─ >15 秒：按镜头切点拆 4–15s 段（段内参考图 ≤9、只重编号 Picture）
→ 轮到哪段才写哪段 → 照注入卡接线出片 → 回传段视频
→ 交接校验 → 首/末帧读图语义人判（五档出路）→ 落段账
→ 全段通过 → 系统拼接 final.mp4
```

素材循环一次只出一张资产图提示词：出词 → 外部生图 → 审查 → 交回 → 读图校对 → 裁决 → 才出下一张。容量按官方单次请求判：**≤9 图 + ≤3 视频 + ≤3 音频、总文件 ≤12**。登记工作流为「59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2」，段内接线按注入卡照做。

### 导演台（director）

```text
起步澄清 → 阶段 1（剧本/设计/分镜；i2v/fl2v 另出关键帧词；r2v 另出资产图词）
→ 收集开场首帧/全片尾帧 → 打包参数 → [r2v：素材循环 + 英文定义块]
→ 按帧网格段规划（声明帧 17k+5）→ 逐段写 director/segment-NN.md
→ 打初始导演包 → 用户导入导演台 → 逐段跑、贴回本段成片视频 → 数帧判定
→ 全段通过 → 导演台合并导出成片与最终包 → 项目交付归档
```

- 菜单有四种模式：`t2v` / `i2v` / `fl2v` / `r2v`。`i2v` 是单段模式（一张首帧 → 一段 ≤15 秒）；要长片、运镜与切镜请用 `fl2v`。`v2v` / `rv2v` / `mixed` 已取消（#170），手写旧值会得到只列四种可用值的「模式无效」。
- `fl2v` 写段按**给图分支**落笔：首＋尾／仅首／仅尾／无图；请求另带【本段起点】（引导段接上一段 `end_hook`、切镜段硬切不承接）。`r2v` 每段只挂参考素材（≤9 张），没有首/尾帧槽。
- 切镜段按需产 `director/firstframe-NN.md`（段首帧词）；轮到它写终点画面词时，首帧路径**必填**（`q` 干净停下）。终点画面词的参考图按段型分流：引导段＝上一段终点图/视频末帧，切镜段＝本段首帧。`ending` 若交了全片尾帧，直接用它、不再产尾帧词。
- 关键帧与尾帧词的语言跟**出图档**（`config/image.yaml` 的 `frame:`）：Qwen／MOSS 英文、Z-Image 中文；三档的工作流名、节点号与画布见 `generation.IMAGE_PROFILE_DEFAULTS`。按需词带 sidecar（`.meta.json`）记模型档，换 `frame:` 后续跑会告警并问是否重写（默认否）。
- 有引导段的导出帧 = 声明帧 + 12，所以总长落位会报告「目标 / 实际 / 差几帧」。
- 逐段验收证据是**用户贴回的成片视频**（数帧）；语义验收留在导演台 UI。

## `--brief` 与 CLI 子命令

`--brief` 是非交互快路径：解析 brief → 跑管线 → 把最终提示词写到 `config/agent.yaml` 的 `output_path`（默认 `output/final_prompt.txt`）。它**不**建完整生成目录、不建素材台账、不陪跑；需要这些请用向导。`--brief` 不支持导演台路线（向导独有）。

```powershell
uv run launch.py create-video --topic "雨夜行人" --variant FL2VA
uv run launch.py generate-prompts --brief examples/brief_fl2va.md
uv run launch.py ledger rebuild <会话目录> --output-dir <ComfyUI 输出目录>
uv run launch.py audit run <会话目录> --output-dir <ComfyUI 输出目录>
uv run launch.py preflight --input-frame <桥接帧.png> --prev-video <上一段.mp4> --session <会话目录>
uv run launch.py ref-concat <生成目录>                          # 多参：按段账重新拼成片
uv run launch.py ref-concat <生成目录> --replace 3 <新视频.mp4>  # 换掉第 3 段后再拼
```

`--model` 当前只接受 `fl2va`，`--mode` 只接受 `base`（旧选项保留兼容，不扩展路线）；路线由 brief 的 `输入` 键或向导选择决定。

## brief 格式

`examples/` 里有示例（模板 `examples/brief_template.md`）。**`输入` 键决定路线/参考方式**：

```markdown
输入: 首尾帧     # 首帧 | 尾帧 | 首尾帧 | 多参（ref2va）
时长: 6          # 秒（整数执行段）
风格: Cinematic 武侠电影质感
语言: Chinese

## 剧情
一段话描述创意……（核心输入）

## 参考图
# 首帧/尾帧=1 张；首尾帧=2 张（Picture1=首帧、Picture2=尾帧）；多参=N 张参考素材
# 有路径 → 向导里运行前用视觉模型读图自动出描述，人工确认后进管线（--brief 快路径不读图）
- Picture 1: 首帧 — 白发仙师在竹林缓抬手  (D:\Comfyui\ComfyUI\output\图片\你的图.png)
- Picture 2: 尾帧 — 青衫青年落地向仙师行礼

## 草稿提示词      # 可选；写了即进入 polish（润色）流程
```

参考图路径在 Windows 下写本机路径；在 WSL 下用 `/mnt/d/...`。图片会自动压缩（≤1024px）再送视觉模型。画布可写成 `画布 z_image: 736x1280`（画布键只认生图档表里的家族，当前登记为 `z_image` / `qwen` / `moss`；旧键 `zimage` 仍可读；裸 `画布: WxH` 写全部家族），缺省按配置。

## 架构

### 角色（17 个 `create_agent` 角色 + 直调写手）

| 阶段 | 角色 | 产出 |
|---|---|---|
| 前期 | 制片 Producer | 制作计划 |
| 前期 | 导演 Director | 导演阐述 |
| 编剧 | 编剧 Screenwriter | 分场剧本 |
| 美术 | 美术指导 Art Director | 统一美术统筹与冲突裁决 |
| 美术 | 人物/背景/道具设计师 | 三类设计正文（生图设计的文字真源） |
| AIGC | 资产图提示词工程师 Image Prompt Engineer | 人物/道具/场景的**资产图提示词**（仅多参、导演台 r2v 等产资产图的路线） |
| 视觉 | 分镜师 Storyboard | `[Shot N]` 镜头表 |
| 视觉 | 摄影指导 Cinematographer | 每镜画面细化 |
| AIGC | 关键帧生图提示词工程师 Frame Prompt Engineer | 首帧/尾帧生图提示词（仅要关键帧的路线） |
| 声音 | 声音设计师 / 配乐师 | 对白、环境声 / `non_diegetic_music` |
| AIGC | 参考资产与一致性经理 Reference & Consistency | `subject_definitions` 等（仅产资产图的路线） |
| AIGC | 可生成性审查员 Feasibility Reviewer | 镜头评审会 |
| AIGC | 提示词工程师 Prompt Engineer | 按官方规范组装最终提示词 |
| 质检 | 质检员 QA | 质检建议（有界精修循环） |

另有**直调写手**（不是 `create_agent` 角色，由代码拼请求）：起步澄清问句、分段规划师（base / ref / director 三种方言）、写段手（base / ref）、尾帧变换提示词写手。

### 主管线（阶段 1 共用，阶段 2 分叉）

```text
阶段 1（LangGraph 线性链，每个节点恰好一次；组合节点内并发）
制片 → 导演 → 创意圆桌 → 编剧
→ 设计三并行（人物‖背景‖道具）→ 美术统筹
→ [资产图提示词三并行]                       ← 仅产资产图的路线
→ 分镜 → [镜头评审会‖身份一致性会]
→ [摄影指导‖参考一致性]                      ← 后者的早期返回取决于路线
→ [关键帧生图提示词]                          ← 仅要关键帧的路线
→ [声音设计‖配乐] → 有界生图常识 QA

阶段 2（按路线分叉）
base：收帧图 → 读图 → base 三段式 → 质检精修 → 长片拆段与陪跑 → 成片
ref ：素材循环 → 注入卡 → ref 六段式 → 拆段/按需写段 → 陪跑 → 成片
director：帧网格段规划 → 导演包 → 逐段数帧验收 → 最终包交付
```

质检精修在 Python 层做**有界循环**：确定性校验 `h3_validator` + 主题守卫驱动，有 error 才让 `qa` 给建议、`prompt_engineer` 重出，最多 `max_qa_iterations` 轮。

## 配置

| 文件 | 管什么 |
|---|---|
| `config/agent.yaml` | 模型（`models.primary` / `models.vision`）、成本单价、`max_qa_iterations`、`roundtable_max_rounds`、`max_clarification_rounds`、`sessions_root`、导演台打包参数（画布/参考图尺寸/ComfyUI 输出目录/帧容差） |
| `config/workflow.yaml` | 多参登记工作流名与槽位数（V2：图 9 / 视频 3 / 音频 3）；`refs_per_segment`（默认 9，且不会超过官方 9 图上限） |
| `config/library.yaml` | 多参素材库根目录（默认 `D:\素材库`，必须是绝对路径） |
| `config/image.yaml` | 资产图三档生图模型（人物 `qwen`、道具/场景 `z_image`）＋关键帧档 `frame`（默认 `qwen`） |

API key 只放项目根 `.env`，不写 YAML、不入库。`provider` 不限于内置值：任何 OpenAI 兼容网关写网关名 + `base_url` + `api_key_env` 即可。⚠ Qwen-Image 2.1 是 Qwen Research License，**仅限非商用**；商用前把相关档切到 `z_image` 或 `moss`。

## 目录结构

```text
src/minimax_h3_prompt/
├── main.py                  # CLI 入口（向导 / --brief / 子命令）
├── config.py                # YAML + .env 统一配置
├── brief_parser.py          # brief 解析、路线与模式判据（唯一真源）
├── model_factory.py         # LLM 客户端、流式与等待判定
├── observability.py         # 进度、成本表、结构化日志
├── session_store.py         # 会话持久化与续跑状态
├── topic_generation.py      # 主题 → 提示词单入口（CLI 快路径）
├── generation.py            # 生成结果模型、提示词渲染与校验
├── summary.py               # 视频提示词中文摘要
├── agents/                  # 17 角色装配 + run_agent / run_direct_call
├── graph/                   # state / pipeline（阶段 1 链）/ nodes / roundtable
├── prompts/                 # 角色 system prompt 与直调写手提示词
├── references/              # 官方格式文本（给人读的对照副本）
├── knowledge/               # 电影语言词表（视频/生图/尾帧三份真源）
├── tools/                   # h3_validator / frame_auditor / preflight / theme_guard 等
├── ui/                      # 两阶段向导、进度与菜单（终端侧）
├── api/                     # 工作台服务端：app/routes/jobs/auth/spa/桩管线
├── workbench_store.py       # 工作台会话真源：快照、事件流、产物登记、单写入者
├── step_machine.py          # 工作台步骤/触发/闸门的单一声明与续跑起点
├── paths.py                 # 唯一根路径解析口（只读资源 vs 可写数据、冻结形态）
├── desktop.py               # 桌面外壳（WebView2 开本地服务页面）
├── output/                  # assembler（确定性格式保障）+ renderer
├── asset_ledger.py          # 素材台账与素材库解析
├── asset_loop.py            # 多参素材循环
├── asset_proofread.py       # 读图校对与偏差裁决
├── injection_card.py        # 多参注入卡
├── segment_planner.py       # 整数秒分段规划（base / ref 方言）
├── segment_prompts.py       # 写段（v2 规划式 + 回退式）与交付校验
├── segment_refs.py          # 段内参考素材子集与重编号
├── ref_segments.py          # 多参拆段、按需写段、陪跑账、成片
├── shot_ledger.py           # 首尾帧链段产出账与旧进度兼容
├── final_film.py            # 两条路线共用的成片拼接核心
├── director/                # 导演台路线：planning / prompts / pack / cache /
│                            #   ledger / companion / cascade / delivery / frames
├── image_config.py          # 三档资产图模型 + 关键帧档
├── image_routing.py         # 按张路由（文字类关键词 → MOSS、手动覆盖）
├── workflow_config.py       # 工作流登记与槽位
├── library_config.py        # 素材库根目录
└── task_package.py          # 受控导入
```

前端（`frontend/`）是独立的单包，不进 Python 包：

```text
frontend/
├── src/
│   ├── api/                 # client.ts（唯一 API 门面 + 错误归一）+ generated.ts（契约生成，勿手改）
│   ├── app/                 # 应用壳、主题（深/浅/跟随系统）、404
│   ├── components/ui/       # AppShell / Button / StatusBadge / MarkdownBlock 等通用件
│   ├── features/sessions/   # 列表页、详情页、按路线派生的分区、待办卡、步骤带推导（progress.ts）
│   ├── features/diagnostics/ # 诊断页（服务器健康 + 诊断包下载）
│   ├── router/              # 会话列表 / 会话详情 / 诊断（深链刷新由服务端 SPA fallback 承接）
│   ├── stores/              # 仅客户端 UI 状态（分区、主题）；业务状态不进 Pinia
│   ├── styles/              # tokens.css（设计令牌唯一真源）、reset、字体
│   └── test/                # 挂载工具、MSW server、会话夹具
├── e2e/                     # Playwright：13 条，桩管线跑三条路线闭环
├── scripts/                 # generate-api.mjs（契约 → TS）、serve-e2e.mjs（起真实 API）
├── dist/                    # 构建产物（工作台同源托管它；gitignore）
└── *.config.ts              # vite / vitest / playwright / eslint / tsconfig
```

前端约定：**设计令牌只住 `src/styles/tokens.css`**（与 [DESIGN.md](DESIGN.md) 同一份口径），组件里不写裸色值；**状态与文字的映射集中在一处**（`status.ts` 的七态、`progress.ts` 的步骤与待办文案）；**产物分组按契约 kind 派生**（`artifactKinds.ts`），不抄文件名白名单。

## 输出与会话目录

```text
output/
├── final_prompt.txt                 # --brief 快路径的最终提示词
├── stages/                          # 各角色/阶段产物（save_stages）
└── sessions/
    └── <主题slug>-<hash8>/GEN001/
        ├── session-state.json       # 工作台快照（workbench.v1）；CLI 会话同名文件是向导状态
        ├── events.jsonl             # 工作台事件流（只追加，seq 严格单调）
        ├── video-prompt.md          # 整片视频提示词
        ├── summary-zh.md / .json    # 中文摘要
        ├── assets/                  # 工作台上传素材（<sha256前16>.<ext> + .json 边车）
        ├── frame-rounds.jsonl       # 首尾帧链的人机修订台账
        ├── asset-ledger.json        # 多参素材台账（CLI 侧；工作台的台账住 session-state.json）
        ├── asset-rounds.jsonl       # 多参资产图改写轮次
        ├── injection-card.md        # 多参接线清单（多段另有 segments/injection-card-NN.md）
        ├── frames/                  # 首尾帧图与 source.json
        ├── bridge_frames/           # 首尾帧链的桥接帧（CLI 布局；工作台落 segments/bridge_frames/）
        ├── segments/                # 段表、段提示词、段产出账、闸门遭遇
        ├── director/                # 导演台：plan.json、段提示词、导演包、交付
        └── final.mp4                # 成片（base/ref 由系统拼；导演台只把路径记进 director/final-film.json）
```

根目录可用 `config/agent.yaml` 的 `defaults.sessions_root` 修改（默认 `output/sessions`）。工作台是**以文件为真源**的：一个会话一个目录，快照 + 事件流 + 产物登记都在目录里，服务重启后照旧能读。

## 测试与守卫

- `scripts/check.sh fast|full|clean`：结构守卫 + lint + 测试；`full` 另跑 mypy、全量测试、前端守卫（eslint/prettier/vue-tsc/vitest）、契约漂移与守卫变异，`clean` 摘掉本机 `.env` 模拟 CI 干净检出。
- `scripts/check.sh mutate`：守卫变异检验（关键实现改坏后用例必须变红）。
- `scripts/check.sh frontend`：只跑前端那一段（`frontend/` 有改动时 `fast`/`full` 会自动带上）。
- `tests/test_context_glossary.py`：`CONTEXT.md` 词条结构守卫。
- `tests/test_adr_guard.py`：`ADR NNNN` 引用必须指向真实文件，状态行格式守卫。
- `scripts/ac_trace.py`：按 `#NNN` 汇总 issue → 代码 → 测试（不是门禁）。

**前端测试分两层**：`frontend/src/**/*.spec.ts`（Vitest + MSW，24 个）覆盖组件与纯函数；`frontend/e2e/*.spec.ts`（Playwright，13 个）对着真实构建产物 + 真实 FastAPI 跑，桩管线（`MINIMAX_FAKE_PIPELINE=1`）让三条路线的闭环不花模型钱。**契约链**是 `FastAPI → scripts/export_openapi.py → contracts/openapi.json → frontend/scripts/generate-api.mjs → frontend/src/api/generated.ts`，`check.sh full` 与 CI 都逐字节比对，漂移即红。

## 文档地图

| 文档 | 内容 |
|---|---|
| [CONTEXT.md](CONTEXT.md) | **术语表 + 置顶的三路线流程总览**（开发前必读） |
| [DESIGN.md](DESIGN.md) | **前端设计系统**：令牌、线型语言（七态）、步骤带/波长轴、动效与无障碍约束 |
| [PRODUCT.md](PRODUCT.md) | **前端产品事实**：用户、产品目的、能力与约束、验证优先级 |
| `docs/adr/` | 架构决策（路线边界、成片归系统、帧网格、容量上限、前端技术栈与工作台形态等） |
| `docs/agents/frontend.md` | 前端工程实操：版本矩阵、本地命令、E2E 端口、EXE 打包与自检、踩过的坑 |
| `docs/agents/workbench.md` | 工作台会话/Headless API：模块地图、事件/写入/产物不变量、契约链、冻结形态 |
| `docs/design/` | 设计与根因分析（含导演台路线实现设计） |
| `docs/research/` | 调研笔记（电影语言、导演台插件原生格式等） |
| `docs/agents/` | 工程常态、issue tracker、triage 标签等 agent 规约 |
| `CHANGELOG.md` | 变更记录 |

## 已知限制

- **导演台只支持四种模式**：`v2v` / `rv2v` / `mixed` 已取消（#109／#110 not planned），菜单里没有它们；手写旧值会得到「模式无效」。
- **`--brief` 能力有限**：不建完整生成目录、不建素材台账、不陪跑、不支持导演台；需要这些用向导。
- **首尾帧链 ≤10 秒不出成片**：那条视频本来就是一次生成的结果。
- **成片实测时长只做「只报短」核对**：H3 产出从不是整数秒（4s 档恒 4.458s），严格相等会把真产物判成伪产物。
- **耗时与成本**：一次全流程是几十次 LLM 调用；ref 路线更重。可减少圆桌轮数、换更快模型；读图与外部生成成本另计。
- **工作时只服务本机单人**：服务只绑 `127.0.0.1`，另有 Host/Origin 白名单与每进程随机令牌（不进 URL、不进日志）；没有多用户、权限或团队协作。桌面 EXE 需要系统装有 WebView2。
- **画布与模型许可**：Qwen-Image 2.1 仅限非商用；换生图模型时要连同 `prompts/image_prompt_disciplines/` 的纪律块一起换。
