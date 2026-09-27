# 电影语言词表 · 生图半

**生图侧（静态画面：Z-Image / Flux.2）**的词表与写法真源：写关键帧画面时**从这里选词**，别自造词。
它由本仓 `docs/film-language-playbook.md`（第十节）按本仓硬约束过滤而来，把那份的结论收成**唯一真源**。

**与视频半是两份真源，不是一份切片**：同级的 `film-language-video.md` 管 H3 **视频**正文
（运镜、每镜六要素、七格骨架、H3 写作纪律）；本份管**静态画面**。视频侧的运镜词表
**一个字都不进这里**——静态图无从执行机位运动，写进去就是错。要视频侧的词去那一份取。

**与官方参考文献的区别**：同级的 `references/base-en.txt` / `ref-en.txt` 是 **H3 官方格式规范**，
与静态生图无关；本份是**选词与写法**。输出 JSON 长什么样由角色提示词定义，本份不定义格式。

**本份里没有要逐字符照抄的官方原文**，故无须指针：静态生图侧没有任何一处是「必须逐字符一致」的
（Flux.2 / BFL 的 JSON 是**建议结构**，本仓按自己的口径落成字段表，见「结构化字段」一节）。

---

## 四条硬约束

本仓纪律。与下面任何词表冲突时以本节为准，**违反即不合格**：

1. **不写运镜、剪辑、时间码、H3 三段式**：静态画面没有时间轴。`push in` / `tracking` / `dolly` /
   `whip pan` / `orbit` / `zoom` 这类机位运动一律不写；`integrated_multimodal_description`、
   `overall_soundscape`、`non_diegetic_music` 三段式字段不写；画面里出现的时间码
   （`At 00:02.000`）同样不写。**机位角度可以写**（俯拍、仰拍、平视）——那是**站在哪儿拍**，
   属静态构图；机位**运动**才是禁的。
2. **不堆无意义的质量标签**：`8K` / `4K` / `hyperdetailed` / `masterpiece` / `best quality`
   这类只喊好、不给信息的词一律不用。**有内容的风格词可用**（`photorealistic`、`hyperrealistic`），
   别一刀切——判据是「它有没有说清画面是什么样」。
3. **正文中文**：正向提示词用**中文自然语言**写完整画面；模型不认识的专有名词可保留原文
   （`bokeh`、`anamorphic`、胶片型号如 `Kodak Portra 400`）。
4. **默认不输出负向提示词**：除非输入明确说明目标工作流有独立的负向输入。
   （这条是**纪律**不是「字段没人要」——非空负向词确实会被渲染出去消费。）

## 画布决定构图：两套

**同一条构图指令在两种画布上语义不同**——这是「两个模型必须分别重写」的**物理依据**，
不只是纪律要求：

| 画幅 | 构图含义 |
|---|---|
| **竖幅** | 纵向空间、上下留白；主体偏上还是偏下是**有差别**的取舍 |
| **正方** | 居中与对称更自然；横向留白受限，别指望「左侧留白三分之一」 |

**哪个模型是哪种画幅、尺寸是多少，一律以请求里的【画布】块为准**——本份不复述，
两处写就会在两处漂（画幅是请求里的数据，不是这里的知识）。

两套构图**不能互换**，也不能两个模型抄同一句：

- **竖幅**：纵向分层（上 / 中 / 下三段）、纵向引导（地面反光、楼梯、门框、走廊）、主体占满一列；
  「左右留白」在这里是次要维度。
- **正方**：中心锁定、严格对称、对角或三分法；四周留白要**同时**顾及两边，横向尤其紧。

**默认不写画幅比例**（`9:16`、`Cinemascope` 之类）：工作流的画布已经定了画幅，正文再声明一遍
是多余的，也可能与画布打架。要写就写**构图关系**（谁在画面哪个位置、留白在哪边），不写比例数字。

## 结构化字段

