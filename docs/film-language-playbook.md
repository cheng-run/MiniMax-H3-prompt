# 提示词的电影语言速查（我学会了什么 + 怎么用）

2026-09-26 起，我（Claude）写提示词时按这份执行。**视频侧与生图侧分开成章**：
第一~九节是**视频侧（MiniMax H3）**，第十节是**生图侧（Flux.2 / Z-Image）**——两套语言不能混用。

**证据链**在两份调研报告里（调研过程、逐条核实/证伪、未能证实的项全在里面）：
- 视频侧：`docs/research/2026-09-26-film-language-prompt-sources.md`
- 生图侧：`docs/research/2026-09-26-image-prompt-language-sources.md`

**这份是结论**——写的时候只看这一份就够。

来源标注约定：`[官方]` = MiniMax H3 官方规范（仓内 `src/minimax_h3_prompt/references/`，逐字原文）；
`[已核实]` = 2026-09-26 用 curl 打开原页逐字核到的外部材料；`[仓内]` = 本仓自己的实测纪律。

---

## 一、写一句运镜时

`[官方]` 完整运镜 = **motion type**（怎么动）+ **amplitude**（构图变化幅度）+ **speed**（快慢），三者同一句里写成自然英文动作。
`medium amplitude` 与 `normal speed` 通常省略（`base-en.txt:102`）。

**白名单（官方列出的全部表达，只有这些）**：

| 维度 | 可用表达 |
|---|---|
| Motion type | `Zoom In / Zoom Out`、`Push In / Pull Out`、`Pan Left / Right`、`Truck Left / Right`、`Tilt Up / Down`、`Pedestal Up / Down`、`Arc Shot`、`Tracking Shot`、`Static Shot`、`Shake Slightly / Strongly`、`POV`、`Roll Clockwise / Counterclockwise` |
| Amplitude | `with small amplitude` / `with large amplitude` |
| Speed | `at slow speed` / `at fast speed` |

