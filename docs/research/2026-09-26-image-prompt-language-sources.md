# 生图（静态画面）提示词语言源调研

调研日期：2026-09-26
调研目的：为**生图侧**提示词补「静态画面语言」——构图、光线来源与质感、镜头/焦段、影调与色彩分级、材质与年代质感、写实风格词。视频侧已单独成文（`2026-09-26-film-language-prompt-sources.md`），本文件不重复其内容。
调研范围：用户在用的两个生图模型（**Flux.2 / Black Forest Labs**、**Z-Image / 阿里通义**）的官方口径 + 用户提供清单里各外部源。

## 调研方法（含预算与纪律）

- 分**三轮**抓取，每轮预算 10 次，**三轮各用满 10 次（合计 30 次）**。
  - 第一轮：GitHub 仓库验真 4 次（search API）、BFL docs 根页 / Z-Image README / invideo / Cinematique 共 4 次、BFL 两个正文页 2 次。
  - 第二轮：BFL prompting guide 重取（用于抽链接）1 次、4 个仓库文件树 4 次（trees API）、BFL cheatsheet 的 `.md` 版 1 次、Rylaispirit 四个词表文件 4 次。
  - 第三轮（收口，目标＝景别）：Rylaispirit `01` / `06` 与 Wayhhow `camera-and-composition` / `keyword-library` 共 4 次（`raw`/`HEAD`）、`cinematique.io` 1 次、`invideo.io/sitemap.xml` 1 次。
- 手段：`Bash` + `curl -sSL`（WebFetch 在本环境 12/12 全败，见上一份报告）→ 落盘临时目录 → python 剥 HTML / 解析 → **只取关键词窗口或按小节抽样**，不整页读。GitHub 走 API 与 `raw/HEAD`，并利用 Mintlify 的 `.md` 后缀取干净 markdown（BFL cheatsheet 因此从 660KB HTML 降到 60KB 纯文本）。
- 收尾：三轮的临时目录均已删除。
- **用户提供的那份资源清单：来源不明、疑似 AI 生成，全部条目逐条验真，结果见 §5。** 三轮下来结论有反转也有维持：GitHub 四个仓库名**全部为真**；`cinematic-video-prompt-skill` **确实有成体系词表（6/6 文件已读，约 700 条）**，清单举的两个具体词（`teal-and-orange`、`anamorphic`）**确实存在**；`ai-video-shot-prompt-skill` 提供**唯一的中文景别表**；但 `video-prompt-reverse` 的「词汇库」说法与 `Cinematic-prompt-master` 的术语表**均未获支持**，`Cinematique` / `invideo` 两条**始终未能访问**。

---

## 1. 结论先行

1. **清单不是编的，但权威性被高估；三轮补读后有反转也有维持。** 四个 GitHub 仓库名**全部真实存在**（精确同名，star 分别 95 / **1** / 16 / 48），**没有一个是行业权威源**——全是个人 skill 仓库。但补读后发现：`cinematic-video-prompt-skill`（95★）**确实有成体系词表**——6 个 reference 文件**已全读**，合计**约 700 条**术语，是本次找到的**最丰富的静态画面语言源**；清单举的两个具体词 `Teal and Orange Grading`、`Anamorphic *` 系列也**确实在文件里**（措辞略有出入）。反面：`video-prompt-reverse` 的「电影感词汇库」说法**未获支持**（文件树里没有术语表），`Cinematic-prompt-master`（1★）**读了，没有术语表**。清单里唯一的一手权威项仍是 **Black Forest Labs 官方文档**。

2. **BFL 官方口径里最有价值的发现不是词表，而是「结构化 JSON 提示词」。** `/flux_2/flux2_text_to_image` 官方写明：`Use JSON-structured prompts for precise control over generation — ideal for production workflows and automation.`，并给出字段示例——`subject` / `background` / `lighting` / `style` / `camera_angle` / `composition`。**后四项正是本仓生图侧缺的四类静态画面语言**，而本仓**已经**按模型输出 JSON（`image_prompt_engineer.md` 的 `zimage` / `flux2` 两键），接这条几乎是零结构改动。同页展示区的 `Eye Level View` / `Worm's Eye View` 也说明 BFL 对**静态图**确实在用机位角度词。

3. **BFL 的 Examples & Cheatsheet 已逐字读全——110 条术语，但「景别」缺席。** 真 URL 是 https://docs.bfl.ai/guides/prompting_video_camera_terms （第二轮从 guide 页内链取到）。**静态可用**：角度 15 / 构图 9 / 焦点 6 / 镜头 9 / 光线 11 / 画幅 4 / VFX 12 / 动画 4（全文见 §3A-5）；**视频向**：运动 18 / 时间 10 / 转场 8 / POV 4（已单列于 §3A-6，勿用）。两条限定：① 它是 **FLUX 3** 的参考，**不是本仓在用的 Flux.2**；② **景别词条没有**——nav 有 `Shot sizes` 标签，但数据里 `Extreme wide` **NO MATCH**。清单后半句「建议一个镜头只用一个主要运镜」在 I1/I2/I3 三页内均 **NO MATCH → 未证实**。

4. **景别（shot size）第三轮找到了，但只有第三方来源——这是三轮下来最硬的结论。** 官方两端**都没有**景别表（H3 的 `base-en.txt` 官方例文只出现过 6 个景别词；BFL 的 cheatsheet nav 承诺 `Shot sizes` 却无数据）。现可用的是四处，权威性分三档（详见 §3A-8）：**官方**＝Google S3 的 12 词（混在 `Camera angles` 一节）；**工业站**＝StudioBinder 的完整阶梯；**个人仓库**＝`Wayhhow` 的 **12 条中文景别表**（**唯一中文、免转译**，但其 `MS`/`MCU` 中英映射与工业惯例有偏差，**中文标签可用、英文缩写不可照抄**，见 C12）与 `Rylaispirit` `01` 里那 9 条（还和 61 条角度混在一张表）。⚠️ 落地前须先定「中文标签走哪套映射」。

5. **Z-Image 官方根本没有提示词写作指南。** 官方 README（`Tongyi-MAI/Z-Image`，已逐字读，21757 字节）只有模型结构、推理代码、榜单、Showcase，**没有任何提示词写法指导**。→ 本仓两个生图模型里，**只有 Flux.2 一侧有一手依据**；「Z-Image 官方口径」这条在本次调研中**不存在**，任何声称有 Z-Image 官方提示词规范的说法都缺来源。

6. **最重要的结构性冲突是画布比例，不是措辞。** 本仓两模型默认画布**不同**：`zimage` = **640×1280（1:2 竖构图）**、`flux2` = **1024×1024（正方）**（`generation.py:26-27`；字段定义见 `ImagePromptVariant.width/height`，`:119-120`）。构图类词（三分法、negative space 放哪一侧、留白方向、centered）**必须按模型分别写**——同一条构图指令在 1:2 与 1:1 画布上语义不同。这给「两模型必须分别重写」这条既有纪律补上了**物理依据**，不只是纪律要求。

7. **唯一的中文源同时也是纪律冲突最大的源。** 第三轮读到的 `Wayhhow`（16★）提供了**唯一的中文景别表**（免转译，价值高），但同一仓库的 `keyword-library.md` 的核心方法论是「**强制使用**去 AI 味质量词（`超写实`/`极致逼真`）」+「**杜绝…**」式负向祈使句——**同时撞本仓两条纪律**，且与 Google S3 官方口径（`Not recommended: using instructive language or words such as "no" or "don't"`）**正好相反**。→ 该源**只能取词、不能取法**（见 C11）。

---

## 2. 源清单表

