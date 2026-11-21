<script setup lang="ts">
import { computed } from 'vue'

import { formatElapsed, type ProgressStep, type ProgressView } from './progress'

// 概览顶部的步骤带（#272）：只负责把纯函数算好的步骤铺成一行，不做状态推导。
// 未知节点名已经在 progress/status 里回退成原名，这里不再兜底。
const props = defineProps<{ view: ProgressView }>()

// 当前节点跑完（done）后只剩「上一步」：这一行不能因此整行消失，否则刷新后
// 服务端补发的那条 done 就什么都看不到了（#272）。
const summary = computed(() => {
  const parts: string[] = []
  if (props.view.nodeLabel) {
    let current = `当前：${props.view.nodeLabel}`
    if (props.view.elapsedMs !== null) {
      current += ` · 已耗时 ${formatElapsed(props.view.elapsedMs)}`
    }
    parts.push(current)
  }
  if (props.view.previousLabel) {
    parts.push(`上一步：${props.view.previousLabel}`)
  }
  return parts.join(' · ')
})

// 每格的读屏标签（2026-11-21 layout）：视觉上 done 是绿框、current 是蓝框，读屏
// 只听到一串光秃秃的步骤名，无从知道自己在第几步、哪一步是当前的。
const STATE_TEXT: Record<ProgressStep['state'], string> = {
  done: '已完成',
  current: '进行中',
  pending: '未开始',
}
function stepLabelText(step: ProgressStep, index: number): string {
  const position = `第 ${index + 1} 步（共 ${props.view.steps.length} 步）`
  const detail = step.detail ? `，${step.detail}` : ''
  return `${position} ${step.label}${detail}，${STATE_TEXT[step.state]}`
}

// 进度概览一句：不靠读屏用户逐个听格子才知道整体走到哪。
const position = computed(() => {
  const total = props.view.steps.length
  const index = props.view.steps.findIndex((step) => step.state === 'current')
  if (index >= 0) {
    return `第 ${index + 1} / ${total} 步：${props.view.steps[index]!.label}`
  }
  const done = props.view.steps.filter((step) => step.state === 'done').length
  return done === total && total > 0 ? `全部 ${total} 步已完成` : `${done} / ${total} 步已完成`
})

// 位置与「当前/上一步」合成一行（2026-11-21 layout）：420 宽下两块 13px 灰字各占
// 一行读起来很碎，它们说的本来就是同一件事——走到哪了。合并后只有一个 live region，
// 读屏也不会被两段同时更新的文字打断两次。
const line = computed(() => [position.value, summary.value].filter(Boolean).join(' · '))
</script>

<template>
  <nav class="band" aria-label="步骤带">
    <ol class="steps" aria-label="步骤进度">
      <li
        v-for="(step, index) in view.steps"
        :key="step.key"
        :data-step="step.key"
        :data-state="step.state"
        class="step"
        :class="`step--${step.state}`"
        :aria-current="step.state === 'current' ? 'step' : undefined"
      >
        <!-- 读屏读这一句（位置 + 状态），可见的标签与计数只是它的视觉缩写。 -->
        <span class="sr-only">{{ stepLabelText(step, index) }}</span>
        <span class="step-label" aria-hidden="true">{{ step.label }}</span>
        <span v-if="step.detail" class="step-detail" aria-hidden="true">{{ step.detail }}</span>
      </li>
    </ol>
    <p class="current" role="status">{{ line }}</p>
  </nav>
</template>

<style scoped>
.band {
  margin: 0 0 var(--space-3);
}
.steps {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}
/* 位置 + 当前节点的一句（见 `line`）：原来分成两块各占一行，420 宽下太碎。 */
.current {
  margin: var(--space-2) 0 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-label);
}
.step {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  padding: 2px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  font-size: var(--font-size-label);
}
.step--done {
  border-color: var(--color-ok-text);
  color: var(--color-text);
}
.step--current {
  border-color: var(--color-focus);
  color: var(--color-text);
  font-weight: 600;
}
.step-detail {
  font-variant-numeric: tabular-nums;
}
.current {
  margin: var(--space-2) 0 0;
  color: var(--color-text-muted);
}
</style>