**写法铁律**（`base-en.txt:123` 原文）：写成镜头内的自然英文动作，**不许在句尾堆标签**。官方例句逐字：

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the open doorway.
The camera holds a static shot as the runner exits the frame.
```

`[已核实]` **速度修饰不可省**：每个运动词都该带 slow / deliberate / steady / rapid，或明确时长（如 `slow 5-second pan right`）。
`[已核实]` **机位-主体的关系用动词写明**——原话是「主体-机位关系是最贵的遗漏」：

```text
Camera follows the cyclist.
Camera trucks left, matching the runner's speed.
Camera leads the dancer, pulling back as she advances.
```

`[已核实]` **多阶段运镜写「每阶段画面里有什么」，不要堆动词**：
✅ `The camera arcs around the seated figure, then rises above the table to reveal the empty chairs surrounding her.`
❌ `Orbit the subject and crane up and push in.`

---

## 二、写一个镜头块时：官方六要素

`[官方]` `ref-en.txt:7` 原文要求每镜必须确立：
**composition（构图）· subject appearance and position（主体外观与位置）· environment and lighting（环境与光线）· actions and state changes（动作与状态变化）· camera movement（运镜）· current sound（当下声音）**，
并明确禁止把描述退化成**剧情摘要**或**参考关系罗列**。

---

## 三、七个格子的排序（写一镜的骨架）

`[已核实]` Runway 的排序公式（原文）：

```text
[Shot size] + [Angle] + [Movement + speed] + [Subject & action] + [Lens/look] + [Lighting/mood] + [What the shot reveals]
```

原文示例逐字：

```text
Medium close-up, low angle, slow dolly push-in over 4 seconds, on a detective lighting a
cigarette in the rain, 35mm lens with shallow depth of field, neon reflections.
The push-in reveals the tension in his face.
```

第 7 格（**这镜揭示了什么**）是我以前从来没写过的一格，也是最像「导演」的一格。

---

## 四、词表

### 景别

`[已核实]` StudioBinder 阶梯（宽→近）：`Extreme Wide Shot (EWS)` / `Long Shot (LS)` / `Wide Shot (WS)` / `Full Shot (FS)` / `Medium Long Shot (MLS)` / `Medium Wide Shot (MWS)` / `Cowboy Shot` / `Medium Shot (MS)` / `Medium Close Up (MCU)` / `Close Up (CU)` / `Extreme Close Up (ECU)` / `Establishing Shot`。
关键定义（原文）：`Full shot` = head to toe；`Medium wide` ≈ 膝盖以上；`Medium close up` ≈ 胸口以上；`Establishing shots have no rules`。

`[官方]` ⚠️ **官方 H3 规范没有景别表**——例文里只出现过这 6 个：`a medium-wide shot`、`a close-up`、`an extreme close-up`、`a medium shot`、`a close shot`、`a wide shot`。
→ **保守用法：优先用这 6 个**；用 StudioBinder 那些更细的（MCU / cowboy shot）属于扩张，可以试，但要清楚它不在官方例文里。

### 机位角度

`[已核实]` `Eye-level`、`Low angle`（力量感、压迫感）、`High angle`（脆弱、孤立、交代地理）、`Over the shoulder (OTS)`、`POV`、`Top-down / aerial`、`Bird's eye view`、`Worm's eye view`（压迫、幽闭、不安）、`Dutch angle`（失衡——原文特别标注 **Use sparingly**）。

### 焦点与构图

`[已核实]` 焦点：`Deep focus`、`Shallow focus`、`Soft focus`（全画面柔散——回忆/梦/浪漫）、`Rack focus`（镜头内焦点在景间转移，不动机位就转移注意力）。
`[已核实]` 构图：`Leading lines`、`Frame within frame`、`Symmetrical`、`Negative space`（主体小、留白大——孤立、尺度感）。

### 光线

`[官方同族]` MiniMax 官方仓库 `asset-prompt-guide.md` 的一手光线词：`soft studio lighting`、`golden hour backlight`、`flat diffused light`；
构图写法示例：`left-aligned subject with negative space on the right for text overlay`；技术规格：`4K resolution, sharp focus, shallow depth of field`。
`[官方]` H3 例文里的光线/调色措辞：`soft lighting and a slightly desaturated color palette`、`warm indoor lighting`。

### 风格词

`[官方]` 只从这些里选：`Cinematic`、`live-action`、`2D-animated`、`3D CG`、`claymation`、`watercolor`、`vintage film`。
`[已核实]` 风格词还可借：`editorial photography`、`3D render`、`flat vector illustration`。

---

## 五、写法纪律（每条都有出处）

| 纪律 | 依据 |
|---|---|
| 运镜写成句，不许句尾堆标签 | `[官方]` `base-en.txt:123` |
| 速度修饰不可省 | `[已核实]` Runway |
| 机位-主体关系用动词写明 | `[已核实]` Runway |
| **`cinematic`、`4k` 这类词不干活，删掉**（原文注释：`they're doing no work`） | `[已核实]` Runway |
| 正向措辞，别写 `no` / `don't`；要排除的用名词（`wall, frame`） | `[已核实]` Google（S3 Negative prompts） |
| 静态镜头最难落地 → 先写画面里该有的运动，再补一句自然语言**加固** | `[已核实]` Runway |
| 静态加固句（实测**不命中**本仓快机位正则，可安全用）：`The camera is entirely motionless for the duration of the scene, with movement only occurring from the subject.` | `[已核实]` Runway + `[仓内]` 我实跑验证 |
| 只改距离或轻微角度时**优先用运镜，别切镜** | `[官方]` `base-en.txt:98` |
| 一镜一运镜只是漂移时的**兜底**，不是通则（Runway 明说鼓励组合运镜） | `[已核实]` Runway — 但见下节，本仓另有硬约束 |

---

## 六、本仓硬约束（不得违反，与上面任何外部建议冲突时以本节为准）

| 约束 | 位置 |
|---|---|
| 切点必须**整秒**（毫秒位 `.000`），每镜隐含时长必须 **4-10 秒整数** | `[官方]` `base-en.txt:86` |
| 时间戳相对**本段起点**，绝不许写绝对片时 | `[仓内]` `CONTEXT.md:97`（GEN003 坏片实测根因） |
| 每镜块必须以官方 edge-stability 句**逐字符**收尾 | `[官方]` `base-en.txt:92-96` |
| 默认**单镜头**；段内切镜是显式例外 | `[仓内]` `prompt_engineer.md:51` |
| **快机位（fast/rapid/swift/whip + 机位词、`at fast speed`）+ ≥3 个动作节拍不得同段共存**（error）；默认处置是**降机位为静态/慢速**。引用时必须声明：该阈值量于 **4 步采样** | `[仓内]` `segment_prompts.py:35-41, 673-705`、ADR 0003 |
| 最后一个节拍距段尾 **≥1s** | `[仓内]` `prompt_engineer.md:52` |
| 禁止自创结构（`GLOBAL_LOCK:` / `BRIDGE_FROM:` / `END_HOOK:` / 首行时长句） | `[官方]` 无此结构；validator 报 error |