| # | 名称 | URL | 谁维护（权威性依据） | 内容类型 | 信息密度 | 本次是否读到原文 | 对补本仓生图提示词的可直接用性 |
|---|---|---|---|---|---|---|---|
| I1 | **BFL Flux.2 文生图官方文档** | https://docs.bfl.ai/flux_2/flux2_text_to_image | **Black Forest Labs 官方**（正是本仓在用的 Flux.2） | 官方模型文档 | 中高（结构化提示词 / 精确色彩 / 示例） | ✅ **逐字读**（645KB→6149 字符正文） | ✅ **最高价值**：结构化 JSON 字段 + 色彩控制口径可直接接 |
| I2 | BFL 官方 Prompting Guide | https://docs.bfl.ai/guides/prompting_video_overview | BFL 官方 | 提示词指南 | 中（但**属 FLUX 3 Video**） | ✅ **逐字读**（660KB→5132 字符） | ⚠️ 主体是视频；对生图只提供「官方承认有 cheatsheet」这一线索 |
| I3 | **BFL「Examples & Cheatsheet」**（真 URL 已取到） | https://docs.bfl.ai/guides/prompting_video_camera_terms （`.md` 版：同址加 `.md`，仅 60KB） | **BFL 官方** | **术语表**（自称 `A compact FLUX 3 reference for shot sizes, angles, composition, movement, and focus prompts`） | **很高**：12 类 110 条，每条带定义 + 示例 prompt | ✅ **逐字读全**（60875 字节 `.md`） | ✅ **本次最大收获**，静态相关 5 类 50 条见 §3A-5；⚠️ 属 **FLUX 3**、不是本仓在用的 Flux.2；⚠️ **景别词仍未取到**（见 §3A-5 注） |
| I3b | BFL「Prompting Basics」 | https://docs.bfl.ai/guides/prompting_summary | BFL 官方 | 提示词基础原则 | 未知 | ❌ **URL 已定位，未读**（额度用尽） | ⚠️ 下次补 |
| I4 | Z-Image 官方 README | https://raw.githubusercontent.com/Tongyi-MAI/Z-Image/main/README.md | **阿里通义官方**（本仓另一个在用模型） | 模型 README | 高（模型/推理），**提示词内容为零** | ✅ **逐字读**（21757 字节） | ❌ **无提示词指南可用**（结论 4） |
| I5 | 仓内 `image_prompt_engineer.md` | `src/minimax_h3_prompt/prompts/image_prompt_engineer.md` | 本仓（一手） | 字段规范 | 高 | ✅ 已读全文 | ✅ 词表的落点（§4 P1） |
| I6 | 仓内 `frame_prompt_engineer.md` | `src/minimax_h3_prompt/prompts/frame_prompt_engineer.md` | 本仓（一手） | 字段规范 | 高 | ✅ 已读全文 | ✅ 词表的落点（§4 P2） |
| I7 | `Rylaispirit/cinematic-video-prompt-skill` | https://github.com/Rylaispirit/cinematic-video-prompt-skill | 个人仓库（95 star） | **结构化词表库**（6 个 references 文件 + examples） | **高**：**6/6 文件已全读**，合计约 **700 条**术语 | ✅ **全部读完**（`01` 70 行含角度+9 条景别 / `02` 83 / `03` 68 / `04` 151 / `05` 212 / `06` 175） | ✅ **有货**，静态词最丰富（§3A-7、§3A-7b）；⚠️ 「英文词 + 越南语解释」需自行译中；⚠️ 含运镜/快门/质量标签等冲突项 |
| I8 | `ngaitom928/Cinematic-prompt-master` | https://github.com/ngaitom928/Cinematic-prompt-master | 个人仓库（**1 star**） | 多语言 README 型 skill | **低** | ✅ **读了文件树**：16 个文件全是 `README*.md`（7 语）+ `SKILL*.md`（7 语）+ `examples/before-after.md`，**没有任何 reference/词表文件** | ❌ **读了，没有术语表**（清单对它的描述无法据此证实） |
| I9 | `Wayhhow/ai-video-shot-prompt-skill` | https://github.com/Wayhhow/ai-video-shot-prompt-skill | 个人仓库（16 star） | 视频分镜工作流 skill（**中文**） | **中高**（就本仓而言：中文免转译） | ✅ **读了两份关键文件全文**：`references/camera-and-composition.md`（**含 12 条中文景别表**，见 §3A-8）、`references/keyword-library.md`（中文关键词库，含「去 AI 味」质量词与「杜绝…」负向词，**与本仓纪律正面冲突**，见 C11） | ⚠️ **有景别表（中文，唯一一份）**；但该仓库的方法论（质量标签 + 负向词）**不能照搬**；`post-production.md` / `templates/style-presets.md` 未读 |
| I10 | `LunarXuan/video-prompt-reverse`（另有 `luozhilzh/video-prompt-reverse` 38 star） | https://github.com/LunarXuan/video-prompt-reverse | 个人仓库（48 star） | 视频提示词**反推** skill | **低**（就术语表而言） | ✅ **读了文件树**（12 个文件）：`references/` 下是 `analysis-contract.md`、`model-adapters.md`、`model_profiles.json`、`optimization-loop.md`，**无独立术语表/词表文件**；正文未读 | ❌ **文件树里没有术语表**（该仓库定位是「反推流程」，不是词汇库；清单称其为「电影感词汇库」**未获支持**） |
| I11 | Cinematique（号称 150+ 技法） | 清单未给 URL；试过 `https://www.cinematique.ai/` 与 `https://cinematique.io/` | 未知 | 未知 | 未知 | ❌ **两个域名均 SSL/TLS 握手失败**（exit 35，`.ai` 与 `.io` 都一样；且清单未给 URL） | **未核实**；域名可能不对，但**两次不同 TLD 同错**，倾向于该站在本环境不可达 |
| I12 | invideo.io AI 视频提示词指南 | 清单未给 URL；试过 `/blog/ai-video-prompt-guide/`（404）与 `/sitemap.xml` | 厂商博客（二手） | 术语参考 | 未知 | ❌ 路径 **HTTP 404**；`sitemap.xml` 返回 200 但只有 2662 字节的**索引页**，其中**无任何含 `prompt` 的 URL** | **未核实**（正确路径未能定位） |
| I13 | PromptHero / megatek.ai | 清单未给 URL | 未知 | 未知 | 未知 | ❌ 未抓取 | **未核实** |

**注**：本轮**没有**任何源是影视工业侧的权威机构（ASC 等）——本次仍未检索。

---

## 3. 可直接抄用的静态画面词汇与句式

> 纪律：以下 3A 全部是**我逐字读到的原文**；3B 全部是**未核实或已证伪**，不得当事实使用。凡是只有清单声称、我未读到的词（例如 `anamorphic widescreen lens`、`teal-and-orange color grade`），**一律放在 3B**，不进 3A。

### 3A. 一手已核实

#### 3A-1. BFL Flux.2 官方：结构化 JSON 提示词（I1，逐字）

原文：`Structured Prompting — Use JSON-structured prompts for precise control over generation — ideal for production workflows and automation.` 官方给的示例（逐字，含字段名）：

```json
{
  "subject": "Mona Lisa painting by Leonardo da Vinci",
  "background": "... ornate gold frame",
  "lighting": "soft gallery lighting, warm spotlights",
  "style": "digital art, high contrast",
  "camera_angle": "eye level view",
  "composition": "centered, portrait orientation"
}
```

**这是本次最有价值的单条**：`lighting` / `style` / `camera_angle` / `composition` 四个字段名直接对上了本仓的缺口，且本仓本来就是出 JSON 的。

#### 3A-2. BFL Flux.2 官方：静态图像的画面语言（I1，逐字摘录）

- 机位角度（展示区标题，均为静态图）：`Eye Level View`、`Worm's Eye View`
- 光线/氛围：`Cinematic lighting`、`Realistic skin texture and lighting`、`Hyper-realistic close up`、`cinematic natural light`
- 影调与色彩：`a stark palette of winter whites and greys`
- 材质与细节（官方对模型的描述）：`It closes the gap between generated and real imagery with accurate hands, faces, and textures`
- 产品/风格短语：`professional product photography`、`clean minimalist tech aesthetic`、`dark gradient background`、`close-up of phone edge showing titanium frame`
- 一条完整自然语言句式（原文）：`At high noon on a blustery day, capture the surreal presence of a sentient tree, seemingly rooted underwater just off a tumultuous ocean shore. Employ a sweeping panning shot, bathing the scene in cinematic natural light and a stark palette of winter whites and greys, as if glimpsing a spectral sentinel through a watery veil.`

#### 3A-3. BFL Flux.2 官方：精确色彩控制（I1，逐字）

原文：`Exact Color Control — Specify brand colors via hex codes with precision matching — no approximation.` 官方示例：`the color of the vase is a gradient of color, starting with color #02eb3c and finishing with color #edfa3c. The flowers inside the vase have the color #ff0088`；多色示例：`top row #B76E79, #E8D5B7, #8B4789; bottom row #CD7F32, #F8F6F0, #800020`。
→ 对本仓的意义：人物服装、道具、场景的**品牌/指定色**可以用 hex 锁死，这是「跨帧同色」比形容词更硬的写法。

#### 3A-4. 本仓与同批一手源里**适用于静态图**的现成词

这些来自上一份报告已核实的一手来源，**静态图同样适用**（不含任何运镜词）：

