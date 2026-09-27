# GitHub 高星 AI 视频/生图提示词仓库普查

调研日期：**2026-09-26**（所有 star 数均为**截至 2026-09-26 经 GitHub search API 读取**；star 会变，引用时务必带上这个日期）
调研目的：按星数普查 AI 视频生成提示词 / 提示词模板类仓库，找出对本仓（MiniMax H3 视频提示词 + Flux.2/Z-Image 生图提示词）**真正可用**的东西。
方法：`gh`（v2.101.0，已在 PATH）与 GitHub search API，`sort=stars&order=desc&per_page=30`；JSON 先过 python 只抽 `full_name` / `stargazers_count` / `description` / `pushed_at` 四字段再打印；README 只 grep 结构与关键词窗口。
预算执行：**搜索 8/8 用满**（6 次成功，第 7、8 次撞未认证限流），**README 抓取 5/5 用满**。

---

## 1. 结论先行

1. **高星 ≠ 有货，而且形态高度同质化。** 星数榜前列几乎全是**「提示词画廊」**（prompt gallery）——一条一条的成品提示词配预览图，**没有术语表、没有结构公式、没有可复用的骨架**。最典型的是榜首 `YouMind-OpenLab/awesome-nano-banana-pro-prompts`（**13494★**，README 304KB、**873 个标题**）：已读确认，它的形态是「每条 = Description + Prompt + Generated Images + Details」的**图库**，`lighting` 出现 130 次 / `composition` 52 次**只是因为每条提示词里都会提到**，不是因为有词表。→ **这类仓库对本仓基本无用**（既无词表也无公式，且都是英文成品句，直接抄就是抄别人的创意）。

2. **真正有货的只有两类，且都不在星数最顶端。** ① **结构公式 + 词汇分节**类：`snubroot/Veo-3-Prompting-Guide`（**320★**）——已读，README 142KB、**206 个标题**，含 **8 要素公式 + 质量分级**、**`4.8 Master Camera Movement Library`**（Static/Pan/Tilt/Tracking/Dolly/Crane/Aerial/Angle Variations/Specialty 九节）、**`4.9 Advanced Shot Composition Mastery`**（内含 `Shot Sizes and Framing`）、**`4.10 Professional Lighting Mastery`**（Classic Setups / Mood / Natural 三节）。**三节标题已用 grep 逐一证实存在，景别/镜头/色彩词条已逐字取回（§2.5）**——它的 `Shot Sizes and Framing` 给了 **7 个景别**（EWS/WS/MWS/MS/MCU/CU/ECU），且其 `Medium Shot (MS)` 界定为 **`framing from waist up`**，**与 StudioBinder 标准一致**，可用来校验中文景别表那处映射偏差。② **带实测证据的模板库**：`LearnPrompt/awesome-seedance`（**1412★**）——已读，有 **25 个分类模板**（Structural foundations 3 / Realism 4 / Commercial 4 / Narrative 5 / Stylized 3 / Action 6），且**它的结构性模板正好是「按秒切时间轴 + 每段一个镜头类型 + 一个主动作 + 自己的声音线」**，与 H3 的 `At MM:SS.mmm` 段落结构**同构**。

3. **★ 本次最有价值的单点发现：`LearnPrompt/awesome-seedance` 里有 MiniMax H3 的实测复现率矩阵。** 原文表格：`MiniMax H3 Max 768p | 253 runs | 73%` 与 `MiniMax H3 768p | 11 runs | 82%`，判词汇总 `✅ 193 reproduced · ⚠️ 68 degraded · ❌ 3 failed`。这是**第三方独立跑出来的、针对本仓同一个模型的提示词复现率数据**，且该仓库自称「一条提示词跑一次是截图，跑两次还能复现才是方法」。→ 值得单独去读它的 `docs/templates/` 与 cross-model retests 明细（本次只读了 README，未读模板正文）。

