<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed } from 'vue'

import { getHealth } from '@/api/client'

// REST 服务器状态交给 Vue Query（缓存/加载/错误都在这里），不进 Pinia。
const { data, isPending, isError, error } = useQuery({
  queryKey: ['health'],
  queryFn: getHealth,
})

const errorMessage = computed(() =>
  error.value instanceof Error ? error.value.message : '加载失败',
)
</script>

<template>
  <section class="diagnostics">
    <h1>诊断</h1>
    <p v-if="isPending" role="status">加载中…</p>
    <div v-else-if="isError" role="alert">加载失败：{{ errorMessage }}</div>
    <dl v-else class="diagnostics-list">
      <dt>状态</dt>
      <dd>{{ data?.status }}</dd>
      <dt>服务</dt>
      <dd>{{ data?.service }}</dd>
      <dt>版本</dt>
      <dd>{{ data?.version }}</dd>
    </dl>
  </section>
</template>

<style scoped>
.diagnostics {
  max-width: 640px;
}
.diagnostics-list {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--space-2) var(--space-3);
}
</style>