**「节奏/剪辑」的物理天花板**：一个 4-10 秒的执行段装不下第二个 ≥4 秒的镜头（切点整秒 + 每镜 4-10 整数秒），所以**节奏只能靠运镜幅度速度 + 动作节拍做**，不能靠多切快切。

---

## 七、我不会用的东西（都是一手证据支持「不用」）

- **不在官方白名单的运镜名**：`crane`、`handheld`、`orbit`、`crash zoom`、`dolly zoom`、`whip pan`、`Steadicam`、`Gimbal` —— 官方只列了 12 个 motion type（`base-en.txt` §4.3 表里 `Motion type` 的行数；2026-09-27 更正，原写 13 是错数）。尤其 `crash zoom` / `snap zoom` 语义就是极快，会绕过本仓快机位正则（快词表只有 `fast|rapid|swift|whip`），用了等于钻闸门空子。
- **Hailuo 的方括号运镜指令**（`[Truck left]` 等）：那是 Hailuo **API 产品线**的能力；官方模型表只有 `MiniMax-Hailuo-2.3` / `MiniMax-Hailuo-02` / `T2V-01-Director` / `T2V-01`，**H3 不在其中**，方括号结构与 H3 的 ComfyUI 提示词格式是两套东西，照搬即自创结构。
- **把景别/光线/镜头词写进静态生图提示词**：本仓明令禁止（`prompt_engineer.md:31`），生图侧和视频侧是两套词表。
- **「一镜一运镜」当成通则**：Runway 原文明确说 `combining camera terms is encouraged`、`One move per clip is the reliable fallback, not the rule` —— 它论的是运镜**个数**，与本仓「机位速度 × 节拍数」不是同一个维度，**不能互相引证**。
- 未能核实的源（Sora cookbook 403、Runway Gen-4 403、`ai.google.dev` 超时）的内容：**一律不引用**。

---

## 八、前后对照（实证：这是本仓一份真实交付过的坏样本）

样本：`tests/fixtures/flicker_arms/gen004_shot02.md`（GEN004 段 2，当年实际交付、被判闪动的提示词）。

### 原件（节选关键处）

```text
integrated_multimodal_description: [Shot 1] Live-action, cinematic, a fast low tracking shot with
small amplitude at fast speed frames the interior of a small convenience store at night; ...
```

问题（对照第一~六节）：**无景别**（`a fast low tracking shot` 不是景别）、无机位角度、无构图、无「揭示什么」；`cinematic` 是不干活的词；**缺官方 edge-stability 收尾句**。

校验器实跑：

```
error:FAST_CAMERA_MULTI_BEAT_COEXIST
warning:EDGE_STABILITY_MISSING
```

### 改写（**节拍一个没动**，同样的 4 个时间戳，只补电影语言）

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a low-angle medium-wide shot frames
the interior of a small convenience store at night, the checkout counter held in the right third of the
frame while the tiled aisle runs away from the camera toward it as a leading line. Cold night light rakes
in from the ajar glass door at the left edge and lays a hard bright rim along the floor tiles and the
counter edge, while the store's own overhead strip leaves the aisle in a soft, faintly green ambience.
A lean orange tabby cat with short dense fur and faint darker striping on its legs is already on the tiled
floor past the doorway, body low and driven forward. The camera tracks slowly with small amplitude as the
cat breaks into a straight-line sprint down the aisle, holding the counter as the only destination in
frame. At 00:00.500, the glass door is pushed further open and the night outside throws a thin rim of cold
light along the cat's back and tail, claws ticking on the tile. At 00:01.700, the cat reaches the counter
and springs up; at 00:02.300 its four paws land flat on the counter top next to a black handheld barcode
scanner sitting in its fixed cradle, the claws pulling in and the tail dropping low and flat against the
counter. At 00:03.000, a store clerk's right forearm and hand in a dark uniform sleeve swings into frame
from the right and stops hanging in the air above the counter, fingers open and motionless, the clerk's
body gone rigid behind it. The framing never leaves the counter, so the frozen hand arrives as an
intrusion into a space the cat has already claimed. Keep every character's silhouette, facial outline,
and clothing edges crisp and stable throughout; no rippling, warping, or edge shimmer.

