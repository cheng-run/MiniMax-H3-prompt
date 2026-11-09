<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <header class="app-header">
    <nav class="app-nav" aria-label="主导航">
      <RouterLink to="/">首页</RouterLink>
      <RouterLink to="/diagnostics">诊断</RouterLink>
    </nav>
  </header>
  <main class="app-main">
    <RouterView />
  </main>
</template>

<style scoped>
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
</style>
