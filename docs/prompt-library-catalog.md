# AI 提示词库与提示词模板 · 资源总表

截至 **2026-09-26**（星数 / installs 均为当日读数，会变）。
本文件是**资源目录**：哪些库/模板存在、里面有什么、可信度几档、对本仓能不能用。

配套文件分工（别混）：
- **怎么写** → `docs/film-language-playbook.md`（我落笔前的执行手册：词表、公式、硬约束、自查）
- **证据链** → `docs/research/2026-09-26-film-language-prompt-sources.md`（视频侧）、`…-image-prompt-language-sources.md`（生图侧）、`…-github-prompt-repos.md`（GitHub 普查）
- **本文件** → 资源在哪、什么货、能不能用

可信度分档（贯穿全表）：**🟢 官方一手**（模型方自己发布）＞ **🟡 已核实**（打开原页逐字读到，但非官方）＞ **⚪ 转述**（只在搜索索引里见过）＞ **🔴 未核实 / 已证伪**（不得当事实用）。

---

## 一、🟢 官方一手（最高优先，先看这些）

| 资源 | 位置 | 里面有什么 | 对本仓 |
|---|---|---|---|
| **MiniMax H3 官方规范（base 模式）** | 仓内 `src/minimax_h3_prompt/references/base-en.txt` | 运镜三维表（13 motion type + 幅度 + 速度）、切镜/跨切音频措辞、切点整秒与 4-10s 整数约束、edge-stability 收尾句 | **格式唯一依据**，只读不改 |
| **MiniMax H3 官方规范（ref 模式）** | 仓内 `references/ref-en.txt` | 六段式结构；`:7` 的「每镜必须确立什么」六要素清单 | 同上 |
| **MiniMax 官方 skills 仓库** | GitHub `MiniMax-AI/skills` → `skills/frontend-dev/references/asset-prompt-guide.md` | **生图口径**：构图 `left-aligned subject with negative space on the right for text overlay`；光线 `soft studio lighting` / `golden hour backlight` / `flat diffused light`；规格 `4K resolution, sharp focus, shallow depth of field`；硬规则 `NEVER include text in image prompts` | 生图侧可直接用（正文译中文） |
| 同上 · 模板体例参考 | `skills/gif-sticker-maker/assets/image-prompt-template.txt` | 「**分块 + 每块带理由**」的模板写法（`Style:` / `Subject handling:` / `Caption rendering (CRITICAL — follow exactly):`） | 可作模板体例 |
| **Black Forest Labs · Flux.2 文生图** | `docs.bfl.ai/flux_2/flux2_text_to_image` | **结构化 JSON 提示词**：`subject` / `background` / `lighting` / `style` / `camera_angle` / `composition`；**hex 精确锁色** | 与本仓现有的 JSON 输出结构对接，近乎零改动 |
| **BFL 官方 · FLUX 3 术语 cheatsheet** | `docs.bfl.ai/guides/prompting_video_camera_terms` | 静态可用五类：**角度 15 / 构图 9 / 焦点 6 / 镜头 9 / 光线 11**（每条带官方定义）+ 画幅 4 / VFX 12 / 动画 4；视频向四类（运动 18 / 时间 10 / 转场 8 / POV 4）**生图勿用** | ⚠️ 是 FLUX 3 不是 Flux.2；⚠️ **无景别** |
| **Google Veo 提示词指南** | `docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/video-gen-prompt-guide`（原 URL 已 301） | `Anatomy of a prompt` 九段结构；Camera angles 表（12 词含景别）；Camera movements 表；镜头词（`wide-angle` / `telephoto` / `bokeh` / `deep depth of field`）；**cinematic terms**（`match cut` / `jump cut` / `montage` / `split diopter`）；**Negative prompts**（**正向措辞**：别写 `no`/`don't`，要排除的写名词）；两处 `not officially supported` 免责声明 | 视频侧可借结构；⚠️ 该页自己声明部分进阶机位/镜头**非官方支持** |

---

## 二、🟡 已核实的外部库（打开原页逐字读到）

