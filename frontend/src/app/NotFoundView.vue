<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section class="not-found">
    <h1>页面不存在</h1>
    <p>你访问的地址没有对应的页面。</p>
    <p>
      <RouterLink to="/">返回首页</RouterLink>
    </p>
  </section>
</template>

<style scoped>
.not-found {
  max-width: 640px;
}
</style>
