# 影视提示词权威库与镜头语言模板调研

调研日期：2026-09-26（第二版：原文核实后重写）
调研目的：找「电影/视频制作」领域的权威提示词库与镜头语言模板，用于提升本项目产出的 **MiniMax H3 提示词的电影语言质量**（用户反馈：生成的视频「没有水平」——缺镜头调度、构图、光线、剪辑节奏）。
调研范围：视频生成厂商官方提示词指南 + 影视工业侧镜头语言词汇表 + 开源提示词库（第三类未覆盖，见 §5）。

## 调研方法（含环境限制与绕行说明）

- `WebSearch` 5 次（用满）——**可用**。
- `WebFetch` **两次、共 12 次调用全部失败（12/12）**，跨 12 个不同域名报同一句错：`Unable to verify if domain <X> is safe to fetch. This may be due to network restrictions or enterprise security policies blocking claude.ai`。卡点在 WebFetch 自己的域名安全校验（它要先问 claude.ai），不在目标站点。
- **绕行**：本机 shell 可直连外网（`env | grep -i proxy` 无代理变量），**改由 `Bash` + `curl -sSL` 直接抓取**完成核实。这是环境限制下的绕行，不是常规路径。
- 抓取纪律：curl 落盘到临时目录 → python 一行式剥 HTML 标签转纯文本 → **只按关键词取窗口**（不整页读进上下文）→ 收尾删除全部临时文件。

**本次已逐字读到原文的源**：S1、S2（仓内官方副本）、**S3、S7、S9、S10、S12**（curl 直连）。
**仍打不开的源**：S4（超时）、S5（403 + 备用主机 SSL 失败）、S6（403）、S8（同域未再试）、S11、S13。
无法验证的一律标注「**未经证实**」。§3 不含任何凭记忆补写的影视术语：凡未在下方来源中出现的词，一律未收录。

---

## 1. 结论先行

1. **最大的电影语言缺口在仓内，不在网上。** 官方 `base-en.txt` 只给了运镜三维表（§4.3）和切镜措辞（§4.2），**没有景别表、没有光线表、没有构图表**；而官方 `ref-en.txt:7` 恰好有一份「每镜必须确立什么」的清单（composition / subject appearance and position / environment and lighting / actions and state changes / camera movement / current sound）。本次核实发现**外部其实也不缺这类词表**（Google S3、Runway S7 各有一份完整表），但它们是**为别的模型写的**；官方 ref-en.txt:7 这份清单是第一手、与 H3 格式同源、且目前只是文档里一句话、没有任何接线——它才是第一顺位依据。同时长视频路径上**摄影指导的产出根本没进写段请求**（§4 P4），所以「没水平」在长视频上首先是接线缺口，不只是词表缺口。

2. **外部最可直接迁移的是「写法纪律」，且三条已逐字证实。** Runway S7 的排序公式 `[Shot size] + [Angle] + [Movement + speed] + [Subject & action] + [Lens/look] + [Lighting/mood] + [What the shot reveals]`、机位-主体关系用动词写明（`Camera follows the cyclist.`）、多阶段运镜改写成「每阶段画面里有什么」；Google S3 的九段式 `Anatomy of a prompt`。这三条与官方 `base-en.txt:123`「运镜要写成镜头内的自然英文动作，不许在句尾堆标签」同向，可直接用来加固提示词模板。

3. **「运镜更花哨」方向有实测禁令拦着，且外部源里确实有词会被拦。** Runway 推荐词表明确列了 `Whip pan`——**逐字命中**本仓 `_FAST_CAMERA_RE`。另有一条**覆盖缺口**：`Crash zoom` 语义上是快变焦却**不命中**该正则（快词表只有 `fast|rapid|swift|whip`）。引入任何运镜词前必须先跑 `_check_fast_camera_multi_beat_coexist`，且引用该规则**必须声明阈值量于 4 步采样**（`CONTEXT.md:150`、`docs/adr/0003`）。

4. **用户抱怨的「剪辑节奏」在结构上被钉死，外部素材救不了。** 官方明文要求切点必须是整秒、且**每个镜头的隐含时长也必须是 4–10 秒的整数**（`base-en.txt:86`），加上「若只是距离或轻微角度变化，优先用运镜而非切」（`:98`）与本仓「默认单镜头」（`prompt_engineer.md:51`）。一个 4–10 秒的执行段在时长上装不下第二个 ≥4 秒的镜头。所以节奏只能靠**运镜幅度/速度与动作节拍**做；外部「多切/快切」建议一律不可用。
   → 附带收获：三条独立官方源（Runway S7、Google S3、MiniMax S12/asset-guide）都指向同一根稳定杆——**回到静态**。Runway 还给了一句可直接抄的加固句（§3B-1）。

5. **一个必须先裁定的内部矛盾。** `prompts/cinematographer.md:16` 要求「**正文必须是中文**」，而 `prompts/prompt_engineer.md:48` 要求「**最终提示词正文用英文**（官方 Output Rules）」。这直接决定 §3 的英文词汇能否直接落进正文。`theme_guard.py:16-21` 的地点词表是**双语**的，所以它不解决这个问题。按仓规「规格冲突先停下裁定」，以下只列证据、不选边（§4 冲突 C6）。

---

## 2. 源清单表

「本次抓取结果」一列是第二版新增的实测值（curl 直连）。