- MiniMax 官方 `asset-prompt-guide.md`（MiniMax 官方 GitHub）：构图 `left-aligned subject with negative space on the right for text overlay`；光线 `soft studio lighting`、`golden hour backlight`、`flat diffused light`；风格 `editorial photography`、`3D render`、`flat vector illustration`；技术 `4K resolution, sharp focus, shallow depth of field`
- H3 官方 `ref-en.txt:237`：`soft lighting`、`slightly desaturated color palette`；`:329` `warm indoor lighting`
- H3 官方 `base-en.txt:78` 风格词（生图侧可借用的视觉风格）：`Cinematic`、`live-action`、`2D-animated`、`3D CG`、`claymation`、`watercolor`、`vintage film`

#### 3A-5. BFL 官方 Examples & Cheatsheet —— 静态画面可用的 5 类（I3，逐字）

来源：https://docs.bfl.ai/guides/prompting_video_camera_terms（`.md` 版 60875 字节，已逐字读全）。原文自述：`A compact FLUX 3 reference for shot sizes, angles, composition, movement, and focus prompts.`
格式为 `term` + `desc` + 一条示例 prompt（示例均为视频向，含 `10 seconds, 16:9`，故此处只取 term/desc）。

**⚠️ 三条重要限定**：① 这是 **FLUX 3** 的参考，**不是本仓在用的 Flux.2**（同厂但不同模型）；② 页内 nav 列了 14 节，但**数据数组只覆盖 12 类，且没有景别（shot size）的词条**——nav 有 `Shot sizes` 标签指向 `#shot-sizes-and-framing`，而整页 `Extreme wide` **NO MATCH**，**景别词本章仍未取到**；③ 下表 5 类为静态画面可用，`movements` / `time` / `transitions` / `pov` 四类见 §3A-6（视频向）。

**Angles（机位角度，15 条）**

| term | desc（官方原文） |
|---|---|
| Aerial | View from high above the scene |
| Low angle | Camera looks upward to add scale or dominance |
| High angle | Camera looks downward to compress or expose |
| POV | Shot from the subject's perspective |
| Over-the-shoulder | Frames action past a foreground subject |
| Dutch angle | Tilted horizon for unease or tension |
| Worm's eye | Extreme low angle looking straight up |
| Bird's eye (top-down) | Straight-down overhead view |
| Eye level | Neutral height matching the subject's eyes |
| Ground level | Camera at the ground looking across the scene |
| Profile shot | Strict side-on view of the subject |
| Tableau | Static, symmetrical, staged wide frame |
| Fourth wall | Subject looks and speaks directly to the camera |
| Object POV | View from an object's perspective |
| Voyeur | Hidden, obstructed view spying on the subject |

**Composition（构图，9 条）**

| term | desc（官方原文） |
|---|---|
| Leading lines | Uses lines in the frame to guide the eye |
| Center framing | Keeps the subject locked in the middle |
| Rule of thirds | Places the subject off-center for balance |
| Symmetry | Mirrors shapes for precision or tension |
| Negative space | Leaves open space around the subject |
| Frame within frame | Uses an in-scene opening to frame the subject |
| Foreground occlusion | Blurred foreground objects add depth |
| Silhouette | Subject rendered dark against bright light |
| Reflection framing | Composes the subject in a reflective surface |

**Focus（焦点，6 条）**

| term | desc（官方原文） |
|---|---|
| Shallow depth of field | Subject stays sharp while background softens |
| Deep focus | Foreground and background both stay sharp |
| Rack focus | Focus shifts from one subject plane to another |
| Split diopter | Near and far planes both stay sharp |
| Focus breathing reveal | Blur slowly resolves to reveal the subject |
| Tilt shift | Selective focus for a miniature look |

**Lenses（镜头/光学，9 条）**

| term | desc（官方原文） |
|---|---|
| Wide angle (24mm) | Exaggerated space and edge distortion |
| Telephoto compression | Flattens depth and stacks planes together |
| Fisheye | Extreme curved wide-angle distortion |
| Anamorphic flares | Horizontal streaks and oval bokeh |
| Macro lens | Extreme magnification of tiny detail |
| Probe lens | Snorkel lens weaves low through tight spaces |
| Halation | Glowing bloom halos around bright highlights |
| Parallax | Depth layers slide against each other at different speeds |
| Vignette | Darkened edges frame a bright center |

**Lighting（光线，11 条）**

| term | desc（官方原文） |
|---|---|
| Rim light | Backlight outlines the subject's edge |
| Chiaroscuro | Strong contrast of light and deep shadow |
| Golden hour | Warm, low-sun backlight |
| Neon practicals | In-scene colored light sources |
| Volumetric light | Visible beams and god rays through haze |
| Hard light | Harsh, sharp-edged shadows and high contrast |
| Haze | Thick atmosphere reveals volumetric light shafts |
| Spotlight | A single beam isolates the subject in darkness |
| Light flash | Strobing flashbulb bursts freeze motion |
| Projections | Projected imagery plays across the subject |
| Underwater light | Rippling caustics dance across surfaces |

**另有两类偏风格、静态图亦可用**：`format`（4 条）`Cinemascope (21:9)` / `Vertical (9:16)` / `Vintage (4:3)` / `Split screen`；`vfx`（12 条，含 `Double exposure` / `Datamosh` / `Kaleidoscope` / `Morphing` / `Slit scan` / `X-ray` / `Levitation` / `Dreamcore` / `Dystopian` / `Magical realism` / `Maximalism` / `Diorama`）；`animation`（4 条）`Stop motion` / `Pixel art` / `Zoetrope` / `Kinetic typography`。

#### 3A-6. BFL 同一页的**视频向**类别（标注：生图侧勿用）

这四类是动态语言，与本仓「不写视频镜头运动/剪辑/时间码」冲突，**只登记不使用**：
- `movements`（18 条）：`Pan`、`Tilt`、`Dolly in`、`Tracking shot`、`Orbit`、`Crane / boom`、`Handheld`、`Whip pan`、`Dolly zoom`、`Steadicam follow`、`Push through`、`Snorricam`、`Camera roll`、`Arc shot`、`Pedestal`、`Trucking`、`Locked-on`、`Lazy Susan`
- `time`（10 条）：`Slow motion`、`Speed ramp`、`Timelapse`、`Long exposure look`、`Bullet time`、`Freeze frame`、`Boomerang`、`Step printing`、`Fast motion`、`Cinemagraph`
- `transitions`（8 条）：`Match cut`、`Whip transition`、`Foreground wipe`、`Jump cut`、`Object portal`、`Pass-through`、`Quick cuts`、`Screen-in-screen`
- `pov`（4 条）：`Drone FPV`、`Bodycam`、`Dashcam`、`Mirror POV`

#### 3A-7. `Rylaispirit/cinematic-video-prompt-skill`（I7）—— 结构化词库，静态子集

**结构**：`references/` 下 6 个文件，每份是一张两列表 `| Prompt (EN) | Giải thích (VI) |`——**左列是可直接进提示词的英文术语，右列是越南语解释**（不是中文，需自行转译）。已读 4 份合计约 **510 条**；`01-camera-angles-and-movement.md`、`06-materials-weather-pose.md` **未读**。

**核验：清单对该仓库的描述基本属实。** 清单提到的两个具体词都能在文件里找到——`Teal and Orange Grading`（在 `05`，解释 `Da cam / bóng xanh ngọc, Hollywood`）、`Anamorphic Squeeze / De-Squeeze`、`Anamorphic Bokeh (Oval)`、`Anamorphic Glare (Red/Orange Streak)`（在 `04`）。清单原文写的是 `anamorphic widescreen lens`，**该确切短语不存在，但词族存在** → 属**措辞略有出入的证实**。

下列为**静态画面可用**的抽样（同义并列写法保留原样；带 ⚠️ 的与本仓纪律冲突，见 §4）：

