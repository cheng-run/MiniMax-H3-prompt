# 共享契约：导轨语言（给并行改造的代理）

本文件是**唯一口径**。列表页与详情页由不同代理改，两边必须照这里的定义写，不许各造一套。

## 线的形态 → 状态（七态）

| 状态 | 线形 | 颜色 |
|---|---|---|
| `running` 运行中 | 实线，全高 18px，2px 粗 | `--color-state-live`（最亮） |
| `awaiting_human` 等待人工 | **双线**（两根 2px 平行，间隔 2px），全高 | `--color-signal`（钠黄，全站唯一亮色） |
| `completed` 已完成 | 实线，**半高** 9px，居中 | `--color-state-done` |
| `interrupted` 已中断 | 实线，全高，但**叠加斜划**（见下） | `--color-state-done` |
| `failed` 失败 | 实线，全高，叠加斜划 | `--color-error` |
| `cancelled` 已取消 | 实线，全高，叠加斜划 | `--color-state-dead` |
| `created` 已创建 | 实线，全高，**极暗** | `--color-state-pending` |

「斜划」实现：`background: linear-gradient(...)` 或伪元素画一根 1.5px 的斜线覆盖在线段上，颜色同该行前景色。**不要**用 `text-decoration` 之外的方式动标题文字——标题被划掉只在列表页的会话标题上用删除线（见下）。

## 列表页与详情页的关系

- **列表页**：左侧一条纵向导轨（宽 56px），每个会话一根刻度线，线与线纵向间距 44px。点线或点行都进入详情。右侧是行内容。
- **详情页**：**不重复导轨**。详情页顶部保留原来的步骤带（`ProgressBand`），但步骤带也改成同一种线型语言。

## 行内容的排版

一行三列，`grid-template-columns: 1fr auto auto`，`gap: var(--space-4)`：

1. 标题（`<RouterLink>`，`--text-body`，`--color-ink`，悬停加下划线）
2. 状态（用 `StatusBadge`，自带线段）
3. 元信息（变体 · 修订，`--text-micro`，`--font-data`，`--color-ink-soft`，`white-space: nowrap`）

行高 44px，行与行之间靠 1px `--color-rule` 分隔线（不用卡片、不用卡片间隙）。悬停整行底色 `--color-plate`。

## 「等待人工」那一行

- 整行底色 `--color-plate-raised`
- 行左缘外（导轨内）的刻度线是双线 + 钠黄
- 标题颜色也是 `--color-signal`
- 行尾追加一枚极小的大写铭牌 `NEXT`（`--text-micro`，`--track-wide`），文字 `--color-signal`

## 动效（全站唯一签名动效）

导轨上那根「当前/悬停」的指示线在被指向时**落位并过冲一次**：
`transform: translateY()` + `transition: transform var(--duration-base) var(--ease-spring)`。
只在导轨指示线上用，别处一律 `--ease-out`、`--duration-fast`。`prefers-reduced-motion` 由全局压掉。

## 禁止

- 不许用卡片 + 圆角 + 阴影重做列表（那是被替换掉的旧世界）
- 不许新增第二种强调色
- 不许把状态做成纯色块或纯图标——**永远同时有文字标签**
- 不许给中文标题加 `letter-spacing`
- 不许写裸色值、裸 px 间距（用 `var(--color-*)` / `var(--space-*)`）