overall_soundscape: Bright glass-door swish and a faint door chime from the opening entrance, over the low
hum of store refrigerators and compressors. Soft paw impacts tick across the tile floor and then thud
dully as four paws hit the counter top. A short fabric rustle accompanies the sleeve that swings into
frame, and the store holds a thin, quiet night ambience underneath.

non_diegetic_music: A tense, low pulsing synth drone with staccato pizzicato strings that accelerate in
step with the sprint, pitched for intrusion rather than playfulness. The pulse tightens and cuts to a
single sustained low note as the paws land and the hand freezes.
```

动的地方：**景别+角度**（`a low-angle medium-wide shot`）、**构图**（柜台占右三分、过道作引导线）、**光线分层**（左侧冷硬边缘光 vs 顶部柔光）、**运镜降速**（快跟踪 → `tracks slowly with small amplitude`）、**补第 7 格**（`The framing never leaves the counter, so the frozen hand arrives as an intrusion...`）、**逐字符补官方 edge-stability 句**。

校验器实跑：

```
全绿（无任何 issue）
```

注：`overall_soundscape` 与 `non_diegetic_music` 原文照留未动（本次只检验电影语言那部分）。

---

## 九、我落笔前的自查（写完一段提示词就跑一遍）

1. 这一镜的**景别**写了吗？（优先官方例文那 6 个词）
2. **机位角度**写了吗？
3. **构图**有没有具体到「谁在画面哪一侧、前景/背景是什么关系」？
4. **光线**有没有方向、强度、质感？（不是笼统的「光线柔和」）
5. **运镜**是不是三要素齐全、白名单内、写成自然句、速度修饰没省？
6. 有没有一句话说清**这镜揭示了什么**？
7. `cinematic` / `4k` 这类不干活的词删了吗？
8. 官方 **edge-stability 句**逐字符在镜块收尾了吗？
9. 跑校验器：无 error；顺带确认**快机位 + ≥3 节拍**没踩（踩了就降机位，别压节拍）。

---

# 十、生图侧（静态画面）：Flux.2 与 Z-Image

生图是**静态画面**，与视频侧是两套语言。**运镜、剪辑、时间码、H3 三段式一律不写**——写了就是错。

## 10.1 四条本仓硬约束

| 约束 | 位置 |
|---|---|
| 不写 video 镜头运动 / 剪辑 / 时间码 / H3 三段式 | `image_prompt_engineer.md:19`、`frame_prompt_engineer.md:72` |
| 不堆砌无意义质量标签 | `image_prompt_engineer.md:20` |
| 正文中文（模型不认识的专有名词可保留原文）；**两模型必须各自重写**，不能同一句 | `image_prompt_engineer.md:22-23` |
| 默认不输出负向提示词（除非输入说明工作流有独立负向输入） | `image_prompt_engineer.md:21` |

⚠️ 第 4 条**不是死参数**：`generation.py:669-670`、`:706-707` 会把**非空** `negative_prompt` 渲染成 `### Negative Prompt` 输出，实测确实会被消费。默认关是纪律，不是字段没人要。
（另：Flux.2 官方两页**都没提**负向提示词——「官方支不支持负向」本次**未证实**，别拿它当依据。）

## 10.2 两个模型不是一回事：画布不同 → 构图词必须分两套

`generation.py:26-27` 实测：

| 模型 | profile / 节点 | 画布 | 构图含义 |
|---|---|---|---|
| **Z-Image Turbo** | `zimage_t2i_v1` / 节点 `10` | **640×1280 = 1:2 竖构图** | 纵向空间、上下留白 |
| **Flux.2** | `flux2_t2i_v1` / 节点 `118` | **1024×1024 = 正方** | 居中/对称更自然，横向留白受限 |