4. **`smixs/visual-skills`（434★）的立场与本仓问题正面吻合。** 原文：`Model syntax is worth nothing until the dramaturgy is there. Editing, staging, camera, light, the objects allowed in frame — hard rules, all of them written into the skill.`（「戏剧构作优先，语法其次」）——这正是用户抱怨的「没有水平」的另一种说法：先有调度与光，再谈提示词。该仓库明确覆盖 **editing / staging / camera / light**，并援引 Murch 的剪辑理论。⚠️ 但它是**视频向**，其 camera/editing 部分**不能进本仓生图侧**。

5. **中文源有一个，且它自带「运镜词典」与「公式」章节。** `cclank/lanshu-awesome-ai-video-kit`（**405★**，v0.9.0 / 2026-05）：**543 条 prompt**（433 单模型 + 110 跨模型对照）、15 模型、7 个 Claude Skill、**21 篇方法论 SOP**；其方法清单里明确列有 **「运镜词典」**、**「约束词清单」**、**「分镜时序」**、**「情绪外化表」**、**「三家独立公式」（Kling 三套写法 + 6 守则 / 跨 5 模型对比 / Sora 2 Shot List / Veo 3.1 8 元素）**。中文术语对生图侧（要求中文正文）**天然省转译**。⚠️ 但它是**视频**工具包，`运镜词典` 属视频侧，需按 H3 的 13 词白名单过滤。

6. **一条系统性风险要写在这里**：本次普查到的所有提示词库，**没有一家是围绕 H3 的官方三段式格式**（`integrated_multimodal_description` + `overall_soundscape` + `non_diegetic_music`）组织的。它们绝大多数是 Seedance / Veo / Kling / Sora 的写法。→ **可借的是「词汇、分节、结构公式」，不可借的是「成品提示词」与「模型专属语法」**（尤其 Hailuo 系方括号 `[Truck left]`，本仓 `base-en.txt` 无此结构，**不可移植**）。

---

## 2. 主表（★ ≥ 300，均截至 2026-09-26 读取）

「读到 README」一列是本次的**实际**状态；标 ❌ 的**一律没有依据**，其内容描述只来自仓库自己的 description。

### 2.1 有货：结构公式 / 词表 / 模板（按对本仓价值排序）

