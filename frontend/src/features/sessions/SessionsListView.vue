<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError, getHealth, listSessions } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'

import SessionCreateForm from './SessionCreateForm.vue'
import { statusLabel, statusTone } from './status'
import { useClipboardCopy } from './useClipboardCopy'

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

// 缺密钥提示（#238 / spec #235 §7）：健康资源里的 missing_keys 非空就显示「去哪个
// 文件、按什么格式写」。读失败（服务异常）时**整块不出现**——提示是加法，不能因为
// 它把会话列表带崩或挡住已有的错误态。
const { data: health } = useQuery({ queryKey: ['health'], queryFn: getHealth, retry: false })
const missingKeys = computed(() => health.value?.missing_keys ?? [])
const envPath = computed(() => health.value?.env_path ?? '')
const configPath = computed(() => health.value?.config_path ?? '')

// 「复制路径」而不是「打开目录」（用户 2026-11-12 裁定）：打开资源管理器要新增端点、
// 还要处理 file:// 的策略差异；复制路径零依赖，用户自己粘贴到地址栏即可。
// 两处复制共用 useClipboardCopy（每次调用一份独立状态），模板变量名保持原样。
const { state: copyState, label: copyLabel, copy } = useClipboardCopy('复制路径')

function copyEnvPath(): Promise<void> {
  return copy(envPath.value)
}

// 实际地址（#237 / spec #235 §5.2、D5-AC2）：启动入口把 `http://127.0.0.1:<实际端口>/`
// 写进 app.state.base_url，健康资源原样下发。端口只在入口算一次，界面不再拼。
// 空串 = 未知（测试直接 create_app、或没按入口启动）——这时整块不显示，不显示假地址。
const baseUrl = computed(() => health.value?.base_url ?? '')

// 与密钥路径的复制按钮分开存状态：useClipboardCopy 每次调用互不影响，点一个按钮
// 不会把另一个的文案也改掉。
const {
  state: addressCopyState,
  label: addressCopyLabel,
  copy: copyAddress,
} = useClipboardCopy('复制')

function copyBaseUrl(): Promise<void> {
  return copyAddress(baseUrl.value)
}
</script>

<template>
  <section class="sessions">
    <div class="sessions__header">
      <h1>会话</h1>
      <Button @click="showForm = !showForm">
        {{ showForm ? '收起新建' : '新建会话' }}
      </Button>
    </div>

    <section v-if="baseUrl" class="local-address">
      <p class="local-address__line">
        本地地址：<code>{{ baseUrl }}</code>
      </p>
      <Button variant="secondary" @click="copyBaseUrl">{{ addressCopyLabel }}</Button>
      <p v-if="addressCopyState === 'failed'" class="local-address__failed">
        复制失败：请手动选中上面的地址复制。
      </p>
    </section>

    <section v-if="missingKeys.length > 0" class="config-hint">
      <h2 class="config-hint__title">还没有配置模型密钥</h2>
      <p>本程序从下面这个文件读密钥，把缺的变量写进去（一行一个，值换成你自己的）：</p>
      <p class="config-hint__path">
        密钥文件：<code>{{ envPath }}</code>
      </p>
      <Button variant="secondary" @click="copyEnvPath">{{ copyLabel }}</Button>
      <p v-if="copyState === 'failed'" class="config-hint__failed">
        复制失败：请手动选中上面的路径复制。
      </p>
      <ul class="config-hint__keys">
        <li v-for="key in missingKeys" :key="key">
          <code>{{ key }}=你的密钥</code>
        </li>
      </ul>
      <p class="config-hint__path">
        配置文件：<code>{{ configPath }}</code>
      </p>
      <p class="config-hint__note">改完配置需重启程序才生效（本程序不热重载配置）。</p>
    </section>

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
.local-address {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.local-address__line,
.local-address__failed {
  margin: 0;
  color: var(--color-text-muted);
  overflow-wrap: anywhere;
}
.local-address__line {
  flex: 1 1 auto;
}
.config-hint {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.config-hint__title {
  margin: 0;
  font-size: 1rem;
}
.config-hint__path,
.config-hint__note,
.config-hint__failed {
  margin: 0;
  color: var(--color-text-muted);
  overflow-wrap: anywhere;
}
.config-hint__keys {
  margin: 0;
  padding-left: var(--space-4);
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