→ 同一条构图指令在两种画布上**语义不同**：「左侧留白三分之一」在 1:2 竖幅与正方形里落出来的画面不一样。**构图组按模型分两套写**。这也是既有纪律「两模型必须分别重写」的**物理依据**，不只是纪律要求。

→ **一手依据只有 Flux.2 一侧**：Z-Image 官方 README（`Tongyi-MAI/Z-Image`，2026-09-26 逐字读过 21757 字节）只有模型结构/推理/榜单，**没有任何提示词写法指导**。所以 Z-Image 侧沿用本仓「中文自然语言完整描述」口径 + 同厂官方风格词，**永远不要声称有「Z-Image 官方写法」**。

## 10.3 Flux.2 官方推荐的结构化提示词（一手，逐字）

`docs.bfl.ai/flux_2/flux2_text_to_image` 官方原文：`Use JSON-structured prompts for precise control over generation — ideal for production workflows and automation.` 官方字段示例逐字：

```json
{
  "subject": "...",
  "background": "...",
  "lighting": "soft gallery lighting, warm spotlights",
  "style": "digital art, high contrast",
  "camera_angle": "eye level view",
  "composition": "centered, portrait orientation"
}
```

`lighting` / `style` / `camera_angle` / `composition` 四个字段正是本仓生图侧缺的**四类静态画面语言**，而本仓本来就在输出 JSON，接这条几乎零结构改动。

**我的默认执行口径**（本仓要中文正文 + 专有名词保留原文）：**英文键名 + 中文值**。要改成全中文键，你说一句即可。

## 10.4 静态画面词表（一手 / 已核实，可直接用）

### 10.4-A BFL 官方 Examples & Cheatsheet（同厂一手，**最优先**）

来源 `https://docs.bfl.ai/guides/prompting_video_camera_terms`（2026-09-26 逐字读全，60875 字节）。
⚠️ **两条限定**：① 它是 **FLUX 3** 的参考，**不是本仓在用的 Flux.2**（同厂不同模型，措辞可用、但别声称「Flux.2 官方词表」）；② 页内导航列了 `Shot sizes` 标签，**实际数据里没有景别词条**（`Extreme wide` 全页 NO MATCH）→ **景别仍是本仓最大的未补缺口**。

**Angles 机位角度（15）**
`Aerial`（高空俯瞰）· `Low angle`（仰看，加尺度/支配感）· `High angle`（俯看，压缩或暴露）· `POV`（主体视角）· `Over-the-shoulder`（越过前景主体拍动作）· `Dutch angle`（地平线倾斜，不安/紧张）· `Worm's eye`（极低角直上）· `Bird's eye (top-down)`（正上方）· `Eye level`（与主体眼平）· `Ground level`（贴地横扫）· `Profile shot`（严格侧面）· `Tableau`（静止对称的舞台式宽画）· `Fourth wall`（主体直视镜头说话）· `Object POV`（物体视角）· `Voyeur`（遮挡偷窥视角）

**Composition 构图（9）**
`Leading lines`（用线条引视线）· `Center framing`（主体锁中间）· `Rule of thirds`（偏心求平衡）· `Symmetry`（镜像，精确或张力）· `Negative space`（主体周围留白）· `Frame within frame`（用场景中的开口框主体）· `Foreground occlusion`（前景虚化物增纵深）· `Silhouette`（逆光剪影）· `Reflection framing`（用反射面构图）

**Focus 焦点（6）**
`Shallow depth of field` · `Deep focus` · `Rack focus`（⚠️ 镜头内焦点转移，**生图勿用**）· `Split diopter`（近远都清晰）· `Focus breathing reveal`（⚠️ 动态，**生图勿用**）· `Tilt shift`（微缩景观感的选择性对焦）

**Lenses 镜头/光学（9）**
`Wide angle (24mm)`（空间夸张、边缘畸变）· `Telephoto compression`（压平纵深、叠层）· `Fisheye` · `Anamorphic flares`（横向光条 + 椭圆散景）· `Macro lens`（极微距）· `Probe lens`（探针镜头，低穿窄空间）· `Halation`（高光周围光晕）· `Parallax`（⚠️ 纵深各层不同速滑动，偏动态）· `Vignette`（暗角框亮心）

