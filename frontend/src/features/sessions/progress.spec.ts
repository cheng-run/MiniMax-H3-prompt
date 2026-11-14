import { describe, expect, it } from 'vitest'

import { buildProgress, formatElapsed, type ProgressInput } from './progress'

const start = Date.UTC(2026, 10, 14, 2, 0, 0)

function actions(
  kind: string,
  status: 'pending' | 'resolved' = 'pending',
): ProgressInput['actionItems'] {
  return [
    {
      item_id: `item-${kind}`,
      kind,
      status,
      created_at: new Date(start).toISOString(),
      resolved_at: '',
      reason: '',
    },
  ]
}

function segments(passed: number, total: number): ProgressInput['segments'] {
  return Array.from({ length: total }, (_, index) => ({
    index: index + 1,
    start_s: index * 4,
    end_s: (index + 1) * 4,
    shots_in_segment: [],
    summary: '',
    end_hook: '',
    status: index < passed ? 'passed' : 'pending',
    prompt_artifact_id: '',
    video_asset_id: '',
    semantic_warning: '',
  }))
}

function input(overrides: Partial<ProgressInput> = {}): ProgressInput {
  return {
    route: 'base',
    variant: 'T2VA',
    duration: 5,
    status: 'awaiting_human',
    actionItems: [],
    artifacts: [],
    segments: [],
    progress: null,
    now: start,
    ...overrides,
  }
}

describe('buildProgress：base 短视频', () => {
  it('等待审查时只列五个步骤里的四个，审查是当前步骤', () => {
    const view = buildProgress(input({ actionItems: actions('stage1_review') }))

    expect(view.steps.map((step) => step.label)).toEqual(['阶段 1', '审查', '阶段 2', '完成'])
    expect(view.steps.map((step) => step.state)).toEqual(['done', 'current', 'pending', 'pending'])
  })

  it('短视频不出现分段规划、分段循环或拼接', () => {
    const view = buildProgress(input({ actionItems: actions('stage1_review') }))
    const labels = view.steps.map((step) => step.label)

    expect(labels).not.toContain('分段规划')
    expect(labels).not.toContain('分段循环')
    expect(labels).not.toContain('拼接')
  })

  it('帧链变体在审查与阶段 2 之间多一步帧提交', () => {
    const view = buildProgress(
      input({
        variant: 'FL2VA',
        actionItems: actions('frame_submission'),
        artifacts: [
          {
            artifact_id: 'sess-1/script.md',
            name: 'script.md',
            kind: 'script',
            bytes: 10,
            created_at: new Date(start).toISOString(),
          },
        ],
      }),
    )

    expect(view.steps.map((step) => step.label)).toEqual([
      '阶段 1',
      '审查',
      '帧提交',
      '阶段 2',
      '完成',
    ])
    expect(view.steps.find((step) => step.key === 'frame')?.state).toBe('current')
  })

  it('已完成时全部步骤都是 done，没有当前步骤，也不残留当前节点', () => {
    const view = buildProgress(
      input({
        status: 'completed',
        actionItems: actions('stage1_review', 'resolved'),
        artifacts: [
          {
            artifact_id: 'sess-1/video-prompt.md',
            name: 'video-prompt.md',
            kind: 'video_prompt',
            bytes: 10,
            created_at: new Date(start).toISOString(),
          },
        ],
        progress: {
          stage: 'stage2',
          node: 'finalize',
          status: 'done',
          startedAt: null,
          previousNode: 'prompt_engineer',
        },
      }),
    )

    expect(view.steps.every((step) => step.state === 'done')).toBe(true)
    expect(view.nodeLabel).toBeNull()
    expect(view.previousLabel).toBeNull()
    expect(view.elapsedMs).toBeNull()
  })
})

