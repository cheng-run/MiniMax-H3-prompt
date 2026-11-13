<script setup lang="ts">
import { computed } from 'vue'

import type { SegmentOut } from '@/api/client'

// 「分段」分区（D6 spec §4.2）：规划结果**只读**——不提供编辑、合并/拆分，也没有
// 「确认分段」按钮（Q4）。段进度用「已通过 x / 总数 y」表达，不用百分比（§4.3）。
const props = defineProps<{ segments: SegmentOut[] }>()

const passedCount = computed(
  () => props.segments.filter((segment) => segment.status === 'passed').length,
)

// 当前段的位次先留好：D6-2 起段状态会多于「待开始」，这里按「第一个还没通过的段」
// 判定，状态仍只读；高亮刷新完全由快照驱动，不靠本地进度。
const currentIndex = computed(() => {
  const segment = props.segments.find((entry) => entry.status !== 'passed')
  return segment?.index ?? null
})

// 状态文案集中一处：未知状态原样显示，不猜一个更好看的词。
const STATUS_LABELS: Record<string, string> = {
  pending: '待开始',
  passed: '已通过',
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

function shotsLabel(shots: number[] | undefined): string {
  return shots && shots.length > 0 ? shots.map((shot) => `镜头 ${shot}`).join('、') : '—'
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
          <header class="segment__head">
            <h2>第 {{ segment.index }} 段</h2>
            <span class="segment__time">{{ segment.start_s }}–{{ segment.end_s }} 秒</span>
            <span class="segment__status">{{ statusLabel(segment.status) }}</span>
          </header>
          <p class="segment__shots">覆盖镜头：{{ shotsLabel(segment.shots_in_segment) }}</p>
          <p class="segment__summary">{{ segment.summary }}</p>
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
.segment__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}
.segment__head h2 {
  margin: 0;
  font-size: 1rem;
}
.segment__time {
  color: var(--color-text-muted);
}
.segment__status {
  margin-left: auto;
  font-weight: 600;
}
.segment__shots,
.segment__summary {
  margin: var(--space-1) 0 0;
}
.muted {
  color: var(--color-text-muted);
}
</style>