**Lighting 光线（11）**
`Rim light`（逆光勾边）· `Chiaroscuro`（强明暗对比）· `Golden hour`（暖低日逆光）· `Neon practicals`（场景内彩光）· `Volumetric light`（雾中可见光柱）· `Hard light`（硬边阴影高对比）· `Haze`（厚大气显光柱）· `Spotlight`（单束光把主体从暗里isolate）· `Light flash`（频闪冻结运动，⚠️ 偏动态）· `Projections`（投影落主体）· `Underwater light`（水面焦散晃动，⚠️ 偏动态）

**另有三类偏风格、静态图亦可用**：`format`（`Cinemascope (21:9)` / `Vertical (9:16)` / `Vintage (4:3)` / `Split screen`）；`vfx`（`Double exposure` / `Datamosh` / `Kaleidoscope` / `Morphing` / `Slit scan` / `X-ray` / `Levitation` / `Dreamcore` / `Dystopian` / `Magical realism` / `Maximalism` / `Diorama`）；`animation`（`Stop motion` / `Pixel art` / `Zoetrope` / `Kinetic typography`）。

**⛔ 同页的四类视频向语言——生图侧勿用**（只登记）：`movements`（18：Pan / Tilt / Dolly in / Tracking / Orbit / Crane / Handheld / Whip pan / Dolly zoom / Steadicam follow / Push through / Snorricam / Camera roll / Arc / Pedestal / Trucking / Locked-on / Lazy Susan）、`time`（10）、`transitions`（8）、`pov`（4）。

### 10.4-B 其他已核实来源（静态可用）

**机位角度**（BFL 官方展示区标题，静态图）：`Eye Level View`、`Worm's Eye View`

**光线**（来源 + 方向 + 质感，别只写「柔和」）：
- BFL 官方：`Cinematic lighting`、`cinematic natural light`、`Realistic skin texture and lighting`、`Hyper-realistic close up`
- MiniMax 官方：`soft studio lighting`、`golden hour backlight`、`flat diffused light`
- H3 官方：`soft lighting`、`warm indoor lighting`

**影调与色彩**：BFL 官方 `a stark palette of winter whites and greys`；H3 官方 `slightly desaturated color palette`

**构图**：MiniMax 官方 `left-aligned subject with negative space on the right for text overlay`；Runway 已核实（静态同样成立）`Leading lines`、`Frame within frame`、`Symmetrical`、`Negative space`

**焦点**：Runway 已核实 `Deep focus`、`Shallow focus`、`Soft focus`（全画面柔散——回忆/梦/浪漫）。
⚠️ `Rack focus` **不进静态图**——它是镜头内焦点转移，属动态。

**镜头 / 焦段**（Google 已核实）：`wide-angle lens`、`telephoto lens`、`shallow depth of field`（官方术语 `bokeh`）、`deep depth of field`

**风格**：MiniMax 官方 `editorial photography`、`3D render`、`flat vector illustration`、`professional product photography`、`clean minimalist tech aesthetic`、`dark gradient background`；H3 官方风格词 `Cinematic` / `live-action` / `2D-animated` / `3D CG` / `claymation` / `watercolor` / `vintage film`

**材质**（BFL 官方对该模型的描述）：`accurate hands, faces, and textures`

### 10.4-C 选词池：`Rylaispirit/cinematic-video-prompt-skill`（个人仓库，510+ 条）

⚠️ **个人仓库，不是权威源**（95 star）。但它是本次**最丰富的静态画面词库**：`references/` 下 6 个文件、两列表 `| Prompt (EN) | Giải thích (VI) |`——**左列是可进提示词的英文词，右列是越南语解释**（不是中文，得自己转译）。已读 4 份约 510 条，全量在报告 §3A-7（光线 83 / 构图 68 / 镜头·胶片质感 151 / 风格·色彩·情绪 212）。

**三条使用纪律**：
1. **只当选词池，不当模板**——它全是英文术语，撞本仓「生图正文中文」（`image_prompt_engineer.md:22`）→ 取词后**译成中文写进正文**，专有名词（`bokeh`、`anamorphic`、胶片型号如 `Kodak Portra 400`）可保留原文。
2. **`04-lens-technical-film.md` 的「快门与运动」整节剔除**：`motion blur`、`Shutter Speed 1/8000s`、`rack focus`、`time-lapse`、`Camera Shake` 都是动态语言，生图侧禁。
3. **`8K Resolution` / `4K` / `hyperdetailed` 撞「禁堆质量标签」**（`image_prompt_engineer.md:20`）**不用**；但 `hyperrealistic` / `photorealistic` 是**有内容的风格词**，可用——别一刀切。