Flux.2 官方推荐**结构化提示词**（原话全文：`Use JSON-structured prompts for precise control over
generation — ideal for production workflows and automation.`），官方示例给了六个字段：`subject`、`background`、`lighting`、`style`、
`camera_angle`、`composition`。本仓的口径是**英文键名 + 中文值**，其中两个已经落进输出 JSON：

| 字段 | 写什么 |
|---|---|
| `composition` | 画面怎么摆：景别 + 主体位置 + 留白 / 引导线 / 对称——**按本模型的画布写** |
| `lighting` | 光怎么打：**方向 + 强度 + 质感**三维（见「词表：光线」） |

⚠️ 这两个字段与正向提示词里的自然语言**并存**，不是二选一：正文给人读，字段给机器改——
复跑同一张图时「只换光、不动构图」靠的就是它们。字段位置（属于哪个模型的哪一层）由角色提示词定义。

## 词表：景别

| 中文 | 画面范围 | 英文 |
|---|---|---|
| 大远景 | 几乎看不到人物 | `extreme wide shot` |
| 远景 | 人物小、强调环境 | `wide shot` / `long shot` |
| 全景 | 全身（head to toe） | `full shot` |
| 中景 | 膝盖以上 | `medium wide shot` |
| 近景 | 腰以上 | `medium shot` |
| 中特写 | 胸以上 / 头肩 | `medium close-up` |
| 特写 | 面部填满画面 | `close-up` |
| 大特写 | 只留眼睛、嘴唇、极小局部 | `extreme close-up` |
| 微距 | 极近距离的小物体 | `macro` |

**官方两侧都没有景别表**（H3 的 `base-en.txt` 没有；BFL cheatsheet 导航承诺 `Shot sizes`
但数据里没有词条），上表按行业惯例（Google / StudioBinder）整理。

⚠️ **第三方中文表的英文缩写不要照抄**：流传最广的那张中文表把 `MS` 标成「胸部以上」
（＝惯例的 `MCU`）、把 `MLS` 标成「膝盖以上」（＝惯例的 `medium wide`）。中文标签怎么叫无所谓，
英文一律以上表的映射为准。「大广角 / 鱼眼 / 监控 POV」严格说不是景别而是镜头或视角，别混进这一栏。

## 词表：视角与机位

**站在哪儿拍**（静态合法，与「禁运镜」不冲突）：

`eye level`（平视）、`low angle`（仰拍：力量感、压迫感）、`high angle`（俯拍：脆弱、孤立、交代地理）、
`over-the-shoulder / OTS`（过肩）、`POV`（主体视角）、`top-down / aerial`（正上方俯视 / 航拍）、
`bird's eye view`（鸟瞰）、`worm's eye view`（贴地仰视：压迫、幽闭）、`dutch angle`（地平线倾斜：失衡——**少用**）、
`profile shot`（严格侧面）、`three-quarter view`（四分之三侧）、`ground level`（贴地横扫）、
`overhead flat lay`（正上方平铺，静物常用）。

## 词表：构图

**构图**：`leading lines`（引导线，用线条把视线带过去）、`frame within frame`（框中框，用场景里的开口框住主体）、
`symmetry`（对称：镜像，可精确也可用来造张力）、`center framing`（主体锁定画面正中）、
`rule of thirds`（三分法：偏心求平衡）、`negative space`（负空间：主体小、留白大——孤立、尺度感）、
`foreground occlusion`（前景虚化遮挡，增纵深）、`silhouette`（逆光剪影）、
`reflection framing`（用反射面构图）。

**焦点**：`deep focus`（深焦，远近都实）、`shallow depth of field`（浅焦、`bokeh`）、
`soft focus`（全画面柔散——回忆 / 梦 / 浪漫）、`split diopter`（近远同时清晰）、
`tilt shift`（微缩景观感的选择性对焦）。

⚠️ **`rack focus`（镜头内焦点转移）与 `focus breathing`（呼吸对焦）不进静态图**——它们是
镜头内的**动态**，与运镜同族。

## 词表：光线

写光线要落到**三个维度**，别只写「光线柔和」：