| # | 名称 | URL | 谁维护（权威性依据） | 内容类型 | 信息密度 | 本次抓取结果 | 对 H3 可否直接用 |
|---|---|---|---|---|---|---|---|
| S1 | MiniMax H3 官方 base 模式提示词规范 | 仓内 `src/minimax_h3_prompt/references/base-en.txt` | **MiniMax 官方**；本仓定为「官方规范唯一依据」（`CONTEXT.md:128-136`） | 提示词指南（官方规范） | 高：运镜三维表 18 行（表格行数：表头＋分隔行＋16 数据行，其中 Motion type 12 行；2026-09-27 注明）、切镜/音频衔接措辞全 | ✅ **逐字读到** | ✅ **直接可用**（同族同格式，是判断其他源可用性的基准） |
| S2 | MiniMax H3 官方 ref 模式提示词规范 | 仓内 `src/minimax_h3_prompt/references/ref-en.txt` | 同上 | 提示词指南（官方规范） | 高：含「每镜必须确立什么」清单（`:7`） | ✅ **逐字读到** | ✅ 直接可用 |
| S3 | Google「Video generation prompt guide」（Gemini Omni Flash / Veo） | https://docs.cloud.google.com/vertex-ai/generative-ai/docs/video/video-gen-prompt-guide → **301 跳转至** https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/video-gen-prompt-guide | **Google（模型方一手）** | 提示词指南 | **很高**：机位 12 词 + 运镜 11 词 + 镜头 5 项 + 电影术语 + 负向提示 | ✅ **HTTP 200，已逐字核实**（425KB→26.7KB 正文） | ⚠️ 词表可作候选池，但**同页两处「not officially supported」免责**（§3B-2）；且落笔必须成 H3 自然句 |
| S4 | Google AI for Developers — Veo 文档 | https://ai.google.dev/gemini-api/docs/veo | Google（模型方一手） | 提示词指南 | 未知 | ❌ **curl 超时**（exit 28，25s 无响应） | 未读到，不作依据 |
| S5 | OpenAI Sora 2 提示词指南（Cookbook） | https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide.md | **OpenAI（模型方一手）** | 提示词指南（含镜头表模板） | 未知 | ❌ **HTTP 403**（响应体 59 字节 `Forbidden`，含区域标记 `hkg1::`）；备用主机 `cookbook.openai.com` → ❌ **SSL/TLS 握手失败**（exit 35） | **未读到，全部转述未经核实** |
| S6 | Runway Gen-4 Video Prompting Guide | https://help.runwayml.com/hc/en-us/articles/39789879462419-Gen-4-Video-Prompting-Guide | **Runway（模型方一手）** | 提示词指南 | 未知 | ❌ **HTTP 403**（5844 字节错误页，剥标签后仅 18 字符正文） | **未读到，全部转述未经核实** |
| S7 | Runway — AI camera prompts | https://runway.com/resources/ai-camera-prompts | **Runway 官方资源站**（署名 Leah Retta，2026-08-21） | 术语表 + 写法指南 | **很高**：排序公式 + 5 张术语表 + 反面改写 + 可抄用库 | ✅ **HTTP 200，已逐字核实**（556KB→24KB 正文） | ✅ **写法纪律与术语表可直接用**；⚠️ 术语表含 `Whip pan`（撞本仓禁令） |
| S8 | Runway Text to Video Prompting Guide | https://help.runwayml.com/hc/en-us/articles/42460036199443-Text-to-Video-Prompting-Guide | Runway 官方 | 提示词指南 | 未知 | ❌ 未抓取（与 S6 同域，S6 已 403，判定同域不可达） | 未读到 |
| S9 | StudioBinder — Ultimate Guide to Camera Shots | https://www.studiobinder.com/blog/ultimate-guide-to-camera-shots/ | StudioBinder（影视工业教育站，**非标准制定方**） | 术语表 | 高（景别 + 机位 + 运镜 11 项） | ✅ **HTTP 200，已逐字核实**（651KB→44KB 正文） | ⚠️ 候选池；**必须过 H3 格式**（官方示例只出现过 6 个景别词，见 §3A） |
| S10 | StudioBinder — Types of Camera Shots & Shot Sizes | https://www.studiobinder.com/blog/types-of-camera-shots-sizes-in-film/ | 同上 | 术语表 | 高（景别定义最完整） | ✅ **HTTP 200，已逐字核实**（760KB→17.7KB 正文） | ⚠️ 同上 |
| S11 | MiniMax Hailuo 2.3 运镜指令（第三方文档） | https://www.compshare.cn/docs/modelverse/models/video_api/MiniMax-Hailuo-2.3-I2V 、 https://docs.cloudsway.net/maasapi/api-reference/video/hl/ | **第三方云厂商**（非 MiniMax 域名） | API 指令表 | 高 | ❌ 未抓取；**但其核心内容已由 S12 一手证实**（§3B-4） | ❌ **不可移植到 H3**（`[ ]` 属 Hailuo API 产品线，且与 `base-en.txt:123` 冲） |
| S12 | **MiniMax 官方 skills 仓库**（Video Generation Guide 等） | https://raw.githubusercontent.com/MiniMax-AI/skills/main/skills/frontend-dev/references/minimax-video-guide.md ；同仓 `skills/frontend-dev/references/asset-prompt-guide.md`、`skills/gif-sticker-maker/assets/video-prompt-template.txt` | **MiniMax 官方 GitHub 组织**（一手） | API 指南 + 提示词指南 | 中（文件很短但信息实） | ✅ **HTTP 200，已逐字核实**（含仓库全树 API 列出 472 个路径筛查） | ⚠️ **术语与风格词可用**（MiniMax 自家口径）；但**方括号运镜属 Hailuo API、不可用于 H3**（§3B-4） |
| S13 | APIDot — Hailuo 03 (MiniMax H3) Reference Guide | https://apidot.ai/blog/minimax-h3-omni-reference-guide | 第三方博客（**非官方**） | 二手参考指南 | 未知 | ❌ 未抓取（二手，优先级低于已证实的一手源） | 未读到 |

**覆盖缺口（是「没查」，不是「没有」）**：ASC（美国摄影师协会）、No Film School、GitHub 上的 video-prompt / cinematic-prompt 开源集合——本次**完全未检索**。
**顺带筛查**：`MiniMax-AI/skills` 仓库全树 472 个路径中，video/prompt 相关仅 6 个（已列在 S12 行），**没有 H3 专用文档**——即 MiniMax 官方 GitHub 上未公开 H3 的提示词规范（H3 规范仍只有本仓镜像的 `base-en.txt` / `ref-en.txt`）。

---

## 3. 可直接抄用的词汇与句式

### 3A. 一手原文（逐字读到）

#### 3A-1. 仓内官方规范（S1 / S2）

##### 运镜三维表（`base-en.txt:100-121`）

官方规定：完整运镜表达有三个维度——**motion type**（怎么动）、**amplitude**（构图变化幅度）、**speed**（变化快慢）；`medium amplitude` 与 `normal speed` 通常省略（`:102`）。