**另外两个文件 2026-09-26 已补读**（6/6 文件全读，合计约 **700 条**——仓库自称的「700+」成立）：
- `06-materials-weather-pose.md`（175 行）——**正对本仓「外观或材质」字段**，A 节约 90 条材质：`brushed metal`、`Oxidized Copper / Patina Green Copper`、`crushed velvet / Velvet Texture (Absorbing Light)`、`Frosted Glass / Etched Glass`、`Wet and Slick Surface / Reflective puddles`、`translucent`、`cracked earth`、`worn leather / Oiled Leather`、`carbon fiber`、`Chalky / Powdery`、`Dirty/Grungy Texture` …；B 天气 / C 姿态两节同属静态可用。
- `01-camera-angles-and-movement.md`（70 行）——**角度与景别混表**：景别仅 9 条（见 10.4-D），其余 **61 条机位角度很全**，静态可用：`Hip-Level Shot`、`Floor Angle`、`Overhead Flat Lay`、`Forced Perspective`、`Low Horizon Line`、`Through the Keyhole Angle`、`Side Profile Shot`、`Three-Quarter View`、`Counter-Angle` …。
  ⚠️ **须剔除 7 条动态项**：`Stabilized Shot`、`Handheld Look`、`Shoulder Mount Look`、`Whip Pan Transition (Implied Angle)`、`The Trinity Angle`、`Whip Around Angle`、`Reverse-Dolly Angle`；Section B/C **整节是运镜，勿用**。

### 10.4-D 景别（shot size）——**官方两侧都没有，只能用第三方**

2026-09-26 三轮找齐，结论先说：**官方没有景别表**（H3 `base-en.txt` 没有；BFL cheatsheet 导航承诺 `Shot sizes` 但数据里没有词条）。四个来源、权威性三档：

| 来源 | 权威性 | 形态 |
|---|---|---|
| Google Veo 官方指南 | **官方（模型方一手）** | 12 个景别词，混在 `Camera angles` 一节，带定义 + 例句 |
| StudioBinder | 影视工业教育站（**非标准制定方**） | 完整阶梯 + 逐条定义（`EWS`→`ECU`，含 `Cowboy Shot`、`Establishing Shot`） |
| `snubroot/Veo-3-Prompting-Guide`（320★，**第三方整理**，非 Google 官方发布） | 二手（转录 Veo 官方口径） | **7 个景别** EWS / WS / MWS / MS / MCU / CU / ECU（术语 + 示例句，无定义表、无 Cowboy / Establishing）；同节还有 `Advanced Framing Techniques` 4 条、`Lens Effect Keywords` 7 词、`Color Palette Keywords` 7 词 |
| `Wayhhow/ai-video-shot-prompt-skill`→`camera-and-composition.md` | **个人仓库（16★）** | **唯一的中文景别表**（12 条，带「描述 + 适用」） |
| `Rylaispirit/…` 的 `01` | 个人仓库（95★） | 角度与景别**混在一张表**，景别仅 9 条 |

**唯一的中文景别表（免转译，直接契合本仓中文正文）**：

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
| **监控 / POV** | 第一人称 / 监控视角 | 沉浸、窥视、恐怖 |

🚨 **中文标签可用，英文缩写不要照抄**——该表的中英映射与工业惯例**不一致**：它把 `近景 MS` 定为「胸部以上」、`中景 MLS` 定为「膝盖以上」；而 StudioBinder 的标准是 `MS` = **腰以上**、`MWS/MLS` = **膝盖以上**、`MCU` = **胸以上**、`Cowboy Shot` = **大腿以上**（它没有 Cowboy Shot）。即**它的 `MS` 其实等于惯例的 `MCU`**。→ 中文照它写没问题；**正文里若要写英文景别，以 Google / StudioBinder 的映射为准**。
另注：该表最后三条（大广角 / 鱼眼 / 监控 POV）严格说不是景别而是**镜头/视角**，混表了。

