import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { SegmentOut } from '@/api/client'

import SegmentsTab from './SegmentsTab.vue'

// D6-1（#244）：分段分区只读。段号、时间区间、覆盖镜头、摘要、状态与「x / y」进度
// 都直接来自快照；没有编辑与确认入口。
function makeSegment(overrides: Partial<SegmentOut> = {}): SegmentOut {
  return {
    index: 1,
    start_s: 0,
    end_s: 6,
    shots_in_segment: [1, 2],
    summary: '开场：雨夜街口',
    end_hook: '人物停在旧楼门口',
    status: 'pending',
    prompt_artifact_id: '',
    video_asset_id: '',
    ...overrides,
  }
}

describe('SegmentsTab 分段分区（只读）', () => {
  it('逐段显示段号、时间区间、覆盖镜头、摘要与状态，并给出「已通过 / 总数」', () => {
    const wrapper = mount(SegmentsTab, {
      props: {
        segments: [
          makeSegment(),
          makeSegment({
            index: 2,
            start_s: 6,
            end_s: 12,
            shots_in_segment: [3],
            summary: '收束：抬头看向楼内',
            end_hook: '灯亮起',
            status: 'passed',
          }),
        ],
      },
    })

    const text = wrapper.text()
    expect(text).toContain('第 1 段')
    expect(text).toContain('0–6 秒')
    expect(text).toContain('镜头 1、镜头 2')
    expect(text).toContain('开场：雨夜街口')
    expect(text).toContain('待开始')
    expect(text).toContain('第 2 段')
    expect(text).toContain('6–12 秒')
    expect(text).toContain('已通过')
    expect(text).toContain('已通过 1 / 总数 2')

    // 只读：没有编辑/确认/合并按钮。
    expect(wrapper.findAll('button')).toHaveLength(0)
  })

  it('当前段高亮落在第一个未通过的段上；没有覆盖镜头时显示占位', () => {
    const wrapper = mount(SegmentsTab, {
      props: {
        segments: [
          makeSegment({ status: 'passed' }),
          makeSegment({ index: 2, start_s: 6, end_s: 12, shots_in_segment: [] }),
        ],
      },
    })

    expect(wrapper.find('[data-segment-index="1"]').classes()).not.toContain('segment--current')
    expect(wrapper.find('[data-segment-index="2"]').classes()).toContain('segment--current')
    expect(wrapper.find('[data-segment-index="2"]').text()).toContain('—')
  })

  it('空段表给出提示而不是空分区', () => {
    const wrapper = mount(SegmentsTab, { props: { segments: [] } })

    expect(wrapper.text()).toContain('暂无分段')
    expect(wrapper.find('ol').exists()).toBe(false)
  })
})
