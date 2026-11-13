<script setup lang="ts">
import { computed } from 'vue'

import type { ActionItemOut, ArtifactInfo, Asset, SegmentOut } from '@/api/client'

import { segmentInjectionCardName } from './artifactKinds'
import SegmentCard from './SegmentCard.vue'

// 「分段」分区（D6 spec §4.2）：规划结果**只读**——不提供编辑、合并/拆分，也没有
// 「确认分段」按钮（Q4）。段进度用「已通过 x / 总数 y」表达，不用百分比（§4.3）。
const props = defineProps<{
  sessionId: string
  // 路线决定段分区的能力（D7 §5.5 / #258）：ref 的已通过段能「换一段」，base 不能。
  mode: string
  segments: SegmentOut[]
  assets: Asset[]
  actionItems: ActionItemOut[]
  artifacts: ArtifactInfo[]
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

const isRef = computed(() => props.mode === 'ref')

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

// 段级注入卡按「产物在不在」显示（D7 §9）：段循环没跑到该段就没有这份文件，
// 组件不做空卡，也不在快照里另记一份状态。
const artifactsByName = computed(() => new Map(props.artifacts.map((item) => [item.name, item])))

function cardFor(index: number): ArtifactInfo | null {
  return artifactsByName.value.get(segmentInjectionCardName(index)) ?? null
}
</script>

<template>
  <section class="segments" aria-label="分段">
    <p class="progress" role="status">已通过 {{ passedCount }} / 总数 {{ segments.length }}</p>

    <p v-if="!segments.length" class="muted" role="status">
      规划中：分段还没落盘，完成后这里会逐段列出。
    </p>

    <ol v-else class="segment-list">
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
          :card-artifact="cardFor(segment.index)"
          :is-ref="isRef"
          @refresh="emit('refresh')"
        />
      </li>
    </ol>
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