✅ **该偏差已被第二个来源独立坐实**（2026-09-26）：`snubroot/Veo-3-Prompting-Guide` 的 `Shot Sizes and Framing` 里 `Medium Shot (MS)` 界定为 `framing from waist up`（**腰以上**），与 StudioBinder 一致、与那张中文表的「胸部以上」不一致。→ **英文景别一律按 Google / StudioBinder 的映射**，这条不再是单一来源的判断。

## 10.5 hex 锁色：跨帧同色的硬办法（一手，逐字）

BFL 官方 `Exact Color Control` 原文：`Specify brand colors via hex codes with precision matching — no approximation.` 官方例子逐字：`the color of the vase is a gradient of color, starting with color #02eb3c and finishing with color #edfa3c. The flowers inside the vase have the color #ff0088`。

→ **用法**：人物服装主色、关键道具色、场景主光色用 hex 写死，比「暗红色」这类形容词硬得多——**首帧/尾帧跨帧一致性**最需要这个。
→ **配套**：光线方向与色温要**同时**写进 `continuity_constraints`，否则两帧照样漂（`frame_prompt_engineer.md` 的既有字段正是为此存在）。

## 10.6 我不会用的东西

- **任何运镜词**：`push in`、`tracking`、`dolly`、`whip pan`、`orbit`… 一律不进生图提示词——本仓明令禁止，且静态图无从执行。
- **没出处就不用的词**：外部清单里的具体术语（`anamorphic widescreen lens`、`teal-and-orange color grade`）经两轮验真——**词族确实存在**（`Teal and Orange Grading`、`Anamorphic Squeeze / Bokeh / Glare`，在 `Rylaispirit/cinematic-video-prompt-skill` 的 `04`/`05` 文件里），但**清单把它们归错了仓库**（说成 `video-prompt-reverse`）→ 清单**可能张冠李戴**，它的描述不能直接采信。四个仓库逐个结论：**Rylaispirit 有货**（见 10.4-C）；`ngaitom928`（1★）**读了，没有术语表**；`Wayhhow`（16★）有 `keyword-library.md` / `camera-and-composition.md` 但**正文仍未读**；`LunarXuan`（48★）文件树里**没有术语表**——「电影感词汇库」这个说法**不成立**（它是反推流程仓库）。`cinematique.ai` SSL 握手失败、`invideo.io` 路径 404，两条**仍未核实**。
  → 「700+ 术语」**成立**（6/6 文件全读，约 700 条）；但**权威性低**——都是个人仓库。
- 🚨 **一个方法论陷阱（`Wayhhow/ai-video-shot-prompt-skill` 的 `keyword-library.md`）**：它的**词可以用，方法不能学**。该文件「**强制使用**去 AI 味质量词」（`超写实`、`极致逼真`），并整节写「`杜绝动作僵硬`/`杜绝游戏CG感`」这类**负向祈使句**——同时撞本仓两条纪律（禁堆质量标签 `image_prompt_engineer.md:20`），而且与 **Google 官方口径正好相反**（官方原文：`Not recommended: using instructive language or words such as "no" or "don't"`）。→ **取词不取法**。
- **`professional product photography` 这类质量短语**：⚠️ 注意 BFL 官方示例里就有（说明**模型认**），但**本仓自设纪律不许堆**（`image_prompt_engineer.md:20`）。这是**纪律**不是模型限制——别因为官方这么写就放宽，也别误以为模型不认。
- **「Z-Image 官方推荐写法」这类说法**：不存在这样的官方内容（见 10.2）。

## 10.7 生图侧自查

1. 这一版是给**哪个模型**写的？构图词按它的画布走（**zimage 1:2 竖幅 / flux2 1:1 正方**），两套不能互换。
2. **景别**写了吗？中文按 10.4-D 那张表；**若要写英文缩写，以 Google / StudioBinder 的映射为准**（别照抄那张中文表的中英对应）。
3. 运镜、剪辑、时间码、H3 字段**清干净**了吗？
4. 光线写了**来源 + 方向 + 质感**吗？
5. 关键色要不要用 **hex** 锁死？光线方向/色温写进 `continuity_constraints` 了吗？
6. 有没有堆无意义质量标签？
7. 两模型版本**分别重写**了吗（不是同一句复制）？
8. 首/尾帧共享 `scene_anchor` 了吗？有没有偷偷换地点？