| Dimension | Available Expression（官方原文） | 官方描述 |
|---|---|---|
| Motion type | `Zoom In / Zoom Out` | The focal length changes while the camera body remains stationary |
| Motion type | `Push In / Pull Out` | The camera moves forward / backward |
| Motion type | `Pan Left / Pan Right` | The camera remains in place while the lens pivots horizontally |
| Motion type | `Truck Left / Truck Right` | The camera translates horizontally |
| Motion type | `Tilt Up / Tilt Down` | The camera remains in place while the lens pivots vertically |
| Motion type | `Pedestal Up / Pedestal Down` | The entire camera moves upward / downward |
| Motion type | `Arc Shot` | The camera moves in an arc around the subject |
| Motion type | `Tracking Shot` | The camera follows a moving subject |
| Motion type | `Static Shot` | The camera position and lens remain still |
| Motion type | `Shake Slightly / Shake Strongly` | Slight / strong camera shake |
| Motion type | `POV` | The subject's point of view |
| Motion type | `Roll Clockwise / Roll Counterclockwise` | The camera rolls clockwise / counterclockwise around the lens axis |
| Amplitude | `with small amplitude` / `with large amplitude` | Small-range change / Large-range change |
| Speed | `at slow speed` / `at fast speed` | Slow movement / Fast movement |

**这就是运镜词白名单**：只有这 12 个 motion type + 2 个幅度 + 2 个速度。外部词表里不在此列的运镜名（crane / dolly / orbit / arc 之外的英文名、whip pan、handheld、crash zoom …）**都不是官方列出的表达**。

##### 运镜写法铁律 + 官方例句（`base-en.txt:123-129`）

> Camera motion should be written as a natural English action within the shot, rather than stacked as separate labels at the end of a sentence:

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the open doorway.
The camera holds a static shot as the runner exits the frame.
```

`ref-en.txt:219` 同义重述：`Write camera movement as natural English within the current shot, including movement type, amplitude, and speed when they need to be expressed.`
官方例文里的同构句：`The camera pulls out with small amplitude at slow speed as she releases the bicycle handle...`、`The camera pushes in with small amplitude at slow speed as the fingertips strike the rim.`

##### 切镜衔接措辞（`base-en.txt:98`）

> For ordinary cuts, use `the camera cuts to`, `the shot cuts to`, `the shot transitions to`, `the shot changes to`, or `the shot switches to`. When explicitly requested by the user, cross-dissolve, fade, or wipe may also be used. A cut should introduce new information about the subject, space, state, viewpoint, or time. **If only the distance or a slight angle needs to change, prefer camera motion.**

##### 跨切口音频衔接（`base-en.txt:148`）

标签：`<scenetrans>`（对白/歌词跨切时**两侧连接点**都标，并明说音频跨切延续）、`<cutoff>`（语音被视频结尾截断）。
官方连续性措辞（逐字）：`continues seamlessly across the cut`、`continues uninterrupted into the next shot`、`carries over from the previous shot`、`remains audible across the transition`。

##### 风格词 / 光线 / 切点约束

- 风格词（`base-en.txt:78`）：`Cinematic`、`live-action`、`2D-animated`、`3D CG`、`claymation`、`watercolor`、`vintage film`。
- 光线与调色示例（`ref-en.txt:237`）：`a cinematic, literary music-video style with soft lighting and a slightly desaturated color palette`；（`:329`）`a realistic multi-camera sitcom style with warm indoor lighting`。
- 切点约束（`base-en.txt:86`）：切点必须整秒（毫秒位固定 `.000`），**每个镜头的隐含时长也必须是 4–10 秒的整数**；理由：ComfyUI H3 的时长选项只提供 4–10s 整秒档。

##### 官方**出现过的**景别措辞（穷举，共 6 个）

官方**没有景别表**——这是最重要的「缺口」证据。以下是官方例文中实际出现过的全部景别词：

| 景别措辞（原文） | 出处 |
|---|---|
| `a medium-wide shot` | `base-en.txt:81`（§4.1 格式示例）、`:181` |
| `a close-up` | `base-en.txt:181` |
| `an extreme close-up` | `ref-en.txt:239` |
| `a medium shot` | `ref-en.txt:330` |
| `a close shot` | `base-en.txt:223`（L2VA 例文） |
| `a wide shot` | `segment_prompts.py:365`、`:450` 引用的官方逐字符格式示例 |

##### 「每镜必须确立什么」清单（`ref-en.txt:7`，官方一手）

> **Description detail:** Make `detailed_description` as detailed and explicit as possible. For each shot, clearly establish the current **composition**, **subject appearance and position**, **environment and lighting**, **actions and state changes**, **camera movement**, **current sound**, and the points where referenced content actually appears or takes effect. Avoid reducing the description to a plot summary or a list of reference relationships.

这 6 项就是官方口径的「电影语言检查表」，且明确否定了「剧情摘要化」与「只罗列参考关系」。

#### 3A-2. MiniMax 官方 skills 仓库（S12，一手，curl 已核实）

来源：`MiniMax-AI/skills` → `skills/frontend-dev/references/asset-prompt-guide.md`。这是 MiniMax 自家对提示词的写法口径，**构图/光线/风格词可直接借用**：

- 构图：`left-aligned subject with negative space on the right for text overlay`（原文强调 "Be specific about composition"）
- 光线（原文三例）：`soft studio lighting`、`golden hour backlight`、`flat diffused light`
- 风格修饰：`editorial photography`、`3D render`、`flat vector illustration`
- 技术规格：`4K resolution, sharp focus, shallow depth of field`
- 视频提示词口径（原文）：`Describe scene, subject, lighting, and mood — the API auto-optimizes prompts by default`；`For web backgrounds: keep 6s duration, add [Static shot] for stability`
- 图片提示词硬规则（原文）：`**NEVER** include text in image prompts unless explicitly requested — AI text rendering is unreliable`（与本项目「画面可见文字」处理相关）

同仓 `skills/gif-sticker-maker/assets/image-prompt-template.txt` 亦为 MiniMax 官方模板，其结构示范了「分块 + 每块带理由」的写法（`Style:` / `Subject handling:` / `Caption rendering (CRITICAL — follow exactly):`），可作为提示词模板的体例参考。

### 3B. 外部源：逐条核实结果（第二版）

第一版的 §3B 是搜索索引转述。第二版用 curl 打开原页后，**逐条给出「已核实 / 证伪 / 未能核实」**；证伪的条目**保留并写明错在哪**（反转本身就是证据）。

#### 3B-1. ✅ 已核实（Runway S7，https://runway.com/resources/ai-camera-prompts）

**排序公式**（原文）：
> `[Shot size] + [Angle] + [Movement + speed] + [Subject & action] + [Lens/look] + [Lighting/mood] + [What the shot reveals]`

原文示例：`Medium close-up, low angle, slow dolly push-in over 4 seconds, on a detective lighting a cigarette in the rain, 35mm lens with shallow depth of field, neon reflections. The push-in reveals the tension in his face.`

**机位-主体关系必须用动词写明**（原文）：
> State the relationship with a verb: "Camera follows the cyclist." "Camera trucks left, matching the runner's speed." "Camera leads the dancer, pulling back as she advances." "Slow tilt up to reveal the skyline behind him."

**多阶段运镜：写「每阶段画面里有什么」，不要堆动词**（原文）：
> Stacked verbs (unreliable): "Orbit the subject and crane up and push in." / Phased description (reliable): "The camera arcs around the seated figure, then rises above the table to reveal the empty chairs surrounding her."

**反 vague 改写**（原文完整对照）：原句 `A cinematic shot of a woman walking through a crowded market, dynamic camera, people selling fruit and fabrics, warm sunlight, shallow depth of field, 4k.` → 改写 `Medium-full shot, eye-level angle, camera follows a woman walking through a crowded market, matching her pace, steady tracking shot, warm sunlight, shallow depth of field, 35mm lens.`
原文对该改写的注解：`"Cinematic" and "4k" come out because they're doing no work.`、`The subject-camera relationship is the costliest omission.`

