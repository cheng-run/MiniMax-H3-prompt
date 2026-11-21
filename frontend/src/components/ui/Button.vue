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
  color: var(--color-on-accent);
  border-color: var(--color-accent);
}
.btn--primary:hover {
  background: var(--color-accent-hover);
  border-color: var(--color-accent-hover);
}
.btn--primary:active {
  background: var(--color-accent-active);
  border-color: var(--color-accent-active);
}
.btn--secondary {
  background: var(--color-surface);
  color: var(--color-text);
}
/* 次要按钮悬停靠底色下沉一档（画布灰）——平面分层里不加阴影、不位移。 */
.btn--secondary:hover {
  background: var(--color-bg);
  border-color: var(--color-text-muted);
}
.btn--secondary:active {
  background: var(--color-border);
}
/* 禁用态显式覆盖：主按钮的实心底色会盖住全局 :disabled 的透明度观感，
   这里同时压暗悬停反馈，避免「看着能点」。文字也退到次要灰——白底次要按钮
   在灰画布上只靠 0.45 透明度，扫一眼仍像能点（2026-11-21 polish 实测「上一页／
   下一页」并排几乎同貌）。 */
.btn:disabled,
.btn:disabled:hover,
.btn:disabled:active {
  opacity: 0.45;
  cursor: not-allowed;
}
.btn--primary:disabled {
  background: var(--color-accent);
  border-color: var(--color-accent);
}
.btn--secondary:disabled {
  background: var(--color-surface);
  border-color: var(--color-border);
  color: var(--color-text-muted);
}
</style>
