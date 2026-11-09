<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed } from 'vue'

import { ApiError, getHealth } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useUiStore } from '@/stores/ui'

// 服务器状态（请求/缓存/加载/错误）归 Vue Query；retry: false 让错误立即呈现，
// 重试交给用户点按钮（按钮触发 refetch）。
const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['health'],
  queryFn: getHealth,
  retry: false,
})

// 客户端 UI 状态（详情是否展开）归 Pinia，不和服务端数据混在一起。
const ui = useUiStore()

const errorMessage = computed(() =>
  error.value instanceof ApiError ? error.value.message : '加载失败，请稍后重试。',
)
</script>

<template>
  <section class="diagnostics">
    <h1>诊断</h1>

    <LoadingState v-if="isPending" />

    <ErrorNotice v-else-if="isError" :message="errorMessage">
      <Button @click="refetch()">重试</Button>
    </ErrorNotice>

    <template v-else>
      <p>
        <StatusBadge tone="ok" label="服务正常" />
      </p>
      <dl class="diagnostics-list">
        <dt>状态</dt>
        <dd>{{ data?.status }}</dd>
        <dt>服务</dt>
        <dd>{{ data?.service }}</dd>
        <dt>版本</dt>
        <dd>{{ data?.version }}</dd>
      </dl>

      <Button
        variant="secondary"
        :aria-expanded="ui.diagnosticsExpanded"
        @click="ui.toggleDiagnostics()"
      >
        {{ ui.diagnosticsExpanded ? '收起详情' : '展开详情' }}
      </Button>
      <pre v-if="ui.diagnosticsExpanded" class="diagnostics-detail">{{
        JSON.stringify(data, null, 2)
      }}</pre>
    </template>
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
.diagnostics-detail {
  margin-top: var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow-x: auto;
}
</style>