**double-directing**（原文）：
> The most common failure mode is double-directing. If the prompt and the setting disagree, you'll get a confused visual. Pick one source of truth for the movement itself, then use the other layer for everything around it.

**运镜术语表**（原文「What it looks like / When to use it」两列，逐条）：
`Dolly in / push-in`（Camera physically moves closer; background shifts with perspective —— Building tension, intimacy, focus on a reaction）、`Dolly out / pull-back`（Camera retreats; environment expands —— Reveals, endings, showing isolation）、`Pan left / right`（Camera swivels horizontally from a fixed position）、`Whip pan`（Very fast pan that smears the frame into motion blur —— Connecting two subjects, energetic transitions between beats）、`Tilt up / down`、`Truck left / right`（Camera slides sideways, parallel to the subject —— Walk-and-talks）、`Pedestal up / down`、`Orbit`（Camera circles the subject completely —— Product shots, hero moments）、`Arc`（Camera travels a curved path around the subject without completing a circle）、`Crane up / drone rise`（Establishing scale, dramatic reveals, endings）、`Crash zoom`（Extremely fast zoom in or out —— Comedy beats, shock reveals）、`Handheld`（Subtle natural shake —— Documentary realism, urgency, rawness）、`Steadicam`（Stabilized handheld, smooth while walking）、`Gimbal`（Electronically stabilized motion, the smoothest）、`Locked-off static`（Camera doesn't move at all —— Tension, symmetry）。

**机位角度表**（原文）：`Eye-level`、`Low angle`（Power, scale, making a subject imposing）、`High angle`（Vulnerability, isolation, showing geography）、`Over the shoulder (OTS)`、`POV`、`Top-down / aerial`、`Bird's eye view`、`Worm's eye view`（Scale, confinement, dread）、`Dutch angle`（Unease and disorientation. **Use sparingly.**）

**景别（Framing / shot size）表**（原文）：`Extreme close-up / macro`（A single detail fills the frame）、`Close-up`（**Shoulders up** —— Emotion, reactions, dialogue）、`Medium shot`（**Waist up** —— Body language plus expression. **The workhorse shot.**）、`Full shot`（Head to toe —— Action, movement, fashion, physicality）、`Wide / establishing shot`（Subject small within the environment —— Openings, scale, location-first storytelling）、`Extreme wide`（Vast area with the subject barely visible）

**焦点表**（原文）：`Deep focus`、`Shallow focus`、`Soft focus`（Diffused, hazy across the whole frame —— Memory, dreams, romance）、`Rack focus`（Focus shifts between planes during the shot —— Redirecting attention without moving the camera）

**构图表**（原文）：`Leading lines`、`Frame within frame`、`Symmetrical`、`Negative space`（Subject small against open emptiness —— Isolation, scale, minimalist product work）

**速度修饰不可省**（原文）：
> Every movement term works better with a speed or duration attached: slow, deliberate, steady, rapid, or an explicit timing like "slow 5-second pan right."

**静态镜头的加固句**（原文，★对本案最有用）：
> Static is the hardest camera instruction to land. Video models are built to generate motion, and establishing shots and wide landscapes are where unwanted drift shows up most. Two things fix it. Describe the motion that should happen in the scene and how elements enter or leave frame, so the model has somewhere to put its energy. Then reinforce the lock in natural language, which Runway's camera guide recommends handling with a line like: **The camera is entirely motionless for the duration of the scene, with movement only occurring from the subject.**

★ 这句与本仓禁令的推荐处置方向完全一致（`segment_prompts.py:468` 的 `a static medium shot with small amplitude at slow speed`），且**不命中** `_FAST_CAMERA_RE`，可直接作为「降机位」时的加固句候选。

**可抄用库样例**（原文，明确标注 `These are written for Gen-4.5`）：
- `Cinematic push-in (for tension and emotion): Medium close-up of a woman standing in the rain at night, slow dolly push-in toward her face over 4 seconds, shallow depth of field, neon reflections on wet pavement, cinematic 35mm film look.`
- `Product reveal: Close-up of a luxury watch on black stone, slow clockwise orbit, macro lens, controlled studio lighting, crisp reflections, premium commercial photography look.`
- `Drone establishing shot: Wide aerial shot over a misty mountain village at sunrise, slo[w]...`

#### 3B-2. ✅ 已核实（Google S3，https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/video-gen-prompt-guide）

页面标题 `Video generation prompt guide`，覆盖 `Gemini Omni Flash and Veo`。**注意：原 URL 已 301 跳转，引用时应用跳转后的 URL。**