- **方向**：光从哪来（逆光 / 侧光 / 顶光 / 场景内实景光）
- **强度**：硬朗还是柔散、光比大不大
- **质感**：暖还是冷、干净还是浑浊（雾、尘、水汽让光成柱）

| 中文 | 英文原文 |
|---|---|
| 柔光 / 柔和影棚光 | `soft lighting` / `soft studio lighting` |
| 硬光、硬边阴影、高对比 | `hard light` |
| 强明暗对比（明暗法） | `chiaroscuro` |
| 逆光勾边 | `rim light` |
| 黄金时刻逆光（暖低日） | `golden hour backlight` |
| 平匀漫射光 | `flat diffused light` |
| 暖调室内光 | `warm indoor lighting` |
| 雾中可见光柱 | `volumetric light` |
| 厚大气（显光柱） | `haze` |
| 场景内实景光（霓虹、招牌、台灯） | `neon practicals` |
| 单束光把主体从暗里拎出来 | `spotlight` |
| 略去饱和的调色 | `slightly desaturated color palette` |

**影调与色彩**可另写一句（如 `a stark palette of winter whites and greys`）——它属光线质感，
不属「质量标签」。

## 词表：镜头与焦段

`wide angle (24mm)`（空间夸张、边缘畸变）、`telephoto compression`（压平纵深、画面叠层）、
`fisheye`（圆形畸变）、`anamorphic flares`（横向光条 + 椭圆散景）、`macro lens`（极微距）、
`probe lens`（探针镜头，穿窄缝）、`halation`（高光周围光晕）、`vignette`（暗角框亮心）、
`parallax`（⚠️ 纵深各层不同速滑动，**偏动态**，静态图慎用）。

## 词表：风格与材质

**文体**：`Cinematic`、`live-action`、`2D-animated`、`3D CG`、`claymation`、`watercolor`、
`vintage film`；摄影向另有 `editorial photography`、`professional product photography`、
`3D render`、`flat vector illustration`、`clean minimalist tech aesthetic`。
风格基调由 brief 给定，落笔时别自创。

**材质**（写外观时用，比「有点旧」具体得多）：`brushed metal`（拉丝金属）、
`oxidized copper / patina green copper`（氧化铜绿）、`frosted glass / etched glass`（磨砂玻璃）、
`translucent`（半透）、`crushed velvet`（压皱丝绒，吸光）、`worn leather / oiled leather`（做旧 / 上油皮革）、
`wet and slick surface / reflective puddles`（湿滑表面 / 反光水洼）、`cracked earth`（龟裂土地）、
`chalky / powdery`（粉质）、`carbon fiber`（碳纤维）、`dirty / grungy texture`（脏旧质感）。

## hex 锁色

**跨帧同色的硬办法**：关键色用 hex 写死，比「暗红色」这类形容词硬得多。BFL 官方原话是
`Specify brand colors via hex codes with precision matching — no approximation.`

用法：人物服装主色、关键道具色、场景主光色写成 `#ff0088` 这样的值；渐变可写成
「从 `#02eb3c` 渐变到 `#edfa3c`」。**首尾帧跨帧一致性最需要这个**。

配套：光线方向与色温要**同时**写进 `continuity_constraints`，否则两帧照样漂。

## 落笔前自查

1. 这一版是给**哪个模型**写的？构图按【画布】块里它那一行的画幅走，两套不互换。
2. **景别**写了吗？中文按本份的景别表；要写英文缩写时别照抄第三方中文表的中英对应。
3. 运镜、剪辑、时间码、H3 字段**清干净**了吗？
4. 光线写了**方向 + 强度 + 质感**吗？`lighting` 字段填了吗？
5. 构图写清了主体位置与留白吗？`composition` 字段填了吗？
6. 关键色要不要用 **hex** 锁死？光线方向 / 色温进 `continuity_constraints` 了吗？
7. 有没有堆无意义质量标签？
8. 两个模型版本**分别重写**了吗（不是同一句复制）？
9. 首尾帧共享 `scene_anchor` 了吗？有没有偷偷换地点？
