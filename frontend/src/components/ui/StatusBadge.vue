<script setup lang="ts">
// 状态徽标：始终带文字，不只靠颜色传达状态（可访问性基线）。
withDefaults(defineProps<{ label: string; tone?: 'ok' | 'error' | 'neutral' | 'attention' }>(), {
  tone: 'neutral',
})
</script>

<template>
  <span class="badge" :class="`badge--${tone}`">{{ label }}</span>
</template>

<style scoped>
.badge {
  display: inline-block;
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-label);
  border: 1px solid var(--color-border);
  /* 列表行是 flex 容器，徽标会被压缩；不换行才能保住「已完成」这类三字标签
     （2026-11-21 critique：420 宽下折成「已完／成」两行）。 */
  white-space: nowrap;
  flex: none;
}
.badge--ok {
  background: var(--color-ok-bg);
  color: var(--color-ok-text);
  border-color: var(--color-ok-text);
}
.badge--error {
  background: var(--color-error-bg);
  color: var(--color-error-text);
  border-color: var(--color-error-text);
}
/* 「要你做事」这一档：用强调色的浅底 + 同色字，不引入新颜色。等待人工是本产品
   的常态，也是唯一必须由人动手的状态，列表里必须一眼跳出来。 */
.badge--attention {
  background: var(--color-attention-bg);
  color: var(--color-attention-text);
  border-color: var(--color-attention-text);
}
</style>
