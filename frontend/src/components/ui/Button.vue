<script setup lang="ts">
// 原生 button：键盘 Enter/Space 天生可用，不额外造键盘处理。
// primary = 这一步唯一要你做的那一下；secondary = 同组的其他出口；ghost = 不抢视线的第三档。
withDefaults(
  defineProps<{
    type?: 'button' | 'submit' | 'reset'
    variant?: 'primary' | 'secondary' | 'ghost'
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
  display: inline-flex;
  align-items: center;
  justify-content: center;
  /* 高度写死 32px：三种变体基线一致，并排时不会一高一低。正文行高 1.55 比按钮内高
     略大，靠 flex 居中让它溢出居中而不是裁字。 */
  height: 32px;
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  font-size: var(--text-body);
  cursor: pointer;
  transition:
    background-color var(--duration-fast) var(--ease-out),
    border-color var(--duration-fast) var(--ease-out),
    color var(--duration-fast) var(--ease-out);
}

/* 主按钮：钠黄只在这里出现一次——它是「现在该点的那一下」。
   悬停靠提亮而不是换色，色相保持不变，主题切换时两种底都成立。 */
.btn--primary {
  background: var(--color-signal);
  border-color: var(--color-signal);
  color: var(--color-signal-ink);
}
.btn--primary:hover {
  filter: brightness(1.08);
}

/* 次按钮：透明底 + 一根发光线。悬停时线先亮起来，再垫一层浮起底。 */
.btn--secondary {
  background: transparent;
  border-color: var(--color-rule-strong);
  color: var(--color-ink);
}
.btn--secondary:hover {
  background: var(--color-plate-raised);
  border-color: var(--color-ink-soft);
}
.btn--secondary:active {
  background: var(--color-plate);
}

/* 幽灵按钮：没有线也没有底，只有字；用于同一区域里的第三档出口。 */
.btn--ghost {
  background: transparent;
  border-color: transparent;
  color: var(--color-ink-soft);
}
.btn--ghost:hover {
  background: var(--color-plate-raised);
  color: var(--color-ink);
}
.btn--ghost:active {
  background: var(--color-plate);
}

/* 禁用态：全局 :disabled 已给透明度与 not-allowed，不再叠加悬停反馈。
   主按钮额外去饱和——高饱和的钠黄会读成「还能点」。 */
.btn--primary:disabled,
.btn--primary:disabled:hover,
.btn--primary:disabled:active {
  filter: saturate(0.2);
}
</style>
