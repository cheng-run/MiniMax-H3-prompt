<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <!-- 跳转主内容链接：键盘用户第一个 Tab 就能拿到，跳过导航直达正文。 -->
  <a class="skip-link" href="#main-content">跳到主内容</a>

  <header class="app-header">
    <nav class="app-nav" aria-label="主导航">
      <RouterLink to="/">首页</RouterLink>
      <RouterLink to="/diagnostics">诊断</RouterLink>
    </nav>
  </header>

  <!-- tabindex="-1" 让跳转链接的 #main-content 能真正把焦点移进来。 -->
  <main id="main-content" class="app-main" tabindex="-1">
    <slot />
  </main>
</template>

<style scoped>
/* 跳转链接默认移出视口，聚焦时回到可见位置并显示焦点环（可访问性基线）。 */
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 100;
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface);
  border: 2px solid var(--color-focus);
  border-radius: var(--radius-sm);
}
.skip-link:focus {
  left: var(--space-2);
}
.app-header {
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}
.app-nav {
  display: flex;
  gap: var(--space-4);
}
.app-main {
  padding: var(--space-4);
}
/* main 拿到焦点时不要额外的默认边框（它只是跳转落点，不是可交互控件）。 */
.app-main:focus {
  outline: none;
}
</style>
