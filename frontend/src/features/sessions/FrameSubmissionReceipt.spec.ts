import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ActionItemOut } from '@/api/client'

import FrameSubmissionReceipt from './FrameSubmissionReceipt.vue'

// 帧提交的尺寸回执（#368 / spec #363）：帧提交卡提交后立刻从界面消失，而尺寸提示写在
// 已决议待办的 `reason` 上——这里给它一个提交后仍可见的落点，与段卡片展示语义提示同形。
function makeItem(overrides: Partial<ActionItemOut> = {}): ActionItemOut {
  return {
    item_id: 'item-frame',
    kind: 'frame_submission',
    status: 'resolved',
    decision: 'submit_frames',
    created_at: '2026-11-11T00:00:00Z',
    resolved_at: '2026-11-11T00:00:30Z',
    reason: '',
    ...overrides,
  }
}

function mountReceipt(items: ActionItemOut[]) {
  return mount(FrameSubmissionReceipt, { props: { actionItems: items } })
}

describe('FrameSubmissionReceipt 尺寸回执', () => {
  it('已决议帧提交带回执时展示全文（尺寸不符的那一句原样呈现）', async () => {
    const wrapper = mountReceipt([
      makeItem({
        reason:
          '关键帧尺寸提示：首帧图 320×240 与当前画布 1920×1088 不一致——生成时会按画布尺寸出图…不阻断，首帧图照常收下。',
      }),
    ])
    await flushPromises()

    const receipt = wrapper.find('[data-frame-canvas-receipt]')
    expect(receipt.exists()).toBe(true)
    expect(receipt.text()).toContain('320×240')
    expect(receipt.text()).toContain('1920×1088')
  })

  it('尺寸相符（回执为空）时不渲染任何东西', async () => {
    const wrapper = mountReceipt([makeItem({ reason: '' })])

    expect(wrapper.find('[data-frame-canvas-receipt]').exists()).toBe(false)
  })

  it('读不出宽高时原样显示「量不出来」，不改写成尺寸不符', async () => {
    const wrapper = mountReceipt([
      makeItem({
        reason:
          '关键帧尺寸提示：读不出首帧图的宽高（文件损坏或不是图片），量不出来；首帧图照常收下。',
      }),
    ])

    const receipt = wrapper.find('[data-frame-canvas-receipt]')
    expect(receipt.exists()).toBe(true)
    expect(receipt.text()).toContain('量不出来')
    expect(receipt.text()).not.toContain('不一致')
  })

  it('只认已决议的 frame_submission：待决项与别的 kind 都不进回执', () => {
    const wrapper = mountReceipt([
      makeItem({ item_id: 'pending-1', status: 'pending', reason: '还没提交' }),
      makeItem({ item_id: 'other-1', kind: 'director_start_image', reason: '开场首帧比例不符' }),
    ])

    expect(wrapper.find('[data-frame-canvas-receipt]').exists()).toBe(false)
  })

  it('多次提交取最新一次的回执（旧回执不串台）', () => {
    const wrapper = mountReceipt([
      makeItem({
        item_id: 'older',
        resolved_at: '2026-11-11T00:00:30Z',
        reason: '关键帧尺寸提示：首帧图 320×240 与当前画布 640×1280 不一致。',
      }),
      makeItem({
        item_id: 'newer',
        resolved_at: '2026-11-11T00:05:30Z',
        reason: '关键帧尺寸提示：尾帧图 1280×720 与当前画布 640×1280 不一致。',
      }),
    ])

    const text = wrapper.find('[data-frame-canvas-receipt]').text()
    expect(text).toContain('1280×720')
    expect(text).not.toContain('320×240')
  })
})
