<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError, listSessions } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'

import SessionCreateForm from './SessionCreateForm.vue'
import { statusLabel, statusTone } from './status'

const PAGE_SIZE = 20

// 服务端只下发「下一页」游标，没有「上一页」指针；走过的游标自己存一份栈。
const cursors = ref<(string | null)[]>([null])
const pageIndex = ref(0)
const currentCursor = computed(() => cursors.value[pageIndex.value])

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: computed(() => ['sessions', { cursor: currentCursor.value }]),
  queryFn: () => listSessions({ limit: PAGE_SIZE, cursor: currentCursor.value }),
  retry: false,
})

const items = computed(() => data.value?.items ?? [])
const hasNext = computed(() => Boolean(data.value?.next_cursor))

function nextPage(): void {
  const next = data.value?.next_cursor
  if (!next) {
    return
  }
  // 截断前进分支：先回退再前进时，旧的前向游标不该残留成第二条分叉。
  cursors.value = [...cursors.value.slice(0, pageIndex.value + 1), next]
  pageIndex.value += 1
}

function prevPage(): void {
  if (pageIndex.value > 0) {
    pageIndex.value -= 1
  }
}

// 新建会话表单在列表页展开（spec §4）；展开是纯 UI 状态，随组件存亡。
const showForm = ref(false)

const errorMessage = computed(() =>
  error.value instanceof ApiError ? error.value.message : '加载失败，请稍后重试。',
)
</script>

<template>
  <section class="sessions">
    <div class="sessions__header">
      <h1>会话</h1>
      <Button @click="showForm = !showForm">
        {{ showForm ? '收起新建' : '新建会话' }}
      </Button>
    </div>

    <SessionCreateForm v-if="showForm" />

    <LoadingState v-if="isPending" />

    <ErrorNotice v-else-if="isError" :message="errorMessage">
      <Button @click="refetch()">重试</Button>
    </ErrorNotice>

    <template v-else>
      <p v-if="items.length === 0" class="sessions__empty">还没有会话，点击「新建会话」开始。</p>
      <ul v-else class="session-list">
        <li v-for="item in items" :key="item.session_id" class="session-item">
          <RouterLink class="session-link" :to="`/sessions/${item.session_id}`">
            {{ item.plot || item.session_id }}
          </RouterLink>
          <StatusBadge :tone="statusTone(item.status)" :label="statusLabel(item.status)" />
          <span class="session-meta">{{ item.variant }} · 修订 {{ item.revision }}</span>
        </li>
      </ul>

      <div class="pager">
        <Button variant="secondary" :disabled="pageIndex === 0" @click="prevPage">上一页</Button>
        <Button variant="secondary" :disabled="!hasNext" @click="nextPage">下一页</Button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.sessions {
  max-width: 880px;
}
.sessions__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.sessions__empty {
  color: var(--color-text-muted);
}
.session-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.session-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.session-link {
  flex: 1 1 auto;
  color: var(--color-accent);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-meta {
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
.pager {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
</style>