**光线（`02-lighting.md`，83 条，按该文件自身小节）**
- 自然光与时点：`golden hour`、`Magic Hour with Sun-Kissed Effect`、`blue hour`、`Direct Sunlight (Midday)`、`Overcast Light (Flat Light)`、`Window Light (Soft & Diffused)`、`dappled light`、`crepuscular rays`、`volumetric lighting`、`Sunburst Effect`、`backlighting`、`silhouette`、`Atmospheric Perspective`、`Haze/Atmosphere Effect`、`Global Illumination (GI)`、`Diffused Ambient Light`、`Ambient Occlusion`、`High Dynamic Range (HDR) Lighting`、`bioluminescence`
- 影棚与人像：`studio lighting`、`soft light`、`hard light`、`rim lighting`、`Rim Light (Subtle)`、`Edge Lighting`、`rembrandt lighting / Rembrandt Triangle`、`Softbox Lighting`、`Octabox Lighting`、`Hard Studio Light (Beauty Dish)`、`Two-point Lighting`、`High Side Lighting`、`Grid Lighting`、`Snoot Effect`、`Gobo Lighting`、`Gel Lighting`、`Specular Highlights`、`Reflective Catchlight`、`spotlight`、`Spotlight vs Floodlight`、`Warm Fill with Cool Key`
- 戏剧/高对比：`Cinematic Lighting`、`chiaroscuro`、`Low-key lighting`、`High Contrast, Deep Shadow`、`Gothic Lighting`、`Under-lighting`、`Light from a Single Candle`、`Muted Lighting`、`Monochromatic Light Source`、`Fluorescent Flicker`
- 城市与人造光：`neon glow / neon lights`、`Practical Lighting`、`Wet reflections lighting`、`Rainy night atmosphere`、`Street Lamp Glare`、`Street Light Halos`、`Car Headlight Beam`、`Screen Light on Face`、`Tungsten Light Color`、`Cove Lighting`、`Mood Lighting (Soft, Amber)`、`Stained Glass Light Pattern`
- 材质光照：`Subsurface Scattering`、`Refraction and Caustics`

**构图（`03-composition.md`，68 条）**
- 基础法则：`rule of thirds`、`Grid of Nine`、`golden ratio / fibonacci spiral`、`The Golden Section / Golden Mean`、`Spiral Rule`、`Dynamic Symmetry`、`The Crosshair Method`、`Rule of Odds`、`Grouping of Threes`、`Rule of Space`、`Breathing Room / Look Space`、`Headroom and Lead Room`
- 引导线与形状：`leading lines`、`Implied Lines`、`Gaze Direction`、`Diagonal Lines`、`Curved Lines`、`S-Curve Composition`、`Z-Shape Composition`、`L-Shape Composition`、`Triangular Composition`、`Circular Composition`、`The Vanishing Point`、`Tunneling Effect`
- 平衡与对称：`symmetry / Central Axis Symmetry`、`Symmetry (Broken)`、`Centred Composition`、`Asymmetrical Balance`、`Dynamic Balance`、`Balance by Color/Value`、`Visual Weight Distribution`、`Static Composition`、`Tonal Harmony`
- 空间与纵深：`negative space`、`Isolation of Subject`、`Filling the Frame`、`Break the Frame`、`Open Composition`、`Closed Composition`、`framing / Framing within a Frame`、`Foreground Interest`、`Repoussoir`、`Depth of Field Stacking`、`Shallow/Deep Perspective`、`Overlapping Subjects`、`Focal Length Compression`、`Focal Length Expansion`、`Near-Far Juxtaposition`、`Panoramic Composition`
- 节奏与对比：`Rhythm and Repetition`、`Repetition with Variation`、`Echoing Shapes`、`Pattern Interruption`、`Juxtaposition`、`Visual Tension`、`Figure-Ground Relationship`、`Focal Point (Hard)`、`Gestalt Principles (Closure/Proximity)`

**镜头 · 技术 · 胶片质感（`04-lens-technical-film.md`，151 条，**本仓缺口最大的一类**）**
- 焦距与光圈：`24mm / 35mm / 50mm / 85mm lens`、`Fast prime lens (50mm f/1.4, 85mm f/1.8)`、`Aperture f/1.2 (Extreme Shallow Depth)`、`T-Stop T1.4`、`shallow depth of field`、`Depth of Field (Extremely Deep)`、`Hyperfocal Distance`、`Telephoto Lens (Focus Stacking)`、`Macro Photography (Extreme Detail)`、`fisheye lens`、`Lens Distortion (Pin Cushion/Barrel)`、`Tilt-Shift Lens Effect (Miniature Faking)`、`Faux Tilt-Shift (Digital Miniature)`、`Vintage Lens Aesthetic`、`Uncoated Lens Look`、`Anamorphic Squeeze / De-Squeeze`
- 散景：`bokeh`、`Anamorphic Bokeh (Oval)`、`Lens Bokeh (Swirly/Petzval)`、`Lens Bokeh (Lemon/Cat's Eye)`
- 光晕：`lens flare`、`anamorphic lens flare`、`Blue Streak Filter / Vertical Flare`、`Anamorphic Glare (Red/Orange Streak)`、`Lens Flare (Multiple Rings)`、`Ghosting Effect (Flare)`、`Flare (Dusty/Dirty Lens)`、`Lens Whacking (Light Leak)`、`Sun Star Effect (Sharp Aperture)`、`Cinematic Halation`
- ⚠️ 快门与运动（**动态，生图侧勿用**）：`motion blur`、`Motion Blur (Radial)`、`Shutter Speed 1/8000s` / `1/30s` / `1/2s`、`Shutter Angle 180 Degrees`、`long exposure for light trails`、`time-lapse`、`Low FPS Motion Blur (Stuttering)`、`Film Stutter (Choppy Motion)`、`Camera Shake (Digital Emulation)`、`rack focus / Focus Pull`
- 滤镜与柔化（静态可用）：`ProMist Filter / Black Pro Mist`、`Soft Focus (Black Mist Filter)`、`Cinematic Diffusion (Heavy)`、`Soft Focus (Dream Glow)`、`Soft Focus (High Contrast Edges)`、`Glow Effect`、`Warm Polarizer Filter`、`Circular Polarizer Filter (Max Saturation)`、`Soft Vignette (Subtle Light Falloff)`、`Vignetting (Rectangular Frame)`、`Pinhole Camera Effect`、`double exposure`、`infrared`、`Chromatic Aberration / Color Fringing`
- 机身与传感器：`Shot on Sony Alpha camera`、`Shot on RED Gemini / Arri Alexa`、`Super 35 Sensor Format`、`Medium Format Film Quality / Medium Format Digital Look`、`authentic mobile phone photography style (24–26mm)`、`ISO 100 (Clean Image)`、`Clean Digital Sensor Look (No Grain)`、`Digital Noise (Color Noise)`、⚠️ `8K Resolution Photography / 8K / 4K / hyperdetailed`、`Photogrammetry Scan Quality`
- 画幅：`Cinemascope / 2.35:1 Aspect Ratio`、`Wide Aspect Ratio 2.76:1 (Ultra Panavision)`、`IMAX Quality`、`16:9 / 9:16 vertical / 1:1`
- 胶片颗粒与胶片型号：`High Grain / Film Grain`、`Cinematic 35mm Film Grain (Medium)`、`Film Grain (Heavy, Colored)`、`Grain (Luminance Only)`、`Overexposed Film Grain`、`Film Stock [Kodak Portra 400]`、`Vintage Kodachrome Colors`、`CineStill 800T Color Shift`、`Slide Film Look (High Saturation, Sharp)`、`Color Negative Film (Faded Shadows)`、`Cross Processed E-6/C-41`、`Bleach Bypass Effect`、`Technicolor Aesthetic`、`Super 8 Film Aesthetic`、`Super 16mm Film Look`、`Pristine 70mm Quality`
- 年代感/模拟：`vintage photo`、`Polaroid (Worn Edges)`、`Disposable Camera Aesthetic`、`Lomography Aesthetic`、`Daguerreotype Photo`、`Wet Plate Collodion (Ambrotype)`、`Sepia Tone (Heavy)`、`Film Scratch Overlay`、`Underexposed Film Look (Muted Colors)`、`Faded Color Grading (Low Saturation)`
- 数字瑕疵（风格化用）：`Lo-Fi Digital Look (JPEG Artifacts)`、`VHS Tracking Error`、`Moiré Pattern Effect`、`Screen Door Effect`、`8-Bit Color Palette`、`Unreal Engine 5 Render (Photorealistic)`、`Photorealistic CGI (No Imperfections)`
- 后期调色：`Vibrant HDR / HDR Tone Mapping`、`Low Contrast Grading`、`Flat Color Profile (Log)`、`Rec. 709 Color Space`、`DaVinci Resolve Color Grade`、`Vibrance Boost`、`Warm White Balance / Cool White Balance`、`Cool color balance with warm neon accents`、`Underexposed / Overexposed`、`Exposure Bracketing`、`black and white / Black and White (High Contrast, Ansel Adams Style)`、`ProRes 4444 Quality`

