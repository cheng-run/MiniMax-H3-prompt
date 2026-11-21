<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section class="not-found">
    <!-- 404 也属于这套系统：一条断掉的刻度线，比一句冷冰冰的「页面不存在」更接近这个产品
         对「没走到那一步」的表达方式。 -->
    <span class="broken-rail" aria-hidden="true"></span>
    <p class="kicker">404</p>
    <h1>这个地址没有对应的页面</h1>
    <p class="hint">可能是会话被删了，或者链接少了一截。</p>
    <p><RouterLink to="/" class="ink-link">返回会话列表</RouterLink></p>
  </section>
</template>

<style scoped>
.not-found {
  max-width: 560px;
  padding-top: var(--space-6);
}
.broken-rail {
  display: block;
  width: 96px;
  height: 2px;
  margin-bottom: var(--space-4);
  background: repeating-linear-gradient(90deg, var(--color-ink-faint) 0 6px, transparent 6px 12px);
}
.kicker {
  font-family: var(--font-data);
  font-size: var(--text-micro);
  letter-spacing: var(--track-wide);
  color: var(--color-ink-faint);
}
h1 {
  margin: var(--space-2) 0 var(--space-3);
  font-size: var(--text-lead);
  font-weight: 500;
}
.hint {
  margin-bottom: var(--space-4);
  font-size: var(--text-body);
  color: var(--color-ink-soft);
}
</style>