| 仓库 | ★ | 一句话是什么 | 有没有模板或词表 | 视频/生图 | 读到 README | 对本仓可用性 |
|---|---|---|---|---|---|---|
| `snubroot/Veo-3-Prompting-Guide` | **320** | 自称 `GOOGLE VEO 3 MASTER PROMPTING GUIDE`，个人整理的 Veo 3 大而全指南（**非 Google 官方**） | ✅ **有，且已逐字核实**：8 要素公式表 + 质量分级；**`4.8 Master Camera Movement Library`（9 节）**、**`4.9 Advanced Shot Composition Mastery`**（内含 **`Shot Sizes and Framing`** 7 个景别 / `Advanced Framing Techniques` 4 条 / `Advanced Lens and Focus Effects` 7 词 / `Professional Color Palette Control` 7 词）、**`4.10 Professional Lighting Mastery`（3 节）**——三节标题均已 grep 证实存在，景别节词条已逐字取回（见 §2.5） | **视频** | ✅ 已读（142KB，206 标题）+ 三节真伪已核 | ✅ **本次唯一同时含「公式 + 景别 + 镜头/焦段 + 色彩 + 灯光」分节的外部源**；⚠️ 是 Veo 写法，落 H3 需改写；⚠️ 个人仓库、无出处标注、排版重度 emoji；⚠️ 它是「术语 + 示例句」而非纯词表，抄词时要自己剥出术语名 |
| `LearnPrompt/awesome-seedance` | **1412** | 自称 evidence-led 的 Seedance 提示词库：463 案例追溯到原帖 + 跨模型重测 | ✅ **有**：**25 个分类模板**（Structural foundations 3 / Realism & UGC 4 / Commercial 4 / Narrative 5 / Stylized 3 / Action 6）；核心结构模板是「**按秒切连续时间段，每段一个镜头类型 + 一个主动作 + 自己的声音线**」 | **视频** | ✅ 已读（101KB，23 标题） | ✅ **价值最高**：① 结构模板与 H3 的 `At MM:SS.mmm` 分段**同构**；② **含 MiniMax H3 实测复现率**（见 §1-3）；⚠️ 是 Seedance 生态，模型专属语法不可搬；⚠️ 模板正文（`docs/templates/`）**未读** |
| `smixs/visual-skills` | **434** | 「AI 电影导演」agent skill，主张 **dramaturgy first, syntax second**（戏剧构作优先，语法其次），援引 Murch 剪辑理论 | ✅ **有**（部分）：`formula` ×4、`vocab` ×2；覆盖 editing / staging / camera / light 的**硬规则** | **视频为主**（含 image skill） | ✅ 已读（16.7KB，12 标题） | ⚠️ **方法论立场与本仓问题最吻合**（先调度与光、再提示词）；但内容偏视频（camera/editing），**其运镜/剪辑部分不可进生图侧**；具体规则正文**未读** |
| `cclank/lanshu-awesome-ai-video-kit` | **405** | 中文企业级 AI 视频工具包：543 prompt + 15 模型 + 7 skill + 21 篇方法论 SOP | ✅ **有**：方法清单含 **「运镜词典」**、**「约束词清单」**、**「分镜时序」**、**「情绪外化表」**、**「三家独立公式」**（Kling 三套写法 + 6 守则 / 跨 5 模型对比 / Sora 2 Shot List / Veo 3.1 8 元素） | **视频** | ✅ 已读（14.4KB，19 标题） | ⚠️ **中文，省转译**；但属视频侧，`运镜词典` 需按 H3 的 13 词白名单过滤；「公式」是别家模型的，只可借结构。具体条目**未读**（55+ 文件 ~640KB） |
| `zenstory-ai/drama-skills` | **2276** | 开源 AI 短剧/漫剧创作 skill 合集：剧本、角色资产、分镜 storyboard、图片/视频提示词、审查 | ⚠️ 未知（未读） | 视频 + 生图（短剧全流程） | ❌ 未读 | ⚠️ **流程形态与本仓最像**（剧本→角色→分镜→图片/视频提示词→审查），值得一读；但没有任何已核实内容 |
| `OSideMedia/higgsfield-ai-prompt-skill` | **637** | Claude AI skill：32 个子技能，覆盖 Seedance 2.5 等的电影感提示词 | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 子技能结构可能含分节词表；未核实 |
| `beshuaxian/higgsfield-seedance2-jineng` | **864** | Seedance 2.0 × Higgsfield 技能集：15 个 Claude prompt skills（cinematic / 3D CGI / anim…） | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 未核实 |
| `jnMetaCode/ai-shortfilm-prompts` | **444** | Claude Code Skill：把任何想法变成「模型可用」的电影感视频提示词（Sora/Kling/Veo/Seedance） | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 未核实 |
| `popopo-99/zy-cinematic-realism` | **421** | 把简单场景想法转成克制、物理可信的电影感 AIGC 提示词 | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 「克制、物理可信」的取向与本仓「禁无中生有」相容，可留意 |
| `mediastormDev/dream-to-video-skill` | **363** | AI agent skill：把梦境描述转成电影感视频 | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 未核实 |
| `rediumvex/ai-video-generator-claude` | **388** | 10 个 Claude skills 生成 Seedance 2.0 的 studio 级提示词 | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 未核实 |
| `zhouwei713/seedance-prompt` | **323** | Seedance / 文生视频的真实感提示词 skill | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 未核实 |
| `Anil-matcha/awesome-seedance-2.5-api-prompts` | **318** | Seedance 2.5 API 指南 + 提示词 + 参数 + 示例 | ⚠️ 未知（未读） | 视频 | ❌ 未读 | ⚠️ 偏 API 用法，未核实 |

### 2.2 高星但形态＝提示词画廊（成品提示词集合，**无词表无公式**）