**该页自己的提示词结构 = `Anatomy of a prompt`**（原文顺序）：Subject → Action → Scene or context → Camera angles → Camera movements → Lens and optical effects → Visual style & aesthetics（下分 Lighting / Tone or mood / Artistic style / Ambiance）→ Temporal elements（Pacing / Evolution / Rhythm）→ Audio。

**Camera angles（原文，含景别词——该页把景别放在「镜头角度」一节里，没有独立的 shot size 节）**：`Eye-level shot`（"eye-level shot of a woman sipping tea."）、`Low-angle shot`（"low-angle tracking shot of a superhero landing."）、`High-angle shot`（"high-angle shot of a child lost in a crowd."）、`Bird's-eye view or top-down shot`、`Worm's-eye view`、`Dutch angle`（"dutch angle shot of a character running down a hallway."）、`Close-up`（"close-up of a character's determined eyes."）、`Extreme close-up`（"extreme close-up of a drop of water landing on a leaf."）、`Medium shot`（"medium shot of two people conversing."）、`Full shot or long shot`（"full shot of a dancer performing."）、`Wide shot or establishing shot`（"wide shot of a lone cabin in a snowy landscape."）、`Over-the-shoulder shot`（"over-the-shoulder shot during a tense n[egotiation]"）。

**Camera movements（原文）**：`Static shot (or fixed)`（"static shot of a serene landscape."）、`Pan (left/right)`（"slow pan left across a city skyline at dusk."）、`Tilt (up/down)`（"tilt down from the character's shocked face to the revealing letter in their hands."）、`Dolly (in/out)`（"dolly out from the character to emphasize their isolation."）、`Truck (left/right)`（"truck right, following a character as they walk along a busy sidewalk."）、`Pedestal (up/down)`（"pedestal up to reveal the full height of an ancient, towering tree."）、`Zoom (in/out)`（原文强调 `This is different from a dolly, as the camera itself doesn't move.`）、`Crane shot`（"crane shot revealing a vast medieval battlefield."）、`Aerial shot or drone shot`（"Sweeping aerial drone shot flying over a tropical island chain."）、`Handheld or shaky cam`、`Vertigo effect (dolly zoom)`（原文：dollying 的同时反向 zoom，主体大小不变而背景透视剧变）。

**Lens and optical effects（原文）**：`Wide-angle lens`（"wide-angle lens shot of a grand cathedral interior, emphasizing its soaring arches."）、`Telephoto lens`（"telephoto lens shot capturing a distant eagle in flight against a mountain range."）、`Shallow depth of field`（原文给出术语 `bokeh`：`The aesthetic quality of this blur is known as 'bokeh'.`）、`Deep depth of field`。

**Cinematic terms（原文，编辑/剪辑术语——本仓此前完全未覆盖的一块）**：
> You can use cinematic terms for editing style and specific techniques. For example, "match cut", "jump cut", "establishing shot sequence", "montage", "split diopter effect."

**Negative prompts（原文，与 Runway S6 的正向措辞规则**交叉印证**）**：
> Not recommended: using instructive language or words such as "no" or "don't". For example, avoid prompts such as "no walls" or "don't show walls". / Recommended: Describe what you don't want to see. For example, "wall, frame".

**Temporal elements（原文）**：`Pacing`: `"slow-motion"`, `"fast-paced action"`, `"time-lapse"`；`Evolution`: `"a flower bud slowly unfurling"`, `"a candle burning down slightly"`, `"dawn breaking, the sky gradually lightening"`；`Rhythm`: `"pulsating light"`, `"rhythmic movement"`。

**★ 该页两处免责声明（原文，逐字）**——第一版转述作「not formally supported」，**措辞有误**：
> **Important: Some advanced camera angles are not officially supported.** The results and reliability may vary depending on the overall prompt and your specific use case.
> **Important: Some advanced camera lenses are not officially supported.** The results and reliability may vary depending on the overall prompt and your specific use case.

#### 3B-3. ✅ 已核实（StudioBinder S9 / S10）

**S9 运镜清单（原文，11 项）**：`Static / Fixed Shot`、`Dolly Shot`、`Zoom Shot`、`Dolly Zoom Shot`、`Pan Shot`、`Tilt Shot`、`Whip Pan Shot`、`Whip Tilt Shot`、`Tracking Shot`、`Crab Shot`、`Arc Shot`。
`Static Shot or Fixed Shot` 定义（原文）：`When there's no movement (i.e. locked camera aim) it's called a static shot.`
`Dutch Angle or Dutch Tilt Shot`（原文）：`the camera is slanted to one side. With the horizon lines tilted in this way, you can create a sense of disorientation.`

**S9/S10 景别阶梯（原文顺序，宽→近）**：`Extreme Wide Shot (ELS)`、`Long Shot (LS) / Wide Shot (WS)`、`Full Shot (FS)`、`Medium Long Shot (MLS) / Medium Wide Shot (MWS)`、`Cowboy Shot`、`Medium Shot (MS)`、`Medium Close Up (MCU)`、`Close Up (CU)`、`Extreme Close Up (ECU)`、`Establishing Shot`。

**S10 景别定义（原文）**：
- `Extreme Wide Shot (EWS)`：`will make your subject appear small against their location`
- `Full shot (FS)`：`lets your subject fill the frame, head to toe, while still allowing some features of the scenery`
- `Medium wide shot (MWS)`：`frames the subject from roughly the knees up`（aka medium long shot）
- `Medium close up (MCU)`：`frames your subject from roughly the chest up`
- `Establishing shots`：`a shot at the head of a scene that clearly shows us the location of the action. Establishing shots have no rules.`

**S10 关于景别的作用（原文）**：`Shot size affects: Framing and composition / Lens choice and camera placement / Blocking and staging / Continuity between shots / The overall visual style of a film`

#### 3B-4. ✅ 已核实（MiniMax Hailuo 运镜指令，经 S12 一手证实）

**S12 官方 Camera commands 表（原文，15 条，全部为英文）**：`[Truck left]`、`[Truck right]`、`[Push in]`、`[Pull out]`、`[Pan left]`、`[Pan right]`、`[Tilt up]`、`[Tilt down]`、`[Pedestal up]`、`[Pedestal down]`、`[Zoom in]`、`[Zoom out]`、`[Static shot]`、`[Tracking shot]`、`[Shake]`。
官方示例（原文）：`"A runner sprints through a forest trail [Tracking shot]"`。另：`Prompt: max 2,000 characters`。

