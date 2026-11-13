<script setup lang="ts">
import { computed } from 'vue'

import type { ActionItemOut, Asset, SegmentOut } from '@/api/client'

import SegmentCard from './SegmentCard.vue'

// 「分段」分区（D6 spec §4.2）：规划结果**只读**——不提供编辑、合并/拆分，也没有
// 「确认分段」按钮（Q4）。段进度用「已通过 x / 总数 y」表达，不用百分比（§4.3）。
const props = defineProps<{
  sessionId: string
  segments: SegmentOut[]
  assets: Asset[]
  actionItems: ActionItemOut[]
}>()

const passedCount = computed(
  () => props.segments.filter((segment) => segment.status === 'passed').length,
)

// 当前段＝第一个还没通过的段。高亮刷新完全由快照驱动，不靠本地进度：段通过后
// SSE 失效信号重拉详情，新的段状态让高亮自然右移。
const currentIndex = computed(() => {
  const segment = props.segments.find((entry) => entry.status !== 'passed')
  return segment?.index ?? null
})
const emit = defineEmits<{ refresh: [] }>()

// 待办与段按段号配对：每段最多一个 pending 的 segment_video 待办，表单只挂在对应
// 那一段上；待办属于哪段由服务端给（`segment_index`），前端不按顺序猜。
const pendingItem = computed(
  () =>
    props.actionItems.find((item) => item.kind === 'segment_video' && item.status === 'pending') ??
    null,
)

function itemFor(index: number): ActionItemOut | null {
  return pendingItem.value?.segment_index === index ? pendingItem.value : null
}
</script>

<template>
  <section class="segments" aria-label="分段">
    <p v-if="!segments.length" class="muted">暂无分段。</p>

    <template v-else>
      <p class="progress" role="status">已通过 {{ passedCount }} / 总数 {{ segments.length }}</p>

      <ol class="segment-list">
        <li
          v-for="segment in segments"
          :key="segment.index"
          class="segment"
          :class="{ 'segment--current': segment.index === currentIndex }"
          :data-segment-index="segment.index"
        >
          <SegmentCard
            :session-id="sessionId"
            :segment="segment"
            :item="itemFor(segment.index)"
            :assets="assets"
            @refresh="emit('refresh')"
          />
        </li>
      </ol>
    </template>
  </section>
</template>

<style scoped>
.segments {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.progress {
  margin: 0;
  font-weight: 600;
}
.segment-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}
.segment {
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.segment--current {
  border-color: var(--color-accent);
}
.muted {
  color: var(--color-text-muted);
}
</style>