| 仓库 | ★ | 形态 | 视频/生图 | 读到 README | 对本仓可用性 |
|---|---|---|---|---|---|
| `YouMind-OpenLab/awesome-nano-banana-pro-prompts` | **13494** | **提示词画廊**：号称 10,000+ 条，每条 = Description + Prompt + Generated Images + Details（README 304KB / 873 标题） | 生图 | ✅ **已读并确认形态** | ❌ **基本无用**：无词表、无公式、无骨架；`lighting`/`composition` 高频只因每条提示词都提到。仅可当「别人怎么写」的语料，**不宜抄** |
| `YouMind-OpenLab/awesome-gpt-image-2` | **9951** | 同类提示词画廊（2000+ 条，带预览图） | 生图 | ❌ 未读（同上形态，未单独核实） | ❌ 同上（形态判断来自 description，**未读 README**） |
| `jamez-bondos/awesome-gpt4o-images` | **8154** | 同类：GPT-4o / gpt-image-1 的图片与提示词集合 | 生图 | ❌ 未读 | ❌ 同上，未核实 |
| `wuyoscar/GPT-Image2-Skill` | **5558** | GPT Image 2/2.5 提示词画廊 + agentic skill + CLI | 生图 | ❌ 未读 | ⚠️ 含 skill 部分，未核实 |
| `YouMind-OpenLab/awesome-seedance-2-prompts` | **2044** | 2000+ 精选 Seedance 2.0 视频提示词（cinematic/anime/UGC/ads/meme 风格） | 视频 | ❌ 未读 | ❌ 画廊形态，未核实 |
| `ZeroLu/awesome-seedance` | **2547** | Seedance 2.0 提示词与资源集合 | 视频 | ❌ 未读 | ❌ 画廊形态，未核实 |
| `jau123/MeiGen-AI-Design-MCP` | **1772** | 支持 GPT Image 2 / Seedance / ComfyUI，含 1400+ 提示词库的 MCP | 生图 + 视频 | ❌ 未读 | ❌ 偏工具，未核实 |
| `0aicoder0/Ultimate-ChatGPT-Image-and-Nano-Banana-Pro-Collection` | **495** | 247 条提示词 / 17 分类 | 生图 | ❌ 未读 | ❌ 画廊形态，未核实 |
| `YouMind-OpenLab/awesome-gemini-3-prompts` | **521** | 50+ 精选 Gemini 提示词（带图） | 生图 | ❌ 未读 | ❌ 未核实 |
| `githubssg/awesome-nano-banana-images` | **308** | 同 `jamez-bondos` 的镜像式集合 | 生图 | ❌ 未读 | ❌ 未核实 |

### 2.3 已排除（**不是提示词库**，按「排除纯 SDK/API 封装与应用」规则）

| 仓库 | ★ | 排除理由 |
|---|---|---|
| `wide-trace/open-higgsfield` | 3752 | 图/视频生成**工作室应用**（调模型的） |
| `IgorShadurin/app.yumcut.com` | 885 | 免费 AI 视频生成**应用** |
| `myccarl/ai-shortVideo-pipeline` | 680 | **生产流水线**（FastAPI + Spring Boot 编排） |
| `Blizaine/Maestro` | 632 | 本地 AI 影音**工作室应用** |
| `aqm857886159/Nomi` | 523 | AI 视频**工作台**应用 |
| `crisng95/flowboard` | 448 | 无限画布**应用** |
| `NickPittas/DirectorsConsole` | 331 | Prompt 生成 + ComfyUI 连接的 **Web 应用** |
| `LuqP2/Image-MetaHub` | 322 | ComfyUI/A1111 图片**管理应用** |
| `rockbenben/img-prompt` | 360 | 点选标签生成英文提示词的**工具**（标签工具，非库） |
| `bytedance/Video-As-Prompt` | 454 | 学术**代码**（ICLR 2026 论文官方实现） |
| `TencentARC/DiTCtrl` | 325 | 学术**代码**（CVPR 2025） |
| `Vchitect/VideoBooth` | 310 | 学术**代码**（CVPR 2024） |
| `TheDesignFounder/DreamLayer-Eval` | 412 | 扩散模型**评测框架**（生成提示词做 benchmark，非提示词库） |
| `ShadowHackrs/Jailbreaks-GPT-Gemini-deepseek-` | 1413 | **越狱提示词**集合，与本研究目的无关，且不宜引入 |
| `xing61/zzz-api` | 985 | API 代理服务 |
| `zszszszsz/.config` / `chrisneagu/FTC-Skystone-...` / `haochenheheda/segment-anything-annotator` | 359 / 313 / 386 | **搜索噪声**（OpenWrt 配置、FTC SDK、标注工具，与提示词无关） |

