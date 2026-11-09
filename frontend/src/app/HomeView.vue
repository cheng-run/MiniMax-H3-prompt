<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section class="home">
    <h1>MiniMax H3 工作台</h1>
    <p>这是 D1 前端工程的最小纵向切片，用于验证前后端全链路。</p>
    <p>
      <RouterLink to="/diagnostics">查看服务诊断</RouterLink>
    </p>
  </section>
</template>

<style scoped>
.home {
  max-width: 640px;
}
</style>
