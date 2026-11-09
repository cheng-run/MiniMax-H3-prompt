<script setup lang="ts">
// 状态徽标：始终带文字，不只靠颜色传达状态（可访问性基线）。
withDefaults(defineProps<{ label: string; tone?: 'ok' | 'error' | 'neutral' }>(), {
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
  font-size: 0.875rem;
  border: 1px solid var(--color-border);
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
</style>