### 2.4 补跑搜索词的新增结果（2026-09-26，`gh` 认证后重跑）

上一轮第 7、8 个搜索词因未认证限流失败，本轮用**已认证** `gh api` 重跑（`gh` 确认为 v2.101.0，账号 `123456RRRRRRR` 已登录）：

- **`prompt template video`（total 151）**：≥300★ 的 4 项（`LearnPrompt/awesome-seedance` 1412、`IgorShadurin/app.yumcut.com` 885、`OSideMedia/higgsfield-ai-prompt-skill` 637、`jnMetaCode/ai-shortfilm-prompts` 444）**全部已在上一轮主表中**，**无新增**。
- **`awesome video generation`（total 175）**：≥300★ 的 13 项里，除已收录的两个 Seedance 画廊外，**新增的全是学术论文列表**，不是提示词库 → 归入下表并**排除**。

| 新增（★ ≥ 300） | ★ | 排除理由 |
|---|---|---|
| `showlab/Awesome-Video-Diffusion` | 5789 | **论文列表**（curated list of recent diffusion models） |
| `leofan90/Awesome-World-Models` | 2025 | **论文列表** |
| `AlonzoLeeeooo/awesome-video-generation` | 785 | **论文列表** |
| `mayuelala/Awesome-Controllable-Video-Generation` | 774 | **论文/综述** |
| `jianzhnie/awesome-text-to-video` | 746 | **论文/综述** |
| `jokieleung/awesome-visual-question-answering` | 674 | 主题不符（VQA，非提示词） |
| `wendell0218/Awesome-RL-for-Video-Generation` | 591 | **论文列表** |
| `YingqingHe/Awesome-LLMs-meet-Multimodal-Generation` | 552 | **论文列表** |
| `ziqihuangg/Awesome-From-Video-Generation-to-World-Model` | 519 | **论文列表** |
| `Winn1y/Awesome-Human-Motion-Video-Generation` | 345 | **论文/综述** |
| `minnie-lin/Awesome-Physics-Cognition-based-Video-Generation` | 327 | **论文列表** |

→ **结论：这两个搜索词对本仓没有新增可用项。** `awesome video generation` 这个词在 GitHub 上被**机器学习论文综述**占满，与「提示词库」几乎不重叠——**以后不必再跑这个词**。

### 2.5 `snubroot/Veo-3-Prompting-Guide` 的逐字词汇（已核实，2026-09-26）

**核验结论**：`4.8 Master Camera Movement Library`、`4.9 Advanced Shot Composition Mastery`、`4.10 Professional Lighting Mastery` 三节标题 **grep 均命中**（各 1 次），断言**成立**。以下为该 README 的**逐字**内容（纯文本，保留原英文；示例句是 Veo 写法，术语名才是可借的部分）。

**A. Shot Sizes and Framing（7 个景别，每条＝术语 + 一句示例）**