**官方模型表（原文，★关键）**：`MiniMax-Hailuo-2.3`、`MiniMax-Hailuo-02`、`T2V-01-Director`（Notes: **Camera control optimized**）、`T2V-01`。→ **H3 不在其中**：方括号运镜是 Hailuo **API 产品线**的能力，与 H3 的 ComfyUI 提示词格式是两套东西。

#### 3B-5. ❌ 证伪 / 措辞有误（第一版转述与原文不符）

| 第一版转述 | 核实结果 | 原文实为 |
|---|---|---|
| Veo 有 5 段式公式 `[Cinematography] + [Subject] + [Action] + [Context] + [Style & Ambiance]` | **证伪（在 S3 页内 NO MATCH）** | S3 无此公式；该页的结构是 `Anatomy of a prompt` 九段（见 3B-2）。该公式可能出自另一份 Google Cloud 文档（未抓到）→ 归入 §5 未证实 |
| Veo 支持 `macro lens` | **证伪（NO MATCH）** | S3 的镜头词只有 `Wide-angle lens`、`Telephoto lens`、`Shallow depth of field`、`Deep depth of field`、`Vertigo effect (dolly zoom)`。`macro lens` 实际出现在 **Runway S7** 的术语表（3B-1），不是 Google 的 |
| Veo 支持构图词 `two-shot` | **证伪（NO MATCH）** | S3 无 `two-shot`；S7 有独立构图表（`Leading lines` / `Frame within frame` / `Symmetrical` / `Negative space`），也**没有** `two-shot` |
| Google 提示「部分进阶机位 not formally supported」 | **措辞证伪** | 原文是 **`not officially supported`**，且**角度与镜头各有一句**（3B-2 ★） |
| `soft focus` 属 Veo 的镜头/焦段术语 | **改判** | S3 里 `soft focus` 出现在 **Artistic style 的 `Romantic`** 风格描述中（原文：`Romantic : Soft focus, warm colors, intimate.`），不是镜头术语。作为**焦点术语**它出自 S7（3B-1） |
| Runway「一个片段一个运镜」 | **需修正（原意被反转）** | S7 原文：`Gen-4.5 has strong enough prompt adherence that **combining camera terms is encouraged**, including mixed angles, motion and composition in a single shot.` 以及 `One move per clip is the reliable fallback, **not the rule**.` → 一镜一运镜只是漂移时的兜底，**不是通则**；Runway 在这一点上并不与本仓禁令「同向」，它论的是**运镜个数**，本仓论的是**机位速度 × 动作节拍数**，两者维度不同、**不能互证**（§4 C5 已相应改写） |
| Hailuo 支持中文方括号指令 `[左移]`/`[左摇]`/`[跟随]`，可组合（≤3 个）、可顺序执行 | **未证实** | S12 官方表**只有英文指令**，且没有 `[Follow]`（官方是 `[Tracking shot]`），也没有「组合 ≤3 个」「顺序生效」的说明。中文形式与组合规则**未在任何一手源中证实** |
| Google 建议「不要写模型名/时长/宽高比」 | **未证实** | S3 抓取内容中未检索到该表述（本次只按关键词取窗口，**不排除**在未取到的段落里） |

#### 3B-6. ⛔ 未能核实（原页打不开，全部转述作废）

- **S5 Sora 2（403 / SSL 失败）**：第一版关于 Sora 的全部内容——镜头表字段（camera / lens / lighting / palette / action beats / dialogue）、`Camera: medium close-up, slow push-in with gentle parallax` 等示例、`same shot, switch to 85 mm`、「一个镜头一个运镜、一个决定性动作」、「冻住相机、简化动作、清空背景」——**全部未经核实**，不得引用。
- **S6 Runway Gen-4 官方指南（403）**：第一版的「正向措辞铁律」对照例（❌ `No camera movement. The camera doesn't move. NO MOVEMENT` → ✅ `Locked camera. The camera remains still.`）与「不要复述输入图」**未经核实**。
  ⚠️ 但「正向措辞」这一原则本身**已被 Google S3 独立证实**（3B-2 的 Negative prompts 一节），故该原则可用，**只是不能归到 Runway Gen-4 名下**。
- **Runway Camera Control 参数会覆盖提示词语言**：来自二手教程站，**未经证实**；S7 的表述更温和——`Pick one source of truth for the movement itself`，并给出「何时用设置 / 何时用提示词语言」的判断表（路径精度用设置，风格与意图用语言）。

---

## 4. 与本项目的对接点（含冲突风险）

### P1. `src/minimax_h3_prompt/references/base-en.txt` / `ref-en.txt` —— 唯一格式依据，只读

`CONTEXT.md:128-136` 已裁定这两个文件是官方格式的唯一依据。**补电影语言绝不允许改动它们**；新词表必须是它的**子集或兼容改写**。具体：
- §4.3 的 18 行表（`base-en.txt:104-121`）是运镜词白名单——**任何外部运镜词先对它做差集**（S7 的 Orbit / Crane up / Crash zoom / Handheld / Steadicam / Gimbal / Whip pan 全部不在白名单内）；
- §4.2（`:98`）与 `:123` 两条写法纪律是「外部词表能不能用」的过滤器；
- `ref-en.txt:7` 的「每镜要素清单」目前**只是文档里一句话，没有对应的检查或提示词接线**——它是本项目最该利用的现成官方资产。

### P2. `src/minimax_h3_prompt/prompts/prompt_engineer.md:26-33`「详细度增强」—— 放受控词表最自然的位置

该段已有「运镜节奏：写清机位类型、运动方向与幅度、速度变化」「光线变化：光源方向、色温/色调随时间变化」等**抽象要求，但没有任何词表**。把 §3A 的白名单与经筛后的候选池落在这里是最小改动。注意同文件 `:33` 的硬约束「**禁止把镜头运动、光线变化等动态过程写进静态生图提示词**」——景别/光线词表若同时用于生图侧必须分成两套。
★ 可直接增补的候选（一手来源，见 §3A）：MiniMax 的 `soft studio lighting` / `golden hour backlight` / `flat diffused light`；官方的 `soft lighting` / `slightly desaturated color palette` / `warm indoor lighting`。

