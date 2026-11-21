<script setup lang="ts">
import { computed } from 'vue'

import { formatElapsed, type ProgressStep, type ProgressView } from './progress'

// 详情页顶部的步骤带（#272）：只负责把纯函数算好的步骤铺成一条刻度线，不做状态推导。
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

// 每格的读屏标签（2026-11-21 layout）：视觉上 done 是一根中灰短线、current 是双线
// 钠黄，读屏只听到一串光秃秃的步骤名，无从知道自己在第几步、哪一步是当前的。
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

// 进度概览一句：不靠读屏用户逐个听刻度才知道整体走到哪。
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

// 横向滚动的提示只在步骤真的多到要滚时才画（>7 步）：少步骤时那条渐隐是纯噪声，
// 还会把最后一格的标签压暗。
const scrollable = computed(() => props.view.steps.length > 7)
</script>

<template>
  <nav class="band" aria-label="步骤带">
    <!-- 滚动区：步骤多到放不下时横向滚；基线放在内层「轨道」里，随内容一起滚。 -->
    <div class="band__frames">
      <div class="track">
        <!-- 贯穿的 1px 基线：纯装饰，读屏不该读到它。 -->
        <span class="baseline" aria-hidden="true"></span>
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
            <!-- 读屏读这一句（位置 + 状态），可见的刻度与标签只是它的视觉缩写。 -->
            <span class="sr-only">{{ stepLabelText(step, index) }}</span>
            <!-- 当前步的三角指示器：占位常驻，只切可见性——否则标签会随状态左右跳一格。 -->
            <span class="tri" aria-hidden="true"></span>
            <!-- 刻度线：挂在基线上的那一段。 -->
            <span class="tick" aria-hidden="true"></span>
            <span class="step-label" aria-hidden="true">{{ step.label }}</span>
            <span v-if="step.detail" class="step-detail" aria-hidden="true">{{ step.detail }}</span>
          </li>
        </ol>
      </div>
      <!-- 右缘渐隐：步骤多到要横向滚动时，让人看得出右边还有内容。 -->
      <span v-if="scrollable" class="scroll-hint" aria-hidden="true"></span>
    </div>
    <p class="current" role="status">{{ line }}</p>
  </nav>
</template>

<style scoped>
.band {
  margin: 0;
}
/* 滚动容器与渐隐要叠在一起：渐隐不能跟着内容滚走，否则滚到中段就看不见了。 */
.band__frames {
  position: relative;
}
/* 轨道：基线与刻度同宽、一起横向滚——滚动容器放在这一层，基线才不会被截短。 */
.track {
  position: relative;
  overflow-x: auto;
  scrollbar-width: none;
}
.track::-webkit-scrollbar {
  display: none;
}
/* 贯穿的 1px 基线：一条与内容同宽的装饰线，绝对定位在轨道顶部。 */
.baseline {
  position: absolute;
  top: var(--space-3);
  left: 0;
  width: 100%;
  height: 1px;
  background: var(--color-rule);
}
.steps {
  position: relative;
  display: flex;
  margin: 0;
  padding: 0;
  list-style: none;
  /* 不换行：这是「一行刻度」，窄屏宁可横向滚动也不折成两行断开的基线。 */
  flex-wrap: nowrap;
  /* 每格至少占满自己的内容宽度，多余空间按份数分——基线因此是一条连续的线。 */
  justify-content: space-between;
}
.step {
  position: relative;
  /* 每格占满自己那份宽度：基线因此是一条连续的线，而不是左挤成一坨。 */
  flex: 1 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  /* 上内边距留给指示器与基线：基线在 16px 处，三角在它上方 4px。用内边距而不是
     负偏移——横向滚动容器会把溢出的绝对定位元素裁掉。 */
  padding: var(--space-3) var(--space-3) 0;
}
/* 刻度短线从基线向下伸出 12px。 */
.tick {
  width: 2px;
  height: 12px;
  background: var(--color-state-pending);
}
.step--done .tick {
  background: var(--color-state-done);
}
/* 当前步是双线 + 钠黄（与 rail-contract 的「等待人工」同一语法）：
   两根 2px 平行、间 2px，用渐变一次画完，不必新增 DOM。 */
.step--current .tick {
  width: 6px;
  background: linear-gradient(
    to right,
    var(--color-signal) 0 2px,
    transparent 2px 4px,
    var(--color-signal) 4px 100%
  );
}
/* 当前指示器：2px 边框转 45° 的小三角，落在基线上方。占位常驻（visibility 保
   布局），只切可见性——否则标签会随状态左右跳一格。 */
.tri {
  position: absolute;
  top: var(--space-1);
  width: 6px;
  height: 6px;
  border-right: 2px solid var(--color-signal);
  border-bottom: 2px solid var(--color-signal);
  transform: rotate(45deg);
  visibility: hidden;
}
.step--current .tri {
  visibility: visible;
}
.step-label {
  /* 需要读出来的文字走 ink-soft：ink-faint 对画布只有 3.94:1，不到 AA。
     极暗的区分交给刻度本身（state-pending），文字不承担那个职责。 */
  color: var(--color-ink-soft);
  font-size: var(--text-body);
  line-height: 1.2;
  white-space: nowrap;
}
.step--done .step-label {
  color: var(--color-ink-soft);
}
.step--current .step-label {
  color: var(--color-ink);
  font-weight: 500;
}
.step-detail {
  color: var(--color-ink-soft);
  font-family: var(--font-data);
  font-size: var(--text-micro);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
  white-space: nowrap;
}
.step--current .step-detail {
  color: var(--color-ink);
}
.scroll-hint {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 12px;
  background: linear-gradient(to right, transparent, var(--color-canvas));
  pointer-events: none;
}
/* 位置 + 当前节点的一句（见 `line`）：原来分成两块各占一行，420 宽下太碎。 */
.current {
  margin: var(--space-3) 0 0;
  color: var(--color-ink-soft);
  font-size: var(--text-micro);
}
</style>
