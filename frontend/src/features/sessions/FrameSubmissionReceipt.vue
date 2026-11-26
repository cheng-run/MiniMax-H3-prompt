<script setup lang="ts">
import { computed } from 'vue'

import type { ActionItemOut } from '@/api/client'

// 帧提交的尺寸回执（#368 / spec #363）：把已决议 `frame_submission` 待办上的
// `reason` 摆出来。为什么需要这个组件：帧提交卡提交后立刻发 `submitted`、卡片从
// 界面消失，而尺寸提示写在那张卡自己的 `reason` 上——不另给落点，这句提示就等于
// 写进了没人读的地方。
//
// 与段卡片展示语义提示（`SegmentCard` 的 `data-semantic-warning`）同形：**通过后仍
// 留在页面上**，用户能慢慢看、自己判断要不要换一张图重交，而不是一闪而过。
//
// 判据与文案全部来自服务端：`reason` 就是回执本体（判据是 #365 的
// `segment_validation.frame_canvas_warning_text`），前端不重算尺寸、不改写口径。
// 尺寸相符时服务端给的是空串——那时一声不响，不制造噪声。

const props = defineProps<{ actionItems: ActionItemOut[] }>()

interface ReceiptView {
  /** 取回执那一次的待办 id：换一次提交就换一个落点，不串台。 */
  key: string
  text: string
}

// 多次提交取**最新一次**，而不是「最新一条有提示的」（#363 复审 B5）：先交了一张
// 不符的、后来换成交相符的，前者那条提示不该继续挂在页面上——那会违反「相符时
// 界面上没有这条提示」。`resolved_at` 比较口径是「人按下那一刻」，同刻取后出现的。
const receipt = computed<ReceiptView | null>(() => {
  let latest: ActionItemOut | null = null
  for (const item of props.actionItems) {
    if (item.kind !== 'frame_submission' || item.status !== 'resolved') {
      continue
    }
    if (latest === null || (item.resolved_at ?? '') >= (latest.resolved_at ?? '')) {
      latest = item
    }
  }
  if (latest === null) {
    return null
  }
  const text = (latest.reason ?? '').trim()
  // 最新那一次没有提示＝整块不渲染（不制造噪声）。
  return text ? { key: latest.item_id, text } : null
})
</script>

<template>
  <!-- 尺寸回执：整块 tint 浮起、底部一条全宽钠黄基线点名「这是待读的附言」，
       与段卡片的语义提示同一语法；不是闸门，所以不铺错误色。 -->
  <p
    v-if="receipt"
    :key="receipt.key"
    class="frame-receipt"
    data-frame-canvas-receipt
    role="status"
  >
    {{ receipt.text }}
  </p>
</template>

<style scoped>
.frame-receipt {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  background: var(--color-plate-raised);
  border-bottom: 1px solid var(--color-signal);
  white-space: pre-wrap;
}
</style>