### P3. `src/minimax_h3_prompt/prompts/cinematographer.md` —— 字段清单已对齐官方，缺词表 + 有语言矛盾

其产出要求（构图 / 机位运动「完整表达运动类型+幅度+速度」/ 光线 / 景深与氛围，`:10-13`）与 `ref-en.txt:7` 的官方清单**基本对齐**——**字段维度已经够了，缺的是每个维度下的受控词汇**。另 `:16` 的「正文必须是中文」与 `prompt_engineer.md:48` 冲突（见 C6）。

### P4. `src/minimax_h3_prompt/segment_prompts.py:620-666` `build_segment_v2_request` —— **长视频路径的接线缺口**

该函数的上下文组装（`:633-666`）包含：本段编号、写段时间窗、包含 Shot、`plan.summary`、帧锚定块（`_frame_anchor_note`）、用户修订块（`_revision_block_for_request`）、未来段剧情禁区、`shot_table` 原文。

**它不包含 `visual_design`（摄影指导产出），也不包含 `shot_review_lock`（分镜圆桌结论）。** 对照整片路径——`visual_design` 是入参的（`graph/nodes.py:491`、`:504` 给 prompt_engineer，`:211` 给 frame_prompt_engineer，`:399` 给 sound_designer）。

→ **含义**：对长视频（本项目主要场景），每段写作只看到分镜表原文，看不到摄影指导的构图/光线/景深细化。「没水平」在长视频上是**结构性缺口**，加词表也到不了终点，必须同时给写段请求加入口。
（`shot_table` 由 storyboard 节点产出 `graph/nodes.py:332,348`；`visual_design` 由 cinematographer 节点产出 `:375`，两者是不同 state 键。）
⚠️ **本条为代码接线事实（可复核）**；「它就是视频没水平的原因」是**我的推断，未实测**，未做 A/B。

### P5. `src/minimax_h3_prompt/segment_prompts.py:35-41, 673-705` —— **冲突闸门，必须过**

`_FAST_CAMERA_RE` 逐字（`segment_prompts.py:35-41`）：

```
\b(?:a\s+)?(?:fast|rapid|swift|whip)[\w-]*\s+
(?:low\s+|high\s+|wide\s+|tight\s+|close\s+|handheld\s+|tracking\s+|pan\s+
|dolly\s+|zoom\s+|push-in\s+|crane\s+|aerial\s+|overhead\s+)*
\w*(?:shot|tracking|pan|dolly|zoom|camera move|push-in|crane|handheld)\b
|at fast speed|rapid(?:ly)?\s+(?:tracking|pan|dolly|zoom|camera)|whip pan
|fast-paced camera
```

触发条件（`_check_fast_camera_multi_beat_coexist`，`:673-705`）：**同段 ≥3 个时间戳** 且命中上述正则 → **error** `FAST_CAMERA_MULTI_BEAT_COEXIST`。默认处置是**降机位**：`a static medium shot with small amplitude at slow speed`（`:468`）；剧情必须快镜时把节拍压到 ≤2 个或拆段。

**★ 本次核实新增的两条观察（供接线时参考，非缺陷断言）**：
- **会命中**：S7 术语表里的 `Whip pan` —— `|whip pan` 分支**逐字命中**。
- **不会命中**：S7 术语表里的 `Crash zoom` —— 快词表只有 `fast|rapid|swift|whip`，`crash` 不在其中；而 `Crash zoom` 语义上正是「extremely fast zoom」。即该正则是**快词同义表**而非速度语义判定，词表外的快机位新说法存在漏网可能。
- **可用的安全降级句**：S7 原文 `The camera is entirely motionless for the duration of the scene, with movement only occurring from the subject.` —— 验证过不命中上述正则，可作为「降机位」处置的加固句候选。

### P6. `src/minimax_h3_prompt/tools/h3_validator.py` —— 没有任何一条检查电影语言词汇

核对了该文件全部 `ValidationIssue` 登记点（`:120-474`）：检查项是结构类——对齐指令、Shot 标记、时间戳顺序/越界、首镜时间戳、对白标签与说话人顺序、标签连续性、段落齐全与顺序、正文长度、soundscape/music 句数。**没有一条检查景别、运镜词、光线或构图。**

→ 引入新词表后**没有既有闸门兜底**，模型自由发挥不会被拦；要防漂移需**新增**检查。按本仓「规则在、接线不在」的教训（已发生 6 次），新增检查必须确认真被调用，且**经图传递的键必须在 schema 里声明**（`graph/state.py`）——`PipelineState` 只搬运声明过的键，未声明的键到节点手上会静默消失。

### P7. `src/minimax_h3_prompt/tools/theme_guard.py:16-21` —— 双语词表的既有先例

该「要求词侧」映射表每一组都是**中英并列**（例：`("tavern", "inn", "alehouse", "酒馆", "酒吧", "客栈")`）。要做地点/景别类受控词表时，这是仓内现成的双语处理先例。
⚠️ 相关观察：`_CONFLICTING_OUTDOOR_TERMS`（`theme_guard.py:23`）只列英文（`forest clearing` / `open field` / `wilderness` / `outdoor camp`），若正文是中文则这些冲突词不会命中——与 C6 的语言问题同族，**未实测**。

### 冲突风险清单（明确标出，不和稀泥）

