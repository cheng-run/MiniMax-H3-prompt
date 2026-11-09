<script setup lang="ts">
// 原生 button：键盘 Enter/Space 天生可用，不额外造键盘处理。
withDefaults(
  defineProps<{
    type?: 'button' | 'submit' | 'reset'
    variant?: 'primary' | 'secondary'
  }>(),
  { type: 'button', variant: 'primary' },
)
</script>

<template>
  <button :type="type" class="btn" :class="`btn--${variant}`">
    <slot />
  </button>
</template>

<style scoped>
.btn {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: pointer;
  font: inherit;
}
.btn--primary {
  background: var(--color-accent);
  color: #fff;
  border-color: var(--color-accent);
}
.btn--secondary {
  background: var(--color-surface);
  color: var(--color-text);
}
</style>
