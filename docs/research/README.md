# 调研笔记索引（`docs/research/`）

新笔记按 `YYYY-MM-DD-主题.md` 落盘（见 `AGENTS.md`「产物落点」），**并在这里补一行**——
没有索引时，找一份旧笔记只能靠 `ls -t` 猜哪份最新，或者把十份全开一遍。

| 笔记 | 主题 | 什么时候该读 |
|---|---|---|
| [2026-09-24-minimax-h3-quantization-options](2026-09-24-minimax-h3-quantization-options.md) | MiniMax H3 量化方案（AMD ROCm / RX 9070 16GB） | 选量化档位、判断显存够不够时 |
| [2026-09-26-film-language-prompt-sources](2026-09-26-film-language-prompt-sources.md) | 影视提示词权威库与镜头语言模板 | 改 `docs/film-language-playbook.md` 或机读真源前 |
| [2026-09-26-github-prompt-repos](2026-09-26-github-prompt-repos.md) | GitHub 高星 AI 视频/生图提示词仓库普查 | 找外部提示词写法参考时 |
| [2026-09-26-image-prompt-language-sources](2026-09-26-image-prompt-language-sources.md) | 生图（静态画面）提示词语言源 | 改生图侧提示词或画布约定前 |
| [2026-10-24-route-product-separation-exploration](2026-10-24-route-product-separation-exploration.md) | 首尾帧与多参两条路线的产物集分离：代码勘察 | 动产物集、台账或路线分离（#77~#83）时 |
| [2026-10-27-首尾帧链拼接实现勘察](2026-10-27-首尾帧链拼接实现勘察.md) | 首尾帧链拼接的实现勘察 | 改拼接判据或首尾帧链时 |
| [2026-10-29-director-现有分段与提示词产物链](2026-10-29-director-现有分段与提示词产物链.md) | 接入导演台前：既有 base / ref 两条路线的分段与提示词产物链 | 动分段产物链之前 |
| [2026-10-29-director-会话与管线接线](2026-10-29-director-会话与管线接线.md) | 导演台路线的会话 / 向导 / 管线 / 续跑 / 账本接线 | 动导演台接线之前 |
| [2026-10-29-director-插件原生格式](2026-10-29-director-插件原生格式.md) | 导演台插件的原生格式与运行时契约（#104~#110 的依据） | 与插件交换包、帧网格、提示词改写相关时 |
| [2026-10-30-matt-skills工作流地图](2026-10-30-matt-skills工作流地图.md) | 本机 41 个 skill 的分工、上下游比对与实测调用画像 | 想不起来某个 skill 干什么、或要改工作流时 |

## 约定

- **一份笔记一个文件**，不要把新结论追加进旧笔记——日期是判断「这条信息还对不对」的唯一线索。
- 笔记里引用的**行号会漂**：`prompts/*.md` 被引用的行号在插入内容后会集体错位且不报错，重读时按符号/原文重新定位。
- 结论若已被后续 issue 推翻，**就地加失效横幅**，不重写正文（同 ADR 状态行的口径）。