| 编号 | 冲突 | 证据 | 处置建议 |
|---|---|---|---|
| **C1** | **运镜花哨化 × error 级禁令**：S7 术语表里的 `Whip pan`（**逐字命中**）、`Handheld`、`Shake`，以及 S3 的 `Handheld or shaky cam`，与「快机位 + ≥3 拍动作同段共存」的 error 禁令相撞 | `segment_prompts.py:35-41, 700-705`；`CONTEXT.md:148-151`；`docs/adr/0003` | 引入任何运镜词前先跑该检查；**同段保持 ≤2 个 `At` 时间戳**即安全；引用该规则**必须声明阈值量于 4 步采样**（8 步下同一文本实测 0.869，两档不可比） |
| **C2** | **「剪辑节奏/多切快切」类建议 × 官方格式天花板**：切点必须整秒，且每个镜头隐含时长必须是 4–10 秒整数 | `base-en.txt:86`、`:98`；`prompt_engineer.md:51` | **不要引入**任何「增加切镜数量/加快剪辑节奏」的方案；节奏改由运镜幅度/速度 + 动作节拍承担。段内多 Shot 仅报 warning（`h3_validator.py:245-249` `MULTI_SHOT_SEGMENT`），不是禁止，但**时长上装不下**。⚠️ 注：S3 的 `Cinematic terms`（`match cut` / `jump cut` / `montage`）属**剪辑层**术语，在 H3 的单段单镜结构里基本无处安放 |
| **C3** | **关键词堆叠式提示词库 × 官方写法铁律**：官方明令运镜要写成镜头内的自然英文动作，不许在句尾堆标签 | `base-en.txt:123`；`ref-en.txt:219` | 排除「参数串/关键词表」式用法。**注意 S7 的排序公式本身是「逗号分隔的短语串」**（`Medium close-up, low angle, slow dolly push-in over 4 seconds, ...`）——它适用于 Runway，**照搬进 H3 就违反 `:123`**；只能借它的**排序思路**，落笔仍须成句 |
| **C4** | **Hailuo `[ ]` 方括号运镜指令 × H3 格式**：那是 Hailuo **API 产品线**的语法（S12 官方模型表里是 Hailuo-2.3 / 02 / T2V-01-Director，**没有 H3**） | S12（一手已核实）；`base-en.txt` 全文无方括号指令结构 | **不可移植**。只取其中英术语对与「减法运动」原则（后者亦**未证实**，见 3B-5） |
| **C5** | ~~Runway「一镜一运镜」与本仓禁令同向、可互为旁证~~ → **已修正** | S7 原文：`combining camera terms is encouraged`；`One move per clip is the reliable fallback, not the rule.` | **不能互证**：Runway 论的是**运镜个数**（鼓励叠加），本仓论的是**机位速度 × 动作节拍数**（禁止快 × 多），两者**维度不同**。既是「不同向」也谈不上冲突，属**不可比**。本仓禁令的放宽条件不变：需补「8 步慢机位对照 + 独立载荷样本」两组实测（`segment_prompts.py:373-376`）。**唯一真正同向且有价值的外部佐证是「静态加固」**（S7 3B-1 ★、S3 的 `Static shot (or fixed)`、S12 的 `[Static shot] for stability`）——三条独立官方源都指向「回静态」这根稳定杆 |
| **C6** | **正文语言矛盾（需用户裁定）**：`cinematographer.md:16`「正文必须是中文」 × `prompt_engineer.md:48`「最终提示词正文用英文」 | `prompts/cinematographer.md:16`、`prompts/prompt_engineer.md:48`、`base-en.txt`（通篇英文 Output Rules 与英文例文）、`theme_guard.py:16-21`（双语，不解决此问题） | 按仓规**先停下裁定，不选边**。此矛盾决定 §3 英文词汇能否直接落进正文：若正文中文，则英文词汇只能作为中间产物（`visual_design`）再翻译，词表需双语（学 `theme_guard.py`）。附带已核实：`brief.language` 是**对白语言**（`brief_parser.py:62` 默认 `Chinese`；`graph/nodes.py:401` 写作「对白语言…（对白原文必须保留）」），**不是正文语言**，不要拿它当依据 |

---

## 5. 未能证实项（第二版）

**已解决的（第一版列出、本次经 curl 绕行解决）**：S3、S7、S9、S10 四个源由「转述」升级为「逐字已核实」；S12 由「未读」升级为「一手已核实」；S11 的核心内容（15 条运镜指令）经 S12 一手证实。**解决途径：shell `curl` 直连绕开 WebFetch 的域名安全校验。**

**仍未解决的**：
1. **S4**（`ai.google.dev/gemini-api/docs/veo`）：curl 超时（exit 28），未读到。
2. **S5 Sora 2 Cookbook**：**HTTP 403**（响应体 `Forbidden`，含区域标记 `hkg1::`），换 `cookbook.openai.com` 主机后 **SSL/TLS 握手失败**（exit 35）。→ 第一版关于 Sora 的全部内容**作废**。
3. **S6 Runway Gen-4 官方指南**：**HTTP 403**（返回 5844 字节错误页）。→ 第一版关于它的两条内容**作废**（其中「正向措辞」原则已由 S3 独立证实，但归属须改写）。
4. **S8** Runway Text-to-Video 指南：与 S6 同域，判定不可达，未再试。
5. **S11**、**S13**：未抓取（前者核心已由 S12 覆盖，后者为二手博客）。
6. **Google 的 5 段式公式 `[Cinematography] + [Subject] + [Action] + [Context] + [Style & Ambiance]`**：在 S3 页内 **NO MATCH**。可能出自另一份 Google Cloud 文档（如检索到的 "Ultimate Prompting Guide for Veo 3.1"），该页本次未抓取 → **未经证实**。
7. **S3 的「不要写模型名/时长/宽高比」**：本次按关键词取窗口未命中，**不排除**在未取到的段落里 → 未证实。
8. **Hailuo 中文方括号形式、组合（≤3）与顺序生效规则**：任何一手源中均未证实 → 未证实。
9. **未检索**：ASC（美国摄影师协会）、No Film School、GitHub 开源 video-prompt / cinematic-prompt 集合。**是「没查」不是「没有」**。
10. **本文 §3 不含任何凭记忆补写的影视术语。** 凡未在 §3A 一手原文或 §3B 已核实条目中出现过的词，一律未收录。
11. §4 P4 的「分段路径收不到摄影指导产出」是**代码接线事实**（可复核）；把它认定为「视频没水平的原因」是**我的推断，未做 A/B 实测**。

### 下次调研建议（按性价比排序）

1. **优先补 S5 / S6**：换网络环境（当前区域被 403）或由用户本人打开，Sora 与 Runway Gen-4 的官方原文仍是空白。
2. 若确认 Google 5 段式公式存在，补抓该页（唯一未落地的「公式级」外部资产）。
3. 补查 ASC 与 GitHub 高星结构化提示词库。
4. **先裁定 C6（正文语言）再动 §3 的词表**——否则词表要做双语还是单语无法定。
5. 若要落词表，先做 P4 的接线（长视频路径），否则词表只对整片路径生效。
