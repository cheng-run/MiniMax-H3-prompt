# 电影语言词表 · 视频半

**视频侧（MiniMax H3）**的电影语言真源：写分镜与画面时**从这里选词**，别自造词。
它由本仓 `docs/film-language-playbook.md`（第一~九节）按本仓硬约束过滤而来，把那份的结论收成**唯一真源**
（各角色提示词不再各抄一份）。

**与官方参考文献的区别**：与本份同级的 `references/base-en.txt` / `ref-en.txt` 是官方**格式规范**
（字段、结构、句法，逐字符照抄用）；本份是**选词与写法**（哪个词能选、句子怎么落），
不定义格式、不替代官方规范。格式问题一律以官方规范为唯一依据。

**要逐字符照抄的官方原文，本份不复述、只留指针**：镜头块收尾的 edge-stability 句取
`h3_validator.EDGE_STABILITY_SENTENCE`，分段首行的锚定行取
`h3_validator.ALIGN_TEMPLATES[segment_prompts.SEGMENT_ANCHOR_VARIANT]`。这两处已有唯一来源、且有测试守着；
在这里再抄一份就是又一份硬拷贝（本仓吃过「同一句官方原文 3 份硬拷贝」的亏）。

**生图侧（静态画面）是另一套语言**，不在本份内：运镜、剪辑、时间码、H3 三段式一律不进静态图提示词。

---

## 运镜怎么写

完整运镜 = **运动类型 + 幅度 + 速度**三要素，写成镜头内的自然句。

**官方白名单（只有这些）**：

| 维度 | 可用表达 |
|---|---|
| 运动类型 | `Zoom In / Zoom Out`（焦距推拉，机位不动）、`Push In / Pull Out`（机位前后移动）、`Pan Left / Pan Right`（原地横摇）、`Truck Left / Truck Right`（横向平移）、`Tilt Up / Tilt Down`（原地纵摇）、`Pedestal Up / Pedestal Down`（整机升降）、`Arc Shot`（绕主体弧移）、`Tracking Shot`（跟拍移动主体）、`Static Shot`（机位与镜头全静）、`Shake Slightly / Shake Strongly`（轻微 / 强烈抖动）、`POV`（主体视角）、`Roll Clockwise / Roll Counterclockwise`（绕光轴滚转） |
| 幅度 | `with small amplitude`（小幅）/ `with large amplitude`（大幅）；`medium amplitude` 通常省略 |
| 速度 | `at slow speed`（慢）/ `at fast speed`（快）；`normal speed` 通常省略 |

**写法铁律**：写成镜头里的自然英文动作，**不许在句尾堆标签**（官方 `base-en.txt` 4.3 原话：
rather than stacked as separate labels at the end of a sentence）。官方例句：

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the open doorway.
The camera holds a static shot as the runner exits the frame.
```

**三条容易漏的**：

- 速度修饰别省：每个运动词都该带 slow / rapid / steady，或写明时长（如 `a slow 5-second pan right`）。
- **机位与主体的关系用动词写明**——这是最贵的遗漏：`Camera follows the cyclist.`、
  `Camera trucks left, matching the runner's speed.`、`Camera leads the dancer, pulling back as she advances.`
- 多阶段运镜写「每阶段画面里有什么」，不要堆动词：
  ✅ `The camera arcs around the seated figure, then rises above the table to reveal the empty chairs surrounding her.`
  ❌ `Orbit the subject and crane up and push in.`

**不在白名单的运镜名一律不用**：`crane`、`handheld`、`orbit`、`crash zoom`、`dolly zoom`、`whip pan`、
`Steadicam`、`Gimbal`。只改距离或轻微角度时**优先用运镜、别切镜**（官方 `base-en.txt` 4.2）。

---

## 每镜六要素

官方 `ref-en.txt` 要求每个镜头块必须确立这六项——少一项，镜头就退化成剧情摘要：

1. **构图 composition**
2. **主体外观与位置 subject appearance and position**
3. **环境与光线 environment and lighting**
4. **动作与状态变化 actions and state changes**
5. **运镜 camera movement**
6. **当下声音 current sound**

官方同时**禁止**把描述退化成**剧情摘要**或**参考关系罗列**。

---

## 七格骨架

写一镜时的排序：

```text
[景别 shot size] + [机位角度 angle] + [运镜 + 速度 movement + speed] + [主体与动作 subject & action]
+ [镜头 / 质感 lens/look] + [光线与调性 lighting/mood] + [这镜揭示了什么 what the shot reveals]
```