| 术语（原文） | 示例句里的界定（原文） |
|---|---|
| `Extreme Wide Shot (EWS)` | `lone figure walking across the vast desert landscape, emphasizing isolation and scale` |
| `Wide Shot (WS)` | `family gathered around the dinner table, showing the warm, intimate setting` |
| `Medium Wide Shot (MWS)` | `detective examining evidence, showing both character and environment context` |
| `Medium Shot (MS)` | `framing from waist up for professional authority` ← **与 StudioBinder 标准一致（腰以上）** |
| `Medium Close-Up (MCU)` | `the artist's focused expression while painting` |
| `Close-Up (CU)` | `the character's eyes widening in realization` |
| `Extreme Close-Up (ECU)` | `the ancient key turning in the lock` |

⚠️ 该节**没有 `Cowboy Shot`、没有 `Establishing Shot`**，且是「术语＋示例句」形态、**不是定义表**。→ 与本仓已有的三处景别来源（Google S3 官方 12 词 / StudioBinder 阶梯 / `Wayhhow` 中文表）**互为补充**；这份的价值在于**它的 `MS` 界定与 StudioBinder 一致**，可用于校验 `Wayhhow` 中文表那处偏差（见同期报告 C12）。

**B. Advanced Framing Techniques（4 条）**：`Rule of Thirds`、`Leading Lines`、`Depth of Field Control`、`Rack Focus`（⚠️ `Rack Focus` 属**动态**焦点转移，生图侧勿用）

**C. Advanced Lens and Focus Effects —— `Lens Effect Keywords` 逐字 7 条**（原文带解释）：
```
"shallow depth of field" - Isolates subjects with beautiful bokeh
"deep focus" - Keeps everything in sharp focus
"rack focus" - Shifts focus dramatically between subjects
"soft focus" - Creates dreamy, ethereal looks
"macro lens" - Shows intricate tiny details
"wide-angle lens" - Expands perspective and space
"lens flare" - Adds cinematic light effects
```

**D. Professional Color Palette Control —— `Color Palette Keywords` 逐字 7 条**（原文带解释）：
```
"monochromatic" - Single color scheme for artistic unity
"vibrant colors" - High saturation for energetic feel
"pastel tones" - Soft, muted colors for gentle mood
"desaturated" - Reduced color intensity for serious tone
"sepia tone" - Vintage brown tinting for nostalgic feel
"cool blue palette" - Cold color scheme for modern/tech feel
"warm orange tones" - Warm color scheme for comfort/intimacy
```
另有一条示例句（原文）：`Cinematic color grading with warm orange tones in highlights and cool blue shadows, creating professional fi[lter look]` —— 即「暖高光 + 冷阴影」的双色分级写法。

⚠️ **本节 A–D 均为该个人仓库的整理，不是 Google 官方口径**；且示例句是 **Veo 提示词写法**，**不可整句照搬进 H3 正文**——可借的是**术语名与它们各自的界定**。

---

## 3. 星数 < 300 但内容特别相关的

**本节的 ★ 数来自搜索 API，同样截至 2026-09-26。**

### 3.1 已经读过正文、且确实有货的两个（详见同期报告 `2026-09-26-image-prompt-language-sources.md`）

| 仓库 | ★ | 为什么特别相关 | 读到什么程度 |
|---|---|---|---|
| `Rylaispirit/cinematic-video-prompt-skill` | **95** | **6 个结构化词表文件，约 700 条术语**——是本次全部调研（含 300★ 以上）中**最丰富、最成体系的静态画面词表**。见该报告 §3A-7 / §3A-7b | ✅ **6/6 文件全文读完** |
| `Wayhhow/ai-video-shot-prompt-skill` | **16** | **唯一的中文景别表**（12 条，免转译），见该报告 §3A-8；同时其 `keyword-library.md` 的方法论与本仓两条纪律正面冲突（C11） | ✅ 2 个关键文件全文读完 |

→ **结论：本仓最需要的三类东西（静态词表 / 景别表 / 中文术语），恰好都在 300★ 门槛以下。星数门槛会把它们全筛掉。**

### 3.2 搜索中出现、但**一个都没读过**的（100★ ≤ ★ < 300）

