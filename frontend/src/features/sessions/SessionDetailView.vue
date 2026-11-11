<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError, getSession, startSessionJob } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'

import { newIdempotencyKey } from './keys'
import { statusLabel, statusTone } from './status'

// 工作台最小详情页（D4-3）：只做「深链可刷新恢复 + 会话快照原始呈现」。
// 四分区、SSE 与审查交互归 #231，不在这里提前铺开。
//
// 会话标识本身含斜杠（`<slug>/GEN001`），路由参数用 `:id(.*)` 承接；这里把它原样
// 交给生成客户端，URL 编码由 openapi-fetch 负责。
const route = useRoute()
const sessionId = computed(() => {
  const id = route.params.id
  return Array.isArray(id) ? (id[0] ?? '') : (id ?? '')
})

// 快照是唯一数据源（spec §6.1）：刷新即重拉，不做事件投影。
const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: computed(() => ['session', sessionId.value]),
  queryFn: () => getSession(sessionId.value),
  enabled: computed(() => sessionId.value.length > 0),
  retry: false,
})

// created 兜底（spec §5）：创建与起跑是两个请求，中间失败或浏览器挂掉时会话停在
// created——这个按钮让用户不刷新也能把它推起来。
const starting = ref(false)
const startError = ref<string | null>(null)

async function start(): Promise<void> {
  if (starting.value || !sessionId.value) {
    return
  }
  starting.value = true
  startError.value = null
  try {
    await startSessionJob(sessionId.value, newIdempotencyKey())
    await refetch()
  } catch (err) {
    startError.value =
      err instanceof ApiError ? (err.problem?.detail ?? err.message) : '起跑失败，请稍后重试。'
  } finally {
    starting.value = false
  }
}

const errorMessage = computed(() =>
  error.value instanceof ApiError ? error.value.message : '加载失败，请稍后重试。',
)
const isCreated = computed(() => data.value?.status === 'created')
</script>

<template>
  <section class="detail">
    <p class="back"><RouterLink to="/">← 返回会话列表</RouterLink></p>

    <LoadingState v-if="isPending" />

    <ErrorNotice v-else-if="isError" :message="errorMessage">
      <Button @click="refetch()">重试</Button>
    </ErrorNotice>

    <template v-else-if="data">
      <h1>{{ data.plot || data.session_id }}</h1>
      <p class="status-line">
        <StatusBadge :tone="statusTone(data.status)" :label="statusLabel(data.status)" />
        <span class="meta">{{ data.session_id }}</span>
      </p>

      <ErrorNotice v-if="startError" :message="startError" />
      <p v-if="isCreated">
        <Button :disabled="starting" @click="start">{{ starting ? '正在开始…' : '开始' }}</Button>
      </p>

      <section>
        <h2>概览</h2>
        <dl class="facts">
          <dt>剧情</dt>
          <dd>{{ data.plot }}</dd>
          <dt>模式</dt>
          <dd>{{ data.brief.mode }}</dd>
          <dt>变体</dt>
          <dd>{{ data.brief.variant }}</dd>
          <dt>时长</dt>
          <dd>{{ data.brief.duration }} 秒</dd>
          <dt>风格</dt>
          <dd>{{ data.brief.style }}</dd>
          <dt>语言</dt>
          <dd>{{ data.brief.language }}</dd>
          <template v-if="data.brief.clarifications">
            <dt>补充说明</dt>
            <dd>{{ data.brief.clarifications }}</dd>
          </template>
          <template v-if="data.brief.draft">
            <dt>草稿</dt>
            <dd>{{ data.brief.draft }}</dd>
          </template>
        </dl>
      </section>

      <section>
        <h2>待办</h2>
        <p v-if="!data.action_items?.length" class="muted">暂无待办。</p>
        <ul v-else>
          <li v-for="item in data.action_items" :key="item.item_id">
            {{ item.kind }} · {{ item.status }}
          </li>
        </ul>
      </section>

      <section>
        <h2>产物</h2>
        <p v-if="!data.artifacts?.length" class="muted">暂无产物。</p>
        <ul v-else>
          <li v-for="artifact in data.artifacts" :key="artifact.artifact_id">
            {{ artifact.name }}（{{ artifact.bytes }} 字节）
          </li>
        </ul>
      </section>

      <section>
        <h2>素材</h2>
        <p v-if="!data.assets?.length" class="muted">暂无素材。</p>
        <ul v-else>
          <li v-for="asset in data.assets" :key="asset.asset_id">
            {{ asset.name }}（{{ asset.bytes }} 字节）
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<style scoped>
.detail {
  max-width: 880px;
}
.back {
  margin: 0 0 var(--space-2);
}
.status-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.meta {
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
.muted {
  color: var(--color-text-muted);
}
.facts {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--space-2) var(--space-3);
}
section {
  margin-top: var(--space-4);
}
</style>