第七格「这镜揭示了什么」最像导演、也最常被漏掉。示例：

```text
Medium close-up, low angle, slow dolly push-in over 4 seconds, on a detective lighting a
cigarette in the rain, 35mm lens with shallow depth of field, neon reflections.
The push-in reveals the tension in his face.
```

---

## 词表：景别

| 中文 | 画面范围 | 英文 |
|---|---|---|
| 远景 | 人物小、强调环境 | `wide shot` / `long shot` |
| 全景 | 全身（head to toe） | `full shot` |
| 中景 | 腰以上 | `medium shot` |
| 近景 | 胸口以上 | `medium close-up` |
| 特写 | 面部填满画面 | `close-up` |
| 大特写 | 只留眼睛、嘴唇等极小局部 | `extreme close-up` |

**官方 H3 例文里只出现过这 6 个**：`a medium-wide shot`、`a close-up`、`an extreme close-up`、
`a medium shot`、`a close shot`、`a wide shot`——**优先用它们**。更细的（`MCU`、`cowboy shot`）
属扩张，可以试，但要清楚它不在官方例文里。

官方**没有景别表**，上面这套英文映射按行业惯例（Google / StudioBinder）来：
`medium shot` = 腰以上、`medium close-up` = 胸以上。别照抄某些第三方中文表的中英对应——它们把这两档对调了。

---

## 词表：机位角度

`eye-level`（平视）、`low angle`（仰拍：力量感、压迫感）、`high angle`（俯拍：脆弱、孤立、交代地理）、
`over the shoulder / OTS`（过肩）、`POV`（主体视角）、`top-down / aerial`（正上方俯视 / 航拍）、
`bird's eye view`（鸟瞰）、`worm's eye view`（贴地仰视：压迫、幽闭）、
`dutch angle`（倾斜：失衡——**少用**）。

---

## 词表：构图与焦点

**构图**：`leading lines`（引导线）、`frame within frame`（框中框）、`symmetrical`（对称）、
`negative space`（负空间：主体小、留白大——孤立、尺度感）。

**焦点**：`deep focus`（深焦，远近都实）、`shallow focus`（浅焦）、`soft focus`（全画面柔散——回忆 / 梦 / 浪漫）、
`rack focus`（镜头内焦点在景间转移，不动机位就转移注意力）。

---

## 词表：光线

写光线要落到**三个维度**，别只写「光线柔和」：

- **方向**：光从哪来（逆光 / 侧光 / 顶光 / 场景内实景光）
- **强度**：硬朗还是柔散、光比大不大
- **质感**：暖还是冷、干净还是浑浊（雾、尘、水汽让光成柱）

可用词（全部逐字，来源：MiniMax 官方同族 `asset-prompt-guide.md` 与 H3 官方例文）：

| 中文 | 英文原文 |
|---|---|
| 柔和的影棚光 | `soft studio lighting` |
| 黄金时刻逆光 | `golden hour backlight` |
| 平匀漫射光 | `flat diffused light` |
| 柔光 | `soft lighting` |
| 暖调室内光 | `warm indoor lighting` |
| 略去饱和的调色 | `slightly desaturated color palette` |

视频侧的光线词表就这些，**别在这份之外自造光线词**。外部那些几十条的静态生图词库（BFL、Google 的清单）
属**生图侧**，不在本份内——要引也得先按本仓硬约束过滤，不许整表搬。

---

## 词表：风格

官方只从这 7 个里选：`Cinematic`、`live-action`、`2D-animated`、`3D CG`、`claymation`、
`watercolor`、`vintage film`。风格基调由 brief 给定，落笔时别自创。

---

## 写作纪律

最容易漏的两条。它们都**没有校验闸门**（validator 只管切点递增与越界，不管毫秒位、也不管堆标签）；
提示词层此前只有两个选词角色各说了半句，**落笔侧一处都没提**——没有一处把它当成落笔时的纪律：

1. **运镜写成一句自然句**，不许在句尾堆标签：
   ✅ `The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.`
   ❌ `Push In, small amplitude, slow speed.`
2. **切点必须是整秒**：时间戳毫秒位写 `.000`（如 `At 00:04.000`），且严格递增。
   官方给的理由是 ComfyUI 的 H3 时长档只有 **4-10 秒的整数档**——分镜阶段按情节节拍给镜头时长
   （本仓允许长短不一），换算到执行段时由分段拆分吸收进整数档。