describe('buildProgress：base 长片', () => {
  it('15 段里只显示分段循环 x/y，不逐段展开', () => {
    const view = buildProgress(
      input({
        duration: 60,
        segments: segments(6, 15),
        actionItems: actions('segment_video'),
      }),
    )

    expect(view.steps.map((step) => step.label)).toEqual([
      '阶段 1',
      '审查',
      '阶段 2',
      '分段规划',
      '分段循环',
      '拼接',
      '完成',
    ])
    const loop = view.steps.find((step) => step.key === 'segment_loop')!
    expect(loop.state).toBe('current')
    expect(loop.detail).toBe('6/15')
    // 段号只出现在 detail，不生成 15 个步骤。
    expect(view.steps).toHaveLength(7)
  })

  it('长片在阶段 2 运行中：分段规划与分段循环都还不提前标记', () => {
    const view = buildProgress(
      input({
        duration: 18,
        status: 'running',
        actionItems: actions('stage1_review', 'resolved'),
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.stage2).toBe('current')
    expect(byKey.segment_plan).toBe('pending')
    expect(byKey.segment_loop).toBe('pending')
  })

  it('规划已落盘但还没交第一段：分段规划完成，分段循环是当前步骤', () => {
    const view = buildProgress(
      input({ duration: 18, segments: segments(0, 3), actionItems: actions('segment_video') }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.segment_plan).toBe('done')
    expect(byKey.segment_loop).toBe('current')
    expect(view.steps.find((step) => step.key === 'segment_loop')?.detail).toBe('0/3')
  })

  it('等待交段时流上残留的 done 进度不能把当前步骤钉在上一跳', () => {
    const view = buildProgress(
      input({
        duration: 18,
        segments: segments(0, 3),
        actionItems: actions('segment_video'),
        progress: {
          stage: 'segmenting',
          node: 'segment_plan',
          status: 'done',
          startedAt: null,
          previousNode: 'segment_plan',
        },
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.segment_plan).toBe('done')
    expect(byKey.segment_loop).toBe('current')
  })

  it('15 段全部通过后拼接是当前步骤', () => {
    const view = buildProgress(
      input({
        duration: 60,
        status: 'running',
        segments: segments(15, 15),
        progress: {
          stage: 'segmenting',
          node: 'segment_assemble',
          status: 'started',
          startedAt: start,
          previousNode: 'segment_validate-15',
        },
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.segment_loop).toBe('done')
    expect(byKey.assemble).toBe('current')
  })

  it('失败会话保留最后状态：已通过的段仍计入 x/y，当前步骤不再前进', () => {
    const view = buildProgress(input({ duration: 60, status: 'failed', segments: segments(3, 15) }))

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.segment_plan).toBe('done')
    expect(byKey.segment_loop).toBe('current')
    expect(byKey.assemble).toBe('pending')
    expect(view.steps.find((step) => step.key === 'segment_loop')?.detail).toBe('3/15')
  })
})

describe('buildProgress：当前节点、上一步与已耗时', () => {
  it('节点名翻成中文，上一步来自最近一条 done，已耗时从 started 事件起算', () => {
    const view = buildProgress(
      input({
        status: 'running',
        progress: {
          stage: 'stage1',
          node: 'producer',
          status: 'started',
          startedAt: start,
          previousNode: 'storyboard',
        },
        now: start + 12_000,
      }),
    )

    expect(view.nodeLabel).toBe('制片')
    expect(view.previousLabel).toBe('分镜')
    expect(view.elapsedMs).toBe(12_000)
    expect(view.steps.find((step) => step.key === 'stage1')?.state).toBe('current')
  })

  it('带段号的节点翻成中文并保留段号', () => {
    const view = buildProgress(
      input({
        duration: 60,
        status: 'running',
        segments: segments(0, 3),
        progress: {
          stage: 'segmenting',
          node: 'segment_write-02',
          status: 'started',
          startedAt: start,
          previousNode: 'segment_validate-01',
        },
        now: start + 3_000,
      }),
    )

    expect(view.nodeLabel).toBe('第 2 段写词')
    expect(view.previousLabel).toBe('第 1 段校验')
  })

  it('未知节点回退显示原名，不隐藏', () => {
    const view = buildProgress(
      input({
        status: 'running',
        progress: {
          stage: 'stage1',
          node: 'mystery_node',
          status: 'started',
          startedAt: start,
          previousNode: 'also_unknown',
        },
        now: start + 1_000,
      }),
    )

    expect(view.nodeLabel).toBe('mystery_node')
    expect(view.previousLabel).toBe('also_unknown')
  })

  it('已耗时只在节点 running 且有 started 时间时给出', () => {
    const view = buildProgress(
      input({
        status: 'awaiting_human',
        actionItems: actions('stage1_review'),
        progress: {
          stage: 'stage1',
          node: 'producer',
          status: 'done',
          startedAt: start,
          previousNode: null,
        },
        now: start + 5_000,
      }),
    )

    expect(view.elapsedMs).toBeNull()
  })
})

describe('formatElapsed', () => {
  it('秒级与分钟级都给人读的写法', () => {
    expect(formatElapsed(0)).toBe('0 秒')
    expect(formatElapsed(12_400)).toBe('12 秒')
    expect(formatElapsed(65_000)).toBe('1 分 05 秒')
  })
})

describe('buildProgress：路线边界', () => {
  it('本票只做 base；ref 与导演台先返回空步骤（#273 补齐）', () => {
    expect(buildProgress(input({ route: 'ref', duration: 8 })).steps).toEqual([])
    expect(buildProgress(input({ route: 'director' })).steps).toEqual([])
  })
})
