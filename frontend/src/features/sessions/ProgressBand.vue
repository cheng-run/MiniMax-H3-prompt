<script setup lang="ts">
import { computed } from 'vue'

import { formatElapsed, type ProgressView } from './progress'

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
</script>

<template>
  <nav class="band" aria-label="步骤带">
    <ol class="steps" aria-label="步骤进度">
      <li
        v-for="step in view.steps"
        :key="step.key"
        :data-step="step.key"
        :data-state="step.state"
        class="step"
        :class="`step--${step.state}`"
      >
        <span class="step-label">{{ step.label }}</span>
        <span v-if="step.detail" class="step-detail">{{ step.detail }}</span>
      </li>
    </ol>
    <p v-if="summary" class="current" role="status">{{ summary }}</p>
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
.step {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  padding: 2px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  font-size: 0.875rem;
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