**风格 · 色彩 · 情绪（`05-style-color-mood.md`，212 条，只列与本仓字段最相关的）**
- 摄影/电影风：`cinematic`、`hyperrealistic / photorealistic`、`street photography / Cinematic street photography`、`Urban portraiture`、`Neon noir aesthetic`、`film noir`、`documentary photography`、`food photography`、`cinemagraph`
- 画风/媒材（`image_prompt_engineer.md` 的「写实视觉风格」字段可用）：`anime style / Ghibli style`、`cel shading`、`line art`、`pencil sketch / charcoal drawing / ballpoint pen art`、`watercolor painting`、`ink wash painting`、`concept art`、`book illustration / vintage children's book illustration`、`claymation / stop-motion`、`pixel art`、`vector art`、`low poly`、`isometric`、`glitch art`、`blueprint / schematic diagram`、`cross-section / exploded view / cutaway drawing`
- 流派与美学：`surrealism`、`minimalism / maximalism`、`pop art`、`art deco / art nouveau`、`bauhaus`、`expressionism`、`renaissance painting / baroque / rococo painting`、`chiaroscuro`、`fresco`、`ukiyo-e / woodblock print`、`cyberpunk`、`steampunk`、`synthwave / vaporwave`、`stained glass / mosaic / tapestry`、`psychedelic art`
- 调色板：`vibrant colors / High Saturation`、`pastel colors`、`Muted Color Palette / Desaturated`、`Desaturated but Warm`、`Cinematic Muted Tones`、`monochromatic`、`Achromatic Color Scheme`、`Monochrome with Spot Color`、`sepia tone`、`Duotone/Tritone Effect`、`Minimalist Palette (Limited Colors)`、`Color Palette [Specific Hex Codes]`、`Gradients of Color`
- 色彩和谐：`Complementary Colors`、`Split Complementary`、`Analogous Colors`、`Triadic Color Palette`、`Tonal Harmony`
- 色温与影调：`Warm Color Palette (Dominant)`、`Cool Color Palette (Dominant)`、`Earth Tones`、`Golden Hues`、`Jewel Tones`、`Color Temperature 2700K / 6500K / 9000K`、`Tinted Shadows`、`Lifted Blacks / Faded Shadows`、`High Key Colors / Low Key Colors`、`High Chroma / Low Chroma Color`、`Ambient Color Shift`
- 电影分级：**`Teal and Orange Grading`**、`Split Tone (Warm/Cool)`、`Cinematic Contrast (Darkened Midtones)`、`High Contrast (Color vs Luminosity)`、`Luminosity Masking`、`Neon Palette (Cyberpunk Hues)`、`Vaporwave Palette`、`Retro 70s Color Scheme`、`Color Blocking / Pop Art Brightness`、`Soft Focus with Color Bloom`
- 情绪（对应「氛围/基调」，可支撑 `non_diegetic_music` 与导演阐述，也是生图氛围词）：`serene / peaceful / Calm`、`Zen / Meditative`、`Comfort / Coziness`、`Rustic Charm`、`Hopeful / Uplifting`、`Romantic / Tender / Intimate`、`Quiet Dignity / Solemn / Formal`、`joyful / energetic`、`Playful / Cheerful / whimsical`、`Triumphant / Victorious`、`melancholy / Melancholic Beauty`、`nostalgic / Retro Vibe`、`Wistful / Longing`、`Contemplative / Pensive`、`Isolation / Solitude / Alienation`、`Despair / Gloomy / Somber`、`Ephemeral / Fleeting`、`mysterious / Enigmatic`、`ominous / Foreboding`、`Eerie / Uncanny / Haunting`、`Tension / Anticipation / Suspenseful`、`Dread / Apprehension`、`Menacing / Sinister`、`Trapped / Claustrophobic`、`Sense of Urgency`、`Intense / Dramatic`、`majestic / Grand / Awe / Wonder`、`Spiritual / Sacred / Ethereal`、`Resilience / Strength`、`Defiant / Rebellious`、`Overwhelmed / Chaotic / Frenetic`、`Surprise / Shock`、`Dreamy / Hazy`、`Surreal / Dreamlike / Escapism`、`Detached / Clinical`、`Flawed / Imperfect`

#### 3A-7b. Rylaispirit 其余两个文件（第三轮补读，6/6 文件已全读）

**`06-materials-weather-pose.md`（175 行；A 材质与表面 / B 天气与空气 / C 姿态）——正对本仓「外观或材质」字段**
- 表面光泽：`matte finish / Non-reflective`、`glossy finish / Glossy Varnish Finish / Wet Look Finish (High Gloss)`、`Highly Reflective (Mirror-like)`、`Burnished Finish`、`Anisotropic Reflection`、`iridescent / Pearlescent / Mother of Pearl (Nacre)`、`holographic / Anodized Titanium (Rainbow) / Oil Slick Effect`、`Micro-etched Surface (Anti-Glare)`
- 金属：`brushed metal / Brushed Aluminium`、`chrome / Chromed Plastic`、`Hammered Metal`、`Rough Cast Iron`、`Rusted Iron / Corroded Metal`、`Oxidized Copper / Patina Green Copper / Aged Patina`、`Liquid Metal (Flowing)`、`Diamond Plated Metal`、`Etched Metal (Patterned)`、`Foil Stamping (Metallic Sheen)`
- 木石土：`polished wood / Aged Wood Grain / Distressed Wood / Engraved Wood`、`polished marble`、`Rough Sandstone / Gravel Texture`、`Sculpted Concrete / Sealed Concrete / Stucco Texture`、`cracked earth / Dry Cracked Mud / Cracked and Eroded`、`Wet Clay/Pottery`、`Smoky Quartz / Translucent Crystal`、`Algae/Moss Covered`、`Worn out by Weathering / Chipped Paint`
- 织物与皮：`velvet / Crushed Velvet / Velvet Texture (Absorbing Light)`、`worn leather / Worn and Patched Leather / Oiled Leather`、`Brushed Suede`、`embroidered / Stitched and Seamed`、`Woven Fabric (Visible Fibers)`、`Knitted Wool (Thick Gauge) / Quilted Pattern`、`Woven Hemp/Burlap / Woven Basketry`、`Sequined Fabric`、`Microfiber Fabric / Fluffy/Downy Texture`、`Scaly Texture (Reptile/Fish)`
- 玻璃/塑胶/液体：`translucent / Translucent (Soft Glow)`、`Frosted Glass / Etched Glass`、`Scratched Glass/Acrylic`、`Transparent Silicone / Semi-Transparent Plastic`、`Resin Encased`、`Wet and Slick Surface / Wet asphalt texture / Reflective puddles`、`Rippled Water Surface`、`Beaded Water / Condensation / Hydrophobic Surface`、`Aqueous/Gooey / Sticky Texture`、`Icy and Brittle`
- 其他质地：`carbon fiber`、`Rough, Coarse Texture / Abrasive Texture / Fine Grain Sandpaper`、`Porous and Absorbent / High Porosity Foam`、`Chalky Texture / Powdery / Dusty and Dull`、`Dirty/Grungy Texture / Stained and Discolored`
- B 天气与空气 / C 姿态两节（雾/雨/雪/晴、静与内省/力量与动作/人像与时尚）虽未逐条抄录，但**同属静态可用**，在第 3 轮已确认存在（175 行总量，A 节约 90 条已在上面）。

