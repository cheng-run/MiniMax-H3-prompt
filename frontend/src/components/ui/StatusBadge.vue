<script setup lang="ts">
// 状态徽标：始终带文字，不只靠颜色传达状态（可访问性基线）。
withDefaults(defineProps<{ label: string; tone?: 'ok' | 'error' | 'neutral' | 'attention' }>(), {
  tone: 'neutral',
})
</script>

<template>
  <!-- 线段只是视觉：读屏只读文字标签，形状本身不承载额外语义。 -->
  <span class="badge" :class="`badge--${tone}`">
    <span class="badge__line" aria-hidden="true"></span>
    <span class="badge__label">{{ label }}</span>
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-micro);
  letter-spacing: var(--track-mid);
  /* 列表行是 flex 容器，徽标会被压缩；不换行才能保住「已完成」这类三字标签
     （2026-11-21 critique：420 宽下折成「已完／成」两行）。 */
  white-space: nowrap;
  flex: none;
}

/* 线段语言：状态是线，不是色块。一根 14px 的小线段放在标签左侧。
   未开始/已完成/失败都用 2px 单线，「加倍」的双线只留给等待人工。 */
.badge__line {
  display: block;
  flex: none;
  width: 14px;
  height: 2px;
  background: var(--color-state-pending);
}

.badge--ok .badge__line {
  background: var(--color-state-done);
}
.badge--ok .badge__label {
  color: var(--color-ink-soft);
}

/* 失败/中断/取消：划过——线断成划痕，与导轨上的斜划同一语义。 */
.badge--error .badge__line {
  background: repeating-linear-gradient(to right, var(--color-error) 0 3px, transparent 3px 5px);
}
.badge--error .badge__label {
  color: var(--color-error);
}

.badge--neutral .badge__label {
  /* 徽标文字要读得出来：ink-faint 对画布只有 3.94:1。极暗的表达交给左侧线段。 */
  color: var(--color-ink-soft);
}

/* 等待人工：两根 2px 平行线、间隔 2px，再加钠黄——全站唯一亮色只给
   「现在最要紧的那一条」。 */
.badge--attention .badge__line {
  height: 6px;
  background: repeating-linear-gradient(to bottom, var(--color-signal) 0 2px, transparent 2px 4px);
}
.badge--attention .badge__label {
  color: var(--color-signal);
  font-weight: 500;
}
</style>