| 资源 | 规模（2026-09-26 读数） | 里面有什么 | 用法与坑 |
|---|---|---|---|
| `Rylaispirit/cinematic-video-prompt-skill` | **95★**，`references/` **6 个文件、约 700 条**（推荐语称 700+，**成立**） | 两列表 `\| Prompt (EN) \| Giải thích (VI) \|`：光线 83 / 构图 68 / **镜头·技术·胶片质感 151** / 风格·色彩·情绪 212 / 材质约 90 / 角度+景别 70 | **本仓最丰富的静态词库**。⚠️ 选词池，不是模板；⚠️ **正文要译成中文**；⚠️ `04` 的「快门与运动」整节、`01` 的 7 条动态项**剔除**；⚠️ `8K/4K/hyperdetailed` 撞禁质量标签（但 `hyperrealistic` 可用） |
| `LearnPrompt/awesome-seedance` | **1412★**，已读 | **25 个分类模板**；核心结构＝「按秒切连续时间段，每段一个镜头类型 + 一个主动作 + 自己的声音线」，**与 H3 的 `At MM:SS.mmm` 分段同构**；含 **MiniMax H3 第三方实测复现率**（`H3 Max 768p 253 runs 73%`、`H3 768p 11 runs 82%`，判定 ✅193 / ⚠️68 / ❌3） | 骨架级模板可套；H3 那组数字是**第三方对同一模型的独立数据** |
| `snubroot/Veo-3-Prompting-Guide` | **320★**，已读 | 8 要素公式 + 质量分级；分节含 `4.8 Master Camera Movement Library`、`4.9 Advanced Shot Composition Mastery`（内有 **7 个景别** EWS/WS/MWS/MS/MCU/CU/ECU）、`4.10 Professional Lighting Mastery`；另构图 4 条、镜头关键词 7、色彩关键词 7 | 二手转录 Veo 口径。**它的 `MS = waist up` 与 StudioBinder 一致**，可校验他表偏差 |
| `Wayhhow/ai-video-shot-prompt-skill` | **16★** | **唯一的中文景别表**（12 条，带「描述 + 适用」）+ `references/keyword-library.md` | ⚠️ **取词不取法**：`keyword-library.md` 强制 `超写实`/`极致逼真` 等质量词 + `杜绝…` 负向祈使句，**同时撞本仓两条纪律，且与 Google 官方口径正相反**；⚠️ 其中英映射与工业惯例有偏差（`MS` 写成胸部以上） |
| StudioBinder（`ultimate-guide-to-camera-shots` / `types-of-camera-shots-sizes-in-film`） | 工业教育站，非标准制定方 | 景别阶梯 `EWS→ECU`（含 `Cowboy Shot`、`Establishing Shot`）+ 逐条定义；机位角度；运镜清单（11 项） | 景别英文映射的**基准之一** |
| Runway `runway.com/resources/ai-camera-prompts` | 官方资源站 | **七格排序公式** `[Shot size] + [Angle] + [Movement+speed] + [Subject & action] + [Lens/look] + [Lighting/mood] + [What the shot reveals]`；机位-主体**用动词写明**；多阶段运镜写「每阶段画面里有什么」；double-directing 失败模式；**静态加固句**；术语表（景别/角度/焦点/构图/运镜） | 写法纪律价值最高。⚠️ 它**鼓励组合运镜**，与本仓「快机位禁令」**维度不同、不可互证** |

---

## 三、高星但**没货**的（避免重复踩坑）

星数高 ≠ 有用。以下均 ≥2500★，**形态是提示词画廊**（一条 = 描述 + 提示词 + 图），**没有词表、没有公式**：

| 仓库 | ★（2026-09-26） | 判定 |
|---|---|---|
| `YouMind-OpenLab/awesome-nano-banana-pro-prompts` | **13494** | **已读确认没货**（873 个标题，纯画廊） |
| `YouMind-OpenLab/awesome-gpt-image-2` | 9951 | 未读，同形态 |
| `jamez-bondos/awesome-gpt4o-images` | 8154 | 未读，同形态 |
| `wuyoscar/GPT-Image2-Skill` | 5558 | 未读 |
| `ZeroLu/awesome-seedance` | 2547 | 未读，画廊形态 |

**一个死路搜索词**：`awesome video generation` —— 结果被**机器学习论文列表**占满（`showlab/Awesome-Video-Diffusion` 5789★ 之类），与提示词库几乎不重叠，**以后不必再搜**。

**关键判断**：本仓最需要的三类东西（静态词表 / 景别表 / 中文术语）**全在 300★ 门槛以下**——按星数筛会**全部漏掉**。

---

## 四、Agent Skills 生态（用 `/find-skills` 查到，2026-09-26 读数）

| # | Skill 名称 | installs | 来源 | 用途 | 判断 |
|---|---|---|---|---|---|
| 1 | **`minimax-ai/minimax-h3@h3-prompt-writing`** | **9.2K** | **MiniMax 官方组织** | `Write MiniMax H3 video generation prompts for T2VA, I2VA, FL2VA, L2VA, and Ref2VA` | **同族官方**；**本会话已加载**，无需再装 |
| 2 | **`samuraigpt/generative-media-skills@muapi-cinema-director`** | 2.9K | 第三方 | `translates creative intent into technical cinematographic directives for Veo3, Kling, and Luma` | 与「电影语言→技术指令」同题；⚠️ 目标是 Veo3/Kling/Luma，措辞需过滤 |
| 3 | **`replicate/skills@prompt-videos`** | 1.9K | **Replicate 官方（厂商）** | `Prompting techniques for AI video generation models on Replicate` | 厂商官方通用技法，可作旁证 |
| 4 | `dexhunter/seedance2-skill@seedance-prompt-en` | 2.8K | 第三方 | `Write effective prompts for Jimeng Seedance 2.0 multimodal AI video generation` | 即梦 Seedance，结构可借；⚠️ 模型不同，语法别照搬 |
| 5 | **`agentara/skills@video-storyboard`** | **772** ⚠️ | 第三方 | `Generate storyboard image boards and matching video-generation prompt scripts` | 功能最贴本仓（**分镜 → 提示词脚本**）；⚠️ **install 低于 1K，按 find-skills 的标准应谨慎** |