**`01-camera-angles-and-movement.md`（Section A = `Góc máy & cỡ cảnh (Angles & Shot Sizes)`，共 70 行）**
- **该文件把「角度」与「景别」混在一张表里**，没有独立的景别阶梯；其中**景别类**仅 9 条：`Extreme Close-up (Detail Focus)`、`Macro Angle (Texture Focus)`、`Pushed-in Close-up`、`The Reaction Shot (Close Up)`、`Medium Close Up (MCU)`、`Full Body Shot`、`Extreme Long Shot (ELS) / Distant View`、`Extreme Wide Shot (Small Subject)`、`Panoramic View`
- 其余 61 条为**机位角度**（静态可用），品类很全：`Eye-Level Shot` / `Shoulder-Level Shot` / `Hip-Level Shot` / `High Angle (Dominating View)` / `Elevated Shot` / `Low Angle (Empowering View)` / `The Hero Shot (Low Angle)` / `Subjective Low Angle` / `Worm's-eye View (Ground Level)` / `Floor Angle` / `Extreme Low Angle (Ground Haze)` / `Bird's-eye View` / `Overhead Flat Lay` / `Overhead (Symmetrical Pattern)` / `Canted Angle / Dutch Tilt` / `Oblique Angle` / `Three-Quarter View` / `Side Profile Shot (90 Degree Angle)` / `Reverse Profile Shot` / `Over-the-Shoulder` / `P.O.V (Point of View)` / `P.O.V (Object Perspective)` / `Counter-Angle` / `Low Horizon Line (Emphasize Sky)` / `High Horizon Line (Emphasize Ground)` / `Centered Composition (Angle Neutral)` / `Off-Center Subject Placement` / `Foreground View (Intense)` / `Wide Angle (Intimate View)` / `Near-Far Juxtaposition` / `Forced Perspective` / `Split Diopter Shot` / `Deep Focus (Compositional Angle)` / `Shallow Focus (Compositional Angle)` / `Point of Focus (Isolated Background)` / `Infinity Focus` / `Through the Keyhole/Peephole Angle` / `Shelf Angle` / `Cross-Section Angle` / `Verticality Emphasis` / `Horizontality Emphasis` / `The Reveal Angle` / `Mobile Phone Angle (Casual/Authentic)` / `Hidden Camera Angle (Candid)` 等
- ⚠️ 其中**动态项须剔除**：`Stabilized Shot (No Camera Shake)`、`Handheld Look (Subtle)`、`Shoulder Mount Look`、`Whip Pan Transition (Implied Angle)`、`The Trinity Angle (Matrix Style)`、`Whip Around Angle`、`Reverse-Dolly Angle`
- Section B 与 C 整节为**运镜**（`Dolly In`/`Crash Zoom`/`Dolly Zoom (Vertigo)`/`Whip Pan`/`Tracking`/`Arc Shot / 360 Orbit`/`Crane / Jib`/`Handheld`/`Steadicam` 等）→ **视频向，生图侧勿用**

#### 3A-8. 景别（shot size）——**本仓最大缺口，第三轮找到**

本轮目标就是补这一块。结论：**官方两侧都没有景别表**（H3 的 `base-en.txt` 没有；BFL 的 cheatsheet nav 承诺 `Shot sizes` 但数据无词条），**只能用第三方**。现有 4 个来源，权威性从高到低的实际情况如下：

| 来源 | 权威性 | 形态 | 本报告位置 |
|---|---|---|---|
| Google 官方 Veo 提示词指南 | **官方（模型方一手）** | 12 个景别词，混在 `Camera angles` 一节，每个带定义 + 示例句 | 视频侧报告 §3B-2 |
| StudioBinder | 影视工业教育站（**非标准制定方**） | 完整阶梯 + 逐条定义（`EWS`→`ECU`，含 `Cowboy Shot`、`Establishing Shot`） | 视频侧报告 §3B-3 |
| `Wayhhow/ai-video-shot-prompt-skill` → `camera-and-composition.md`（**中文**） | **个人仓库（16★）** | **12 条中文景别表**，带「描述 + 适用」两列 | 本节下方 |
| `Rylaispirit/…` 的 `01`（英/越） | **个人仓库（95★，但景别只有 9 条且与角度混表）** | 见 §3A-7b | §3A-7b |

**唯一的中文景别表（`Wayhhow/ai-video-shot-prompt-skill` → `references/camera-and-composition.md`，逐字）**——优势是**免转译**，直接契合本仓生图正文中文：

| 景别 | 描述 | 适用 |
|---|---|---|
| **极特写 ECU** | 仅眼睛、嘴唇、指纹等极小局部 | 情绪高潮、细节揭示 |
| **特写 CU** | 面部填满画面 | 表情、对话、反应 |
| **中特写 MCU** | 头部 + 肩部 | 台词、情感交流 |
| **近景 MS** | 胸部以上 | 对话、过肩镜头 |
| **中景 MLS** | 膝盖以上 | 多人对话、动作起始 |
| **全景 LS** | 全身 | 人物动作、走位 |
| **远景 ELS** | 人物小、强调环境 | 史诗、环境叙事 |
| **大远景** | 几乎看不到人物 | 大场面、空镜 |
| **微距** | 极近距离，小物体 | 昆虫、液体、细节质感 |
| **大广角** | 强烈透视变形，夸张前后景 | 喜剧、动作、恐怖 |
| **鱼眼** | 圆形畸变 | 监控感、运动镜头 |
| **监控/POV** | 第一人称/监控视角 | 沉浸、窥视、恐怖 |

⚠️ **但它的中英映射与工业惯例有偏差，采用前必须校**：该表把 `近景 MS` 定为「胸部以上」、`中景 MLS` 定为「膝盖以上」；而 StudioBinder 的标准是 `Medium shot (MS)` = **腰以上**、`Medium Wide/Long Shot (MWS/MLS)` = **膝盖以上**、`Medium close-up (MCU)` = **胸以上**、`Cowboy Shot` = **大腿以上**。也就是说：**该表的 `MS` 实际等于惯例的 `MCU`，`MCU` 实际等于惯例的 `Cowboy/MS 之间`**，且它**没有 `Cowboy Shot`**。→ **中文标签可用，英文缩写不要照抄**（若 H3 正文里要写英文景别，英文侧请以 Google/StudioBinder 的映射为准）。
⚠️ 该表后两条（`大广角`/`鱼眼`/`监控 POV`）严格说不是景别而是**镜头/视角**，混入了景别表。

### 3B. 未证实 / 已证伪（**不得当事实使用**）

| 条目 | 状态 | 说明 |
|---|---|---|
| `teal-and-orange color grade` | **✅ 已证实（措辞略有出入）** | 读了 `05-style-color-mood.md`，实为 **`Teal and Orange Grading`**（越南语解释：`Da cam / bóng xanh ngọc, Hollywood`） |
| `anamorphic widescreen lens` | **⚠️ 部分证实** | 确切短语**不存在**；同族词存在——`Anamorphic Squeeze / De-Squeeze`、`Anamorphic Bokeh (Oval)`、`Anamorphic Glare (Red/Orange Streak)`、`anamorphic lens flare`（均在 `04-lens-technical-film.md`） |
| 「700+ 相机角度/运动/灯光/构图/调色术语」 | **⚠️ 基本可信（未完全证实）** | `cinematic-video-prompt-skill` 的 6 个词表文件中，**已读的 4 个就有约 510 条**术语；加上未读的 `01`、`06` 两份，700+ **很可能成立**，但因未读全 6 份，不能断言 |
| 「150+ 摄影技法带完整提示词」 | **未核实** | 清单对 `Cinematique` 的说法；该站**未能访问**（SSL 失败），且清单未给 URL |
| 「BFL 建议一个镜头只用一个主要运镜」 | **未证实** | 在 I1、I2 两页内 `one camera` / `per shot` 均 **NO MATCH**；**I3 cheatsheet 已读全，其中也没有这条建议** |
| 「BFL 官方有镜头大小/角度/构图/焦点参考」 | **⚠️ 部分证实** | Cheatsheet（I3）已逐字读全：**角度 15 条 / 构图 9 条 / 焦点 6 条 / 镜头 9 条 / 光线 11 条确实存在**；但 ①**景别（shot size）词条没有**——nav 有 `Shot sizes` 标签，数据里 `Extreme wide` **NO MATCH**；② 属 **FLUX 3**，非本仓在用的 Flux.2 |
| `video-prompt-reverse` 是「电影感词汇库」 | **❌ 未获支持** | 读了该仓库文件树：12 个文件里 `references/` 是 `analysis-contract.md` / `model-adapters.md` / `model_profiles.json` / `optimization-loop.md`，**没有独立术语表**；仓库定位是「反推流程」 |
| `Cinematic-prompt-master`（1★） | **❌ 读了，没有术语表** | 文件树 16 个文件全是多语言 `README*.md` + `SKILL*.md` + `examples/before-after.md`，**无任何 reference/词表文件** |
| `ai-video-shot-prompt-skill`（16★） | **未核实（有文件、未读）** | 文件树里有 `references/keyword-library.md`、`camera-and-composition.md`、`post-production.md`、`templates/style-presets.md`，**正文未读** |
| invideo.io「镜头类型/灯光来源/调色板/情绪」术语表 | **未核实** | 猜的 URL 返回 404；正确路径未定位 |
| Z-Image 官方提示词口径 | **❌ 不存在（已证伪）** | 官方 README 逐字读全，无任何提示词写作内容（结论 4） |
| Flux.2 是否支持负向提示词 | **未证实** | I1、I2 两页内 `negative` 均 **NO MATCH**，官方这两页未提负向提示词 |
| BFL cheatsheet 的**景别**词表 | **❌ 未取到** | nav 承诺 `Shot sizes`，但 `.md` 导出中无景别词条（`Extreme wide` NO MATCH）——**景别仍是本仓最大缺口，本次未补上** |
| `01-camera-angles-and-movement.md`（Rylaispirit） | **未读** | 额度耗尽；该文件最可能含 **framing/景别**，是下次第一优先 |

