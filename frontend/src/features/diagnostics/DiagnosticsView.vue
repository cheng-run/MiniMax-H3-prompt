<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'

import { ApiError, diagnosticsBundleUrl, ensureSessionToken, getHealth } from '@/api/client'
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

// 导出前把清单摆出来（spec #235 §8.2 的表格）：用户点下载之前就知道包里有什么、
// 没有什么（不含密钥、不含素材媒体）。
const BUNDLE_ITEMS = [
  'manifest.json —— 版本、生成时间、数据根、端口、冻结/开发形态、日志窗口',
  'environment.txt —— OS 版本、Python 版本、WebView2 版本（可读时）、架构',
  'config.json —— 配置快照（只有键名与非敏感值，密钥值一律 <redacted>）',
  'logs/*.log —— 保留窗口内的运行日志',
  'sessions/<id>/session-state.json —— 各会话状态',
  'sessions/<id>/events.jsonl —— 各会话事件流',
  'artifacts/<id>/*.md —— 文本产物副本',
]

const downloadAnchor = ref<HTMLAnchorElement | null>(null)
const exportError = ref('')
const bundleUrl = diagnosticsBundleUrl()

async function exportBundle() {
  exportError.value = ''
  try {
    // 先取令牌：引导端点顺带下发 `wb_session` Cookie；`<a download>` 由浏览器自己
    // 发请求、挂不上自定义头，只能靠这个 Cookie（与产物下载同一份约定）。
    await ensureSessionToken()
    downloadAnchor.value?.click()
  } catch {
    exportError.value = '导出失败：无法取得访问凭据，请刷新页面后重试。'
  }
}
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

    <section class="bundle">
      <h2>导出诊断包</h2>
      <p>诊断包是一份 zip，导出前请确认将包含以下条目：</p>
      <ul class="bundle-items">
        <li v-for="item in BUNDLE_ITEMS" :key="item">{{ item }}</li>
      </ul>
      <p class="bundle-note">
        不含密钥值、不含 <code>%APPDATA%</code> 下的 .env，也不含素材图片与视频。
      </p>
      <Button @click="exportBundle()">导出诊断包</Button>
      <!-- 真正的下载入口：Cookie 由 ensureSessionToken() 下发，文件名由服务端
           Content-Disposition 给（URL 里不放凭据，ADR 0033）。 -->
      <a ref="downloadAnchor" class="bundle-download" :href="bundleUrl" download>下载诊断包</a>
      <p v-if="exportError" role="alert" class="bundle-error">{{ exportError }}</p>
    </section>
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
.bundle {
  margin-top: var(--space-5);
}
.bundle-items {
  margin: var(--space-2) 0;
  padding-left: var(--space-4);
}
.bundle-note {
  color: var(--color-text-muted);
}
/* 下载锚点不显示：用户看到的是按钮，锚点只承担「带 Cookie 的浏览器原生下载」。 */
.bundle-download {
  display: none;
}
.bundle-error {
  color: var(--color-error-text);
}
</style>