| 仓库 | ★ | description 原文要点 | 状态 |
|---|---|---|---|
| `jijiutong/ai-visual-director` | 175 | AI Visual Director Skill: storyboards, characters, scenes, cinematic shots… | ❌ README 未读 |
| `machina-exm/film-studio-skills` | 139 | 7 installable agent skills that run the pipeline behind $2M AI video productions | ❌ README 未读 |
| `CyberJ0605/cinematic-video-prompt-engineer-skill` | 119 | Codex skill：把剧情摘要转成电影感 AI 视频提示词，会诊断 sto[ry] | ❌ README 未读 |
| `wuwangzhang1216/DirectorSKILL` | 113 | Claude Code skill for AI filmmaking — shot lists, keyframe & video prompts | ❌ README 未读 |
| `beshuaxian/...` 之外的 `zhengzhentao86/reverse-video-prompt` | 53 | Codex skill：从参考视频反推并迭代 AI 视频提示词 | ❌ README 未读 |
| `gracech0322-cmd/seedance-2-prompt-library` | 46 | Seedance 2.0 提示词库 | ❌ README 未读 |
| `ai9app/AI-Cinematic-Prompt-Director` | 19 | 250 AI Cinematic knowledge base of Master camera movements, 3D design, and video effects | ❌ README 未读 |
| `luozhilzh/video-prompt-reverse` | 38 | 中文「视频反推提示词工程师」，声称掌握运动设计语言（运镜/时序/主体运动/物理与连续感） | ❌ README 未读 |

---

## 4. 对本仓的冲突提醒（按仓库问的「④ 冲突没有」汇总）

本仓硬约束（来自 `CONTEXT.md`、`base-en.txt:100-121`、`image_prompt_engineer.md`、`generation.py:25-28`）：

| 冲突面 | 本次普查中踩线的具体情况 |
|---|---|
| **H3 视频侧只认官方 13 词运镜白名单** | 所有 Seedance/Veo/Kling 提示词库的运镜写法（`dolly zoom`、`whip pan`、`crash zoom`、`orbit`、`snorricam`…）**都不在白名单内**；`Veo-3-Prompting-Guide` 的 `4.8 Master Camera Movement Library` 九节里大部分词属于此类——**只可读其分节思路，不可搬词**。另注意本仓有 `FAST_CAMERA_MULTI_BEAT_COEXIST` **error 级**禁令（快机位 + ≥3 动作节拍同段共存），外部「运镜更丰富」的建议一律先过这道闸门 |
| **生图侧禁任何运镜/时间/剪辑词** | `smixs/visual-skills` 的核心（editing / staging / camera）与 `cclank` 的「运镜词典」**整块不可进生图侧**；`LearnPrompt` 的结构模板含「时间轴/按秒切段」，属**视频侧**结构，**不能进静态图提示词** |
| **方括号 `[Truck left]` 类语法不可移植** | 本仓 `base-en.txt` 无方括号指令结构。Seedance 生态（`LearnPrompt`、`ZeroLu`、`YouMind-*`）与 Hailuo 系均有各自的模型专属语法标记，**一律不可移植** |
| **正文语言：视频侧英文 / 生图侧中文** | 300★ 以上的库**几乎全英文**（`cclank` 405★ 是少数的中文源）；抄词仍需按语言侧分别转译。生图侧可优先看中文源 |
| **两模型画布不同（zimage 640×1280 竖幅 / flux2 1024×1024 正方）** | 外部构图建议（rule of thirds / negative space 方向 / centered）**默认按宽幅或方幅写**，直接套到 zimage 的 1:2 竖幅上语义会变 |
| **「一条提示词直接抄」的风险** | 画廊类仓库（§2.2）本质是**别人的创意成品**。本仓 `prompt_engineer.md` 有「禁止无中生有」硬约束——抄外部成品提示词会直接违反「每个新增细节必须可溯源」 |

---

## 5. 未能核实项