---

## 4. 与本仓生图侧的对接点（含冲突风险）

### P1. `src/minimax_h3_prompt/prompts/image_prompt_engineer.md` —— 字段名齐全，缺词表

「内容要求」段（`:15`）已要求描述「主体、外观或材质、姿态/摆放、**构图**、环境、**光线**、时代和**写实视觉风格**」——**字段维度与 BFL 官方 JSON 的 `composition` / `lighting` / `style` 基本对得上，缺的就是每个维度下的受控词汇**。这与视频侧的缺口形态完全同构（那边 `cinematographer.md` 也只给字段名不给词）。
→ 落点：本文件「内容要求」段，加一张按维度分组的词表；**构图那一组必须再按 zimage / flux2 分成两套**（见 C1）。

### P2. `src/minimax_h3_prompt/prompts/frame_prompt_engineer.md` —— 字段更多，同样无词表

「内容规则」段（`:56`）要求「主体、外观/服装、关键道具、人物与道具关系、场景地点、**空间性质**、**构图**、**光线**、时代和**写实视觉风格**」——比 P1 多「空间性质」「人物与道具关系」两项，仍**无任何词表**。该文件另有 `scene_anchor` 与 `continuity_constraints`（跨帧一致性的显式锚），与光线/色调词表**天然互补**：光线方向与色温是跨帧最容易漂移的项，应同时进 `continuity_constraints`。

### P3. `src/minimax_h3_prompt/generation.py` —— 两模型画布不同（本轮新增的关键事实）

`_IMAGE_PROFILE_DEFAULTS`（`:25-28`）：`zimage → ("zimage_t2i_v1", "10", 640, 1280, "candidate")`、`flux2 → ("flux2_t2i_v1", "118", 1024, 1024, "candidate")`。字段含义见 `ImagePromptVariant`（`:109-122`）的 `width` / `height`。
→ **zimage 是 640×1280（1:2 竖构图）、flux2 是 1024×1024（正方）**。构图词表若不按模型区分，会给出物理上落不了的指令（例如对正方画布说「左侧留白三分之一」）。

### P4. 负向提示词的接线**已经存在**（不是死规则）

`generation.py:669-670` 与 `:706-707` 会把非空 `negative_prompt` 渲染成 `### Negative Prompt` 段输出。所以 `image_prompt_engineer.md:21` / `frame_prompt_engineer.md` 的「默认不输出负向」**不是死参数**——一旦输入说明目标工作流有独立负向输入，该字段确实会被消费。
⚠️ 但「Flux.2 是否支持负向提示词」在本次调研中**未证实**（I1/I2 两页均未提）。即该规则的**依据不在本次调研覆盖范围内**，不要拿本次结果去支撑或推翻它。

### 冲突风险清单（明确标出，不和稀泥）

| 编号 | 冲突 | 证据 | 处置建议 |
|---|---|---|---|
| **C1** | **构图词 × 两模型画布比例不同** | `generation.py:25-28`、`:119-120` | 构图词表**分模型两套**；zimage 按 1:2 竖构图写（主体居中的纵向空间、上下留白），flux2 按 1:1 写。这条同时给「两模型必须分别重写」补上了物理依据 |
| **C2** | **外部视频提示词库 × 本仓禁运镜** | `image_prompt_engineer.md:19`、`frame_prompt_engineer.md:72`（禁 camera movement / 剪辑 / 时间码 / H3 三段式） | 清单里 4 个 GitHub 仓库**全是 video skill**，核心内容（运镜、时序、节奏）**不可直接搬**。只能取其中的**静态子集**：镜头焦段、光线来源与质感、构图、影调、材质。凡带运动语义的词一律剔除 |
| **C3** | **「不要堆砌无意义的质量标签」× BFL 官方示例本身就是短语堆叠** | 本仓 `image_prompt_engineer.md:20`；BFL I1 官方示例 `Samsung Galaxy S25 Ultra product advertisement, 'Ultra-strong titanium' headline, ..., professional product photography` | **这是本仓自设纪律，不是模型限制**——BFL 官方示例里就有逗号分隔的短语堆叠与 `professional product photography` 这类质量词。引用外部库时按本仓纪律过滤即可，**不要误以为模型不认**，也不要因为官方这么写就放宽本仓纪律 |
| **C4** | **中文正文 × BFL 官方英文 JSON 键** | 本仓 `image_prompt_engineer.md:22`「正文语言使用中文；模型不认识的专有名词可保留原文」；frame 侧 `:56`「中文自然语言」；BFL I1 示例键值全英文 | **本轮新增裁定点**：若走结构化 JSON 路线，需先定「英文键名 + 中文值」还是全中文。⚠️ 与上一份报告的 **C6（H3 视频正文语言）是两个独立问题**，不要混为一谈：生图侧文件**明确写了中文正文**，本身**不自相矛盾**；视频侧才是 `cinematographer.md:16` 与 `prompt_engineer.md:48` 互相打架 |
| **C5** | **Z-Image 侧无一手依据** | I4（官方 README 无提示词内容） | 凡「Z-Image 官方推荐写法」的说法**都没有来源**。Z-Image 侧只能沿用本仓现有的「中文自然语言完整描述」口径，并按 `base-en.txt` 等**同厂一手**风格词补充 |
| **C6** | **构图词表 × `scene_anchor` 的跨帧约束** | `frame_prompt_engineer.md` 输出格式（`scene_anchor` / `continuity_constraints`） | 光线方向与色温若写进首帧而不写进 `continuity_constraints`，两帧很可能漂移。建议光线/色调类词表同时接入该字段（呼应「首帧尾帧共享 scene_anchor」的既有要求） |

### 本轮新读到的源带来的**追加冲突项**

读完 BFL cheatsheet（I3）与 Rylaispirit 四个词表（I7）后，新增四条：

| 编号 | 冲突 | 证据 | 处置建议 |
|---|---|---|---|
| **C7** | **两个新源都是「英文术语表」× 本仓生图正文必须中文** | `image_prompt_engineer.md:22`、`frame_prompt_engineer.md:56`；I3 全英文；I7 左列英文、**右列是越南语**（不是中文） | 词表**不能直接粘贴**，需转译成中文再进正文。**例外**：`Color Palette [Specific Hex Codes]`（I7）与 BFL 官方 hex 控制（§3A-3）是**跨语言安全**的——hex 码不涉及翻译，建议优先用 hex 锁色。这同时是 C4（JSON 键值语言）的具体化 |
| **C8** | **新源含大段动态词 × 本仓禁运镜/时间码** | I3 的 `movements`(18) / `time`(10) / `transitions`(8) / `pov`(4)（§3A-6 已单列）；I7 `04` 的「快门与运动」整节：`motion blur`、`Shutter Speed 1/8000s`、`Shutter Angle 180 Degrees`、`long exposure for light trails`、`time-lapse`、`Film Stutter (Choppy Motion)`、`Camera Shake (Digital Emulation)`、`rack focus / Focus Pull` | 这些**一律不进生图提示词**（已在 §3A-6/§3A-7 用 ⚠️ 标出）。取词时按小节整块剔除，别逐条挑 |
| **C9** | **新源含质量标签堆叠 × 本仓「不堆无意义质量标签」** | I7 `04` 里的 `8K Resolution Photography / 8K / 4K / hyperdetailed`、`Hyperrealistic Sharpness`、`Photorealistic CGI (No Imperfections)` | 按 C3 的结论处理：这是本仓自设纪律，按纪律过滤即可。注意 `hyperrealistic / photorealistic`（在 `05`）属**有意义的风格词**，与 `8K`/`4K` 这类纯分辨率标签**不是一类**，不必一刀切 |
| **C10** | **景别词表仍然空白** | I3 的 nav 承诺 `Shot sizes` 但数据无此节（`Extreme wide` NO MATCH）；I7 的 `01-camera-angles-and-movement.md` 未读 | ~~第三轮已解决~~ → **已找到，见 §3A-8**（中文景别表来自 I9，英文景别见 Google/StudioBinder）。但**权威性都是第三方**：官方两端（H3 与 BFL）均无景别表 |
| **C11** | **`Wayhhow` 的中文方法论 × 本仓两条纪律（本轮新增，冲突最直接）** | `references/keyword-library.md`（I9）全文：第 1 节「**去 AI 味核心关键词（强制使用）**」列为 `超写实`/`极致逼真`/`真人实景拍摄`/`电影动作捕捉`，即**质量标签**；第 2 节「限制词（反向约束）」全是 `杜绝动作僵硬`/`杜绝游戏CG感`/`杜绝塑料皮肤`/`杜绝镜头抖动过度`/`杜绝影子方向错误` 等**负向祈使句** | **两节都不能照搬**：① 撞 `image_prompt_engineer.md:20`「不要堆砌无意义的质量标签」；② 撞本仓「默认不输出负向」，且与 Google S3 的 Negative prompts 口径**正好相反**（S3 原文明确说 `Not recommended: using instructive language or words such as "no" or "don't"`），也与 Runway 的正向措辞原则相反。**可用的只有它的风格关键词、色彩影调、光线描述三节**（都是中文名词短语，无祈使句） |
| **C12** | **`Wayhhow` 景别表的中英映射与工业惯例不一致** | 见 §3A-8 的对照：该表 `近景 MS`=「胸部以上」、`中景 MLS`=「膝盖以上」；StudioBinder 标准为 `MS`=腰以上、`MWS/MLS`=膝盖以上、`MCU`=胸以上 | **中文标签可用，英文缩写不要照抄**。若 H3 正文写英文景别，映射以 Google S3 / StudioBinder 为准（官方示例只出现过 `a medium-wide shot` / `a close-up` / `an extreme close-up` / `a medium shot` / `a close shot` / `a wide shot` 6 个） |

