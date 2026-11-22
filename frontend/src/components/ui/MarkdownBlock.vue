<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'

// 产物正文的只读渲染块（剧本、分镜表、段词、注入卡……）。
//
// **为什么需要它**：这些正文是 markdown（实测 202 份产物里标题 200、粗体 569、
// 列表 586、表格 144、行内代码 115），此前一律塞进 `<pre>` 原样显示，用户看到的是
// `## 场 1`、`**场景**` 这样的记号而不是排版。
//
// **为什么 html: false 不是可选项**：产物正文里 `<Picture 1>`、`<Subject 5>`、
// `<d>西藏</d>` 一共出现 770 次，它们是模型写的**占位符**，不是 HTML。一旦允许
// 原始 HTML，浏览器会把它们当标签解析掉，用户就看不到这些编号了——那正是接线时
// 要照抄的东西。禁用原始 HTML 后它们按字面显示。
//
// 同时也是安全边界：禁用原始 HTML 消除了注入面；链接再补 target/rel，
// 并且只放行 http/https（`javascript:` / `data:` 由 markdown-it 的
// validateLink 拦掉），这里不自己写正则放行。
const props = defineProps<{
  /** 原始正文。复制/下载仍用这一份，不用渲染结果。 */
  text: string
}>()

const md = new MarkdownIt({
  html: false, // 见上：产物里的 <Picture N> 是占位符，必须原样显示
  linkify: false, // 正文里的 URL 不是可点的对象，别让它们自动变成链接
  breaks: true, // 提示词按行组织，单换行要真的换行，否则整段会挤成一行
  typographer: false, // 不做智能引号替换：正文字面要跟产物文件一字不差
})

// 外链一律新标签打开并切断 opener；站内相对链接保持默认。
const defaultLinkOpen =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const href = tokens[idx]?.attrGet('href') ?? ''
  if (/^https?:/i.test(href)) {
    tokens[idx]?.attrSet('target', '_blank')
    tokens[idx]?.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, idx, options, env, self)
}

const html = computed(() => md.render(props.text ?? ''))
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- html:false 已禁止原始 HTML，
       输出只含渲染器自己生成的标签；正文里的尖括号会按字面转义。 -->
  <div class="markdown-block" v-html="html"></div>
</template>

<style scoped>
/* 块的外形（1px 线、板面底、最大高度）由调用方按所在分区决定，这里只管正文排版。
   调用方在**同一个**类名上管外框——所以本组件不设 border/background/padding。 */
.markdown-block {
  font-family: var(--font-sans);
  font-size: var(--text-body);
  line-height: 1.75;
  color: var(--color-ink);
  word-break: break-word;
  overflow-x: auto;
}
.markdown-block > :first-child {
  margin-top: 0;
}
.markdown-block > :last-child {
  margin-bottom: 0;
}

/* 标题：产物里 `##`（场）是分区标题、`###` 是次级。字号不铺排——沿用
   线段世界的规矩，靠字重与墨色分层，只比正文多半档。 */
.markdown-block :deep(h1),
.markdown-block :deep(h2) {
  margin: var(--space-4) 0 var(--space-2);
  font-size: var(--text-lead);
  font-weight: 500;
  line-height: 1.45;
  color: var(--color-ink);
}
.markdown-block :deep(h3),
.markdown-block :deep(h4),
.markdown-block :deep(h5),
.markdown-block :deep(h6) {
  margin: var(--space-3) 0 var(--space-1);
  font-size: var(--text-body);
  font-weight: 500;
  line-height: 1.5;
  color: var(--color-ink);
}

.markdown-block :deep(p) {
  margin: 0 0 var(--space-2);
}

/* 粗体是产物里最常用的强调（`**场景**`）：提到骨白 + 500，不用第二档颜色。 */
.markdown-block :deep(strong) {
  font-weight: 500;
  color: var(--color-ink);
}
.markdown-block :deep(em) {
  font-style: italic;
  color: var(--color-ink-soft);
}

/* 列表：缩进对齐正文，条目间留一点空隙（提示词条目是长句）。 */
.markdown-block :deep(ul),
.markdown-block :deep(ol) {
  margin: 0 0 var(--space-2);
  padding-left: var(--space-4);
}
.markdown-block :deep(li) {
  margin: var(--space-1) 0;
}
.markdown-block :deep(li::marker) {
  color: var(--color-ink-soft);
}

/* 分割线是产物里的分场分隔（74 处）：这就是本世界的 1px 发光线。 */
.markdown-block :deep(hr) {
  margin: var(--space-4) 0;
  border: 0;
  border-top: 1px solid var(--color-rule);
}

/* 行内代码：`<Picture 1>`、参数、路径——等宽 + 板面底，不加圆角盒。 */
.markdown-block :deep(code) {
  padding: 0.1em 0.35em;
  border-radius: var(--radius-sm);
  background: var(--color-plate-raised);
  font-family: var(--font-data);
  font-size: 0.95em;
}
.markdown-block :deep(pre) {
  margin: 0 0 var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-rule);
  background: var(--color-plate);
  overflow-x: auto;
}
.markdown-block :deep(pre code) {
  padding: 0;
  background: transparent;
  font-size: var(--text-micro);
}

/* 引用块：竖线在左侧，是线段世界允许的「线」，不是色条卡片。 */
.markdown-block :deep(blockquote) {
  margin: 0 0 var(--space-2);
  padding-left: var(--space-3);
  border-left: 1px solid var(--color-rule-strong);
  color: var(--color-ink-soft);
}

/* 表格（注入卡与多参分镜表各一份）：1px 线分隔，无圆角、无斑马底。 */
.markdown-block :deep(table) {
  margin: 0 0 var(--space-3);
  border-collapse: collapse;
  font-size: var(--text-micro);
}
.markdown-block :deep(th),
.markdown-block :deep(td) {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-rule);
  text-align: left;
  vertical-align: top;
}
.markdown-block :deep(th) {
  color: var(--color-ink);
  font-weight: 500;
}

/* 链接：只给可点的 http(s) 链；下划线而非换色，钠黄留给「最要紧的那一条」。 */
.markdown-block :deep(a) {
  color: var(--color-ink);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.markdown-block :deep(a:hover) {
  color: var(--color-signal);
}
</style>