1. **API 限流实情（已解决）**：首轮**搜索 8 次用满**，其中前 6 次成功，**第 7、8 次（`awesome+video+generation`、`prompt+template+video`）返回 `API rate limit exceeded for 103.172.81.244`** —— 走的是**未认证**请求。原因：`gh` 虽在 PATH（v2.101.0），但脚本按硬编码路径 `/c/Program Files/GitHub CLI/gh.exe` 判断未命中，**回退到了未认证 curl**。
   → **本轮已修复并重跑**：先用 `command -v gh` 探到 `gh`，`gh auth status` 确认**已登录**（账号 `123456RRRRRRR`，token `gho_***`），再用 `gh api` 重跑那两个词，**两次均成功**。新增结果见 §2.4：`prompt template video` **无新增**；`awesome video generation` 的新增项**全是学术论文列表**，已排除。
2. **README 只读了 5 个（首轮）+ 1 个复核（本轮）**：`snubroot/Veo-3-Prompting-Guide`、`LearnPrompt/awesome-seedance`、`smixs/visual-skills`、`cclank/lanshu-awesome-ai-video-kit`、`YouMind-OpenLab/awesome-nano-banana-pro-prompts`。**§2 表里其余所有仓库的「有没有模板或词表」一栏都是未知**（标 ⚠️），内容描述仅来自仓库自己的 description，**不得当事实使用**。
3. ~~`Veo-3-Prompting-Guide` 的词汇正文未逐条抄录~~ → **本轮已解决**：三节标题 **grep 证实存在**，`Shot Sizes and Framing`（7 景别）、`Advanced Framing Techniques`（4 条）、`Lens Effect Keywords`（7 词）、`Color Palette Keywords`（7 词）**已逐字取回，见 §2.5**。仍未取回的是 `4.8 Master Camera Movement Library` 的 9 节正文与 `4.10` 灯光三节正文（**未读**）。
4. **`LearnPrompt` 的 H3 数据只来自 README 摘要**：`MiniMax H3 Max 768p 253 runs 73%` / `MiniMax H3 768p 11 runs 82%` / `✅193 ⚠️68 ❌3` 已逐字核对，但**其模板正文（`docs/templates/*.md`）与逐例明细未读**，无法判断那些模板是否真的适用于 H3 的官方三段式格式。
5. **`smixs/visual-skills` 的「硬规则」正文未读**：只读到其立场声明与 `formula`/`vocab` 的存在，**具体规则条目未知**。
6. **`cclank/lanshu-awesome-ai-video-kit` 的「运镜词典」与「三家独立公式」正文未读**：只在 README 的方法清单里见到条目名；55+ 文件 ~640KB 未展开。
7. **本次未做的筛选**：没有按 `pushed_at` 过滤活跃度（字段已取但未用于筛选）；没有查 `topic:prompt-engineering` 等 topic 维度；**没有做生图侧「静态词表」的专项搜索**（`image prompt library` 只跑了 1 次，返回的全是画廊类）。
8. **本文不含任何凭记忆写的 star 数或仓库内容**。所有 ★ 均为 2026-09-26 经 API 读取；所有内容描述要么逐字来自 description，要么标注为「已读 README」，未读的一律标 ❌/⚠️。

### 下次建议（按性价比排序）

1. **读 `snubroot/Veo-3-Prompting-Guide` 的 `4.8` 与 `4.10` 正文**——`4.8 Master Camera Movement Library` 九节与 `4.10` 灯光三节（Classic/Mood/Natural）**已在同一次抓取里，只是本次未展开**，是**零边际成本**的下一步（该文件已在本地抓过一次，重抓 1 次即可）。⚠️ 但要记得：`4.8` 的运镜词多数不在 H3 的 13 词白名单内，**只可读分节思路**。
2. **读 `LearnPrompt/awesome-seedance` 的 `docs/templates/`**——25 个模板正文，重点看「Structural foundations」那 3 个，并核对其 H3 复现率数据的口径。
3. 读 `cclank` 的「运镜词典」与中文术语条目（中文源省转译）。
4. **`awesome video generation` 这个词以后不必再跑**（已被 ML 论文综述占满，与提示词库几乎不重叠，见 §2.4）。