**点名要装的 3 个**（第 2、3、5 条）：

```
samuraigpt/generative-media-skills@muapi-cinema-director
replicate/skills@prompt-videos
agentara/skills@video-storyboard
```

安装命令（联网安装按本项目规矩**由用户本人执行**）：

```bash
npx skills add samuraigpt/generative-media-skills@muapi-cinema-director -g -y
npx skills add replicate/skills@prompt-videos -g -y
npx skills add agentara/skills@video-storyboard -g -y
```

**两个名字像、其实不对口的（已筛掉）**：
- `vincentwei1021/video-shotcraft@video-shotcraft`（2.6K）——描述验出是 **Remotion 代码生成产品宣传片**（`shot recipe cards... Remotion + real page screenshots`），**不是提示词写作**。
- `nidhinjs/prompt-master@prompt-master`（3K）——通用提示词工程，不专攻影视。

**生图侧：skill 生态里没有对口的。** 命中的全部**绑定别的模型**（`gpt-image-2` 20.3K、`baoyu-image-gen` 32.9K、`nano-banana-pro` 10K）；本仓用 **Flux.2 + Z-Image**，只能回到第一节的 BFL 官方 + 第二节的词库。

---

## 五、🔴 未核实 / 已证伪（**不得当事实使用**）

| 条目 | 状态 |
|---|---|
| `cinematique.ai` 与 `cinematique.io` | **两个 TLD 都 SSL 握手失败**；原始清单未给 URL → 既未证实也未证伪 |
| `invideo.io` AI 视频提示词指南 | 路径 **404**，`sitemap.xml` 里无任何含 `prompt` 的 URL，正确路径未定位 |
| OpenAI Sora 2 提示词指南（Cookbook） | **403**（备用主机 SSL 失败）→ 第一版关于 Sora 的全部内容**作废** |
| Runway Gen-4 官方指南 | **403** → 其「正向措辞」「不要复述输入图」**未经核实**（但「正向措辞」已被 Google 官方独立证实） |
| `ai.google.dev/gemini-api/docs/veo` | 超时 |
| 「Veo 有五段式公式 `[Cinematography]+[Subject]+…`」 | **已证伪**（原页 NO MATCH；该页结构是九段 `Anatomy of a prompt`） |
| 「`macro lens` / `two-shot` 出自 Google 官方表」 | **已证伪**（`macro lens` 实为 Runway 术语表） |
| 「Hailuo 支持中文方括号指令 `[左移]`、可组合 ≤3 个」 | **未证实**；官方表只有 15 条英文指令且无 `[Follow]`。且方括号语法**不可移植到 H3** |
| 「BFL 建议一个镜头只用一个主要运镜」 | **未证实**（两页内 NO MATCH） |
| 原始清单里的「150+ 摄影技法」等数字 | **未证实**（「700+ 术语」已成立，见第二节） |
| Flux.2 是否支持负向提示词 | **未证实**（BFL 两页均未提） |

**清单可信度总评**：那份外部清单**不是编的**（仓库名全部真实存在），但**数字与术语举例大部分未获证实**，且有**张冠李戴**（`teal-and-orange` / `anamorphic` 实际在 `cinematic-video-prompt-skill`，清单归给了 `video-prompt-reverse`）。**引用前必须回到原页。**

---

## 六、引用纪律（本仓硬约束过滤，冲突时以本节为准）

用任何外部库之前，先过这几关：

**视频侧（H3）**
- 运镜词**只在官方 13 个 motion type 白名单内**（`base-en.txt:100-121`）；`crane` / `handheld` / `orbit` / `crash zoom` / `whip pan` 都不在
- **不许句尾堆标签**，要写成镜头内的自然英文动作（`base-en.txt:123`）
- **切点整秒、每镜 4-10 秒整数**，所以「多切/快切」类建议**不可用**
- **快机位 + ≥3 动作节拍同段共存 = error**；处置是降机位；引用该规则**必须声明阈值量于 4 步采样**
- **Hailuo 的方括号 `[Truck left]` 语法不可移植**（属另一产品线，官方模型表里没有 H3）

**生图侧（Flux.2 / Z-Image）**
- **禁一切运镜、时间、剪辑、时间码词**（`image_prompt_engineer.md:19`、`frame_prompt_engineer.md:72`）
- **禁堆无意义质量标签**（`:20`）——⚠️ 这是**本仓纪律**不是模型限制（BFL 官方示例里就有质量词），别因为官方这么写就放宽
- **正文中文**；两模型**必须分别重写**；**画布不同**（zimage 640×1280 竖幅 / flux2 1024×1024 正方）→ 构图词分两套
- **景别英文缩写**以 Google / StudioBinder 映射为准（中文表那套的中英对应有偏移）

**通则**：🟢 官方一手 ＞ 🟡 已核实 ＞ ⚪ 转述；**🔴 未核实的一律不用**；任何一条外部建议**与官方规范冲突时，官方赢**。