---

## 5. 未能证实项 + 对那份清单的逐条验真结果

### 5.1 逐条验真（清单共 8 项 + 1 项补充）—— 第三轮收口后的最终结果

| 清单条目 | 验真结果 | 依据 |
|---|---|---|
| GitHub `cinematic-video-prompt-skill`（700+ 术语） | **✅ 仓库存在 + 有货 + 「700+」成立**：6 个词表文件**已全读**，约 **700 条**术语（`01` 70 行含角度与 9 条景别 / `02` 83 / `03` 68 / `04` 151 / `05` 212 / `06` 175） | I7；6 文件全文 |
| GitHub `Cinematic-prompt-master`（平淡场景→电影级） | **❌ 读了，没有术语表**：16 个文件全是多语言 README/SKILL + `examples/before-after.md` | I8；文件树 |
| GitHub `ai-video-shot-prompt-skill`（中文分镜工作流） | **✅ 有货且不可替代**：`camera-and-composition.md` 含**唯一的 12 条中文景别表**（§3A-8）；`keyword-library.md` 是中文关键词库。其余文件（`post-production.md` / `templates/style-presets.md`）未读 | I9；2 文件全文 |
| GitHub `video-prompt-reverse`（电影感词汇库） | **❌ 「词汇库」说法未获支持**：文件树无独立术语表，`references/` 是流程类文档（`analysis-contract` / `model-adapters` / `model_profiles.json` / `optimization-loop`）。但清单给的两个**具体词**在**另一个仓库**（`cinematic-video-prompt-skill`）里证实了 → 清单可能把两个仓库的内容搞混了 | I10 文件树 + I7 全文 |
| 在线 `Cinematique`（150+ 技法） | **未核实**：清单未给 URL；`cinematique.ai` 与 `cinematique.io` **两个 TLD 均 SSL/TLS 握手失败** | curl exit 35 ×2 |
| `PromptHero (megatek.ai)` | **未核实**：清单未给 URL，未抓取 | — |
| **Black Forest Labs 官方文档** | **✅ 已读到且比清单说的更多**：cheatsheet 逐字读全，**角度 15 / 构图 9 / 焦点 6 / 镜头 9 / 光线 11 / 画幅 4 / VFX 12 / 动画 4 / 运动 18 / 时间 10 / 转场 8 / POV 4 = 110 条**。但 ①**景别词条没有**（nav 承诺、数据无）；② 属 **FLUX 3**，非本仓在用的 Flux.2；③「一个镜头只用一个主要运镜」**仍未证实** | I3 全文 |
| `invideo.io` AI 视频提示词指南 | **未核实**：清单未给 URL；`/blog/ai-video-prompt-guide/` **HTTP 404**，`/sitemap.xml` 只返回 2662 字节索引页、**无含 `prompt` 的 URL** | curl 404 + sitemap |
| （补充）Z-Image 官方提示词口径 | **❌ 不存在该内容**：官方 README 无提示词写作部分 | I4 |

**汇总（第三轮收口后的最终结论）**：**0 条被证伪为「仓库不存在」**；4 条 GitHub 仓库名全部为真。其中 **2 条确认有货**——`cinematic-video-prompt-skill`（95★，**6/6 文件全读，约 700 条**术语）与 `ai-video-shot-prompt-skill`（16★，**中文**，含**唯一的 12 条中文景别表**）；**1 条读完确认没有术语表**（`Cinematic-prompt-master`，1★）；**1 条「词汇库」说法未获支持**（`video-prompt-reverse`）。BFL 官方 cheatsheet **110 条已读全**（但**无景别**、且属 FLUX 3 而非 Flux.2）。**清单举的两个具体词 `teal-and-orange` / `anamorphic` 已证实存在**。仍未核实：`Cinematique`（`.ai` 与 `.io` **两个 TLD 都 SSL 失败**；清单未给 URL）、`invideo.io`（路径 404、sitemap 无 prompt URL）、`PromptHero`（未抓）。
**关于「700+」：第三轮读全 6 个文件后，合计约 700 条（70+83+68+151+212+175 = 759 行表格含表头/分隔行，净术语约 700）→ 清单的「700+」说法成立。**

### 5.2 其他未能证实项

1. **三轮各用满 10 次 curl（合计 30 次）**。仍有三处未读：`Wayhhow` 的 `post-production.md` 与 `templates/style-presets.md`（该仓库是本轮唯一的中文源，值得补）、`Rylaispirit` 的 `examples/example-prompts.md`。
2. **官方两端都没有景别表**——这一条三轮下来是**确定的结论**，不是「没找到」：H3 的 `base-en.txt`/`ref-en.txt` 无景别表（官方例文只出现过 6 个景别词），BFL 的 cheatsheet nav 承诺 `Shot sizes` 但数据无词条。→ **本仓若要用景别，只能引第三方，且必须标注来源权威性**（§3A-8 已按官方 / 工业站 / 个人仓库三档标出）。
3. **BFL `Prompting Basics`（`/guides/prompting_summary`）未读**——URL 已定位，额度用尽。
4. **Flux.2 负向提示词支持与否未证实**（I1/I2/I3 三页均未提）。
5. **Z-Image 侧完全没有一手提示词依据**（官方 README 无此内容；HuggingFace / ModelScope 模型卡与官方博客未抓）。
6. **BFL cheatsheet 是 FLUX 3 的参考，不是 Flux.2 的**——本次全部 BFL 术语都带这个模型错配。
7. `Cinematique` 的 SSL 失败（两个 TLD）**不能证明该站不存在**——只证明在本环境不可达，且清单未给 URL，故**既未证实也未证伪**。
8. **未检索**：ASC（美国摄影师协会）、No Film School、GitHub 上 star 更高的图像提示词库、HuggingFace/ModelScope 上 Z-Image 与 Flux.2 的模型卡。**是「没查」不是「没有」**。
9. **本文 §3A 不含任何凭记忆补写的摄影术语。** §3A-1~3A-8 的每一条都能追到 URL 与逐字原文；凡未读到原文的，只出现在 §3B。

### 下次调研建议（按性价比排序）

1. 读 `Wayhhow` 的 `post-production.md` 与 `templates/style-presets.md`——**唯一的中文源**，若其风格预设也是中文名词短语（不含质量标签/祈使句），可直接进本仓而**免去 C7 的转译成本**。
2. 读 BFL `Prompting Basics`（`/guides/prompting_summary`），补齐官方基础原则一侧。
3. 裁定 **C4（BFL JSON 键值语言）** 与 **C1（两模型画布不同）** 再动词表。
4. 若坚持要有权威景别表：官方无解，只能接受第三方；建议**中英分开取源**——中文标签用 I9（须按 C12 校正），英文标签用 Google S3（官方，12 词）。
5. `Cinematique` / `invideo` 两条若仍要查，需先拿到**真实 URL**（当前清单未给，且换过 TLD 与 sitemap 均失败）。
