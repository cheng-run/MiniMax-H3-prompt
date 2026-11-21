import { describe, expect, it } from 'vitest'

import {
  buildProgress,
  formatElapsed,
  pendingAction,
  pendingCountsByTarget,
  type ProgressInput,
} from './progress'

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

/** 素材循环的待办：`asset_index` 是这一件的件号（1 起）。 */
function assetAction(
  index: number,
  status: 'pending' | 'resolved' = 'pending',
): ProgressInput['actionItems'][number] {
  return {
    item_id: `item-asset-${index}-${status}`,
    kind: 'asset_image',
    status,
    created_at: new Date(start).toISOString(),
    resolved_at: status === 'resolved' ? new Date(start).toISOString() : '',
    reason: '',
    asset_index: index,
  }
}

/** ref 台账摘要：只有 Picture 会进素材循环，视频/音频各占一行但不进循环。 */
function refItems(pictures: number, kind: string = 'picture'): ProgressInput['refs'] {
  return Array.from({ length: pictures }, (_, index) => ({
    asset_id: `sess-1/asset-${index + 1}`,
    kind,
    name: `素材${index + 1}`,
    number: index + 1,
    status: 'passed',
    source: 'derived',
  }))
}

function artifact(kind: string, name: string): ProgressInput['artifacts'][number] {
  return {
    artifact_id: `sess-1/${name}`,
    name,
    kind,
    bytes: 10,
    created_at: new Date(start).toISOString(),
  }
}

function input(overrides: Partial<ProgressInput> = {}): ProgressInput {
  return {
    route: 'base',
    variant: 'T2VA',
    duration: 5,
    directorMode: '',
    status: 'awaiting_human',
    actionItems: [],
    artifacts: [],
    segments: [],
    refs: [],
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

describe('buildProgress：帧链变体集合', () => {
  it('I2VA / L2VA 与 FL2VA 一样列帧提交（契约允许这三档）', () => {
    for (const variant of ['FL2VA', 'I2VA', 'L2VA']) {
      const view = buildProgress(input({ variant, actionItems: actions('frame_submission') }))
      expect(
        view.steps.map((step) => step.label),
        variant,
      ).toContain('帧提交')
    }
  })

  it('T2VA 不列帧提交', () => {
    const view = buildProgress(input({ variant: 'T2VA', actionItems: actions('stage1_review') }))
    expect(view.steps.map((step) => step.label)).not.toContain('帧提交')
  })
})

describe('buildProgress：ref 路线', () => {
  it('等待审查：只列 ref 路线步骤，素材循环显示 0/n', () => {
    const view = buildProgress(
      input({ route: 'ref', refs: refItems(2), actionItems: actions('stage1_review') }),
    )

    expect(view.steps.map((step) => step.label)).toEqual([
      '阶段 1',
      '审查',
      '素材循环',
      '素材补交',
      '阶段 2',
      '分段循环',
      '完成',
    ])
    expect(view.steps.map((step) => step.state)).toEqual([
      'done',
      'current',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
    ])
    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('0/2')
    // ref 没有帧提交、不是 base 的分段规划。
    expect(view.steps.map((step) => step.label)).not.toContain('帧提交')
    expect(view.steps.map((step) => step.label)).not.toContain('分段规划')
  })

  it('等待交第 2 件素材：素材循环是当前步骤，已完成 1/2', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        refs: refItems(2),
        actionItems: [assetAction(1, 'resolved'), assetAction(2, 'pending')],
        artifacts: [artifact('asset_prompt', 'assets/picture-01.md')],
      }),
    )

    expect(view.steps.find((step) => step.key === 'asset_loop')?.state).toBe('current')
    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('1/2')
  })

  it('等待交第 1 件素材：还没完成任何一件，0/2', () => {
    const view = buildProgress(
      input({ route: 'ref', refs: refItems(2), actionItems: [assetAction(1, 'pending')] }),
    )

    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('0/2')
  })

  it('素材循环校对进行中：当前件已交但未定稿，不把它算作已通过', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        status: 'running',
        refs: refItems(2),
        actionItems: [assetAction(1, 'resolved')],
        progress: {
          stage: 'asset_loop',
          node: 'asset_proofread',
          status: 'started',
          startedAt: start,
          previousNode: 'asset_write',
        },
      }),
    )

    expect(view.steps.find((step) => step.key === 'asset_loop')?.state).toBe('current')
    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('0/2')
  })

  it('素材循环已收尾进入阶段 2：素材循环 2/2 完成，阶段 2 是当前步骤', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        status: 'running',
        refs: refItems(2),
        actionItems: [assetAction(1, 'resolved'), assetAction(2, 'resolved')],
        artifacts: [artifact('video_prompt', 'video-prompt.md')],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.asset_loop).toBe('done')
    expect(byKey.stage2).toBe('current')
    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('2/2')
  })

  it('循环走完先落在补交档：补交待办 pending 时当前步骤是素材补交', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        refs: refItems(2),
        actionItems: [
          assetAction(1, 'resolved'),
          assetAction(2, 'resolved'),
          ...actions('asset_extra'),
        ],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.asset_loop).toBe('done')
    expect(byKey.asset_extra).toBe('current')
    expect(byKey.stage2).toBe('pending')
  })

  it('补交落定之后不再退回素材循环：下一步是阶段 2', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        refs: refItems(2),
        actionItems: [
          assetAction(1, 'resolved'),
          assetAction(2, 'resolved'),
          ...actions('asset_extra', 'resolved'),
        ],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.asset_extra).toBe('done')
    expect(byKey.stage2).toBe('current')
  })

  it('只有视频/音频参考素材时素材循环的 n 只算 Picture', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        refs: [...refItems(1, 'picture'), ...refItems(1, 'video'), ...refItems(1, 'audio')],
        actionItems: [assetAction(1, 'pending')],
      }),
    )

    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('0/1')
  })

  it('段循环：已通过 1/2，分段循环是当前步骤', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        refs: refItems(2),
        segments: segments(1, 2),
        actionItems: [
          assetAction(1, 'resolved'),
          assetAction(2, 'resolved'),
          ...actions('segment_video'),
        ],
        artifacts: [artifact('video_prompt', 'video-prompt.md')],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.asset_loop).toBe('done')
    expect(byKey.stage2).toBe('done')
    expect(byKey.segment_loop).toBe('current')
    expect(view.steps.find((step) => step.key === 'segment_loop')?.detail).toBe('1/2')
  })

  it('已完成时全部步骤都是 done', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        status: 'completed',
        refs: refItems(2),
        segments: segments(2, 2),
        actionItems: [assetAction(1, 'resolved'), assetAction(2, 'resolved')],
        artifacts: [artifact('video', 'final.mp4')],
      }),
    )

    expect(view.steps.every((step) => step.state === 'done')).toBe(true)
    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('2/2')
    expect(view.steps.find((step) => step.key === 'segment_loop')?.detail).toBe('2/2')
    expect(view.nodeLabel).toBeNull()
  })

  it('失败会话保留最后到达的步骤，不把未完成的步骤标成 done', () => {
    const view = buildProgress(
      input({
        route: 'ref',
        status: 'failed',
        refs: refItems(2),
        actionItems: [assetAction(1, 'resolved')],
        artifacts: [artifact('asset_prompt', 'assets/picture-01.md')],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.asset_loop).toBe('current')
    expect(byKey.stage2).toBe('pending')
    expect(view.steps.find((step) => step.key === 'asset_loop')?.detail).toBe('1/2')
  })
})

describe('buildProgress：导演台路线', () => {
  it('fl2v 等待开场帧：列出开场帧且它是当前步骤', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'fl2v',
        actionItems: actions('director_start_image'),
        artifacts: [artifact('script', 'script.md')],
      }),
    )

    expect(view.steps.map((step) => step.label)).toEqual([
      '阶段 1',
      '审查',
      '开场帧',
      '阶段 2',
      '打包',
      '段循环',
      '交付',
      '完成',
    ])
    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.stage1).toBe('done')
    expect(byKey.review).toBe('done')
    expect(byKey.start_frame).toBe('current')
  })

  it('t2v 不列开场帧（只列本路线实际步骤）', () => {
    const view = buildProgress(
      input({ route: 'director', directorMode: 't2v', actionItems: actions('stage1_review') }),
    )

    expect(view.steps.map((step) => step.label)).not.toContain('开场帧')
    expect(view.steps.map((step) => step.label)).toEqual([
      '阶段 1',
      '审查',
      '阶段 2',
      '打包',
      '段循环',
      '交付',
      '完成',
    ])
  })

  it('t2v 但快照里仍有开场帧待办时把它列出来', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 't2v',
        actionItems: [
          actions('director_start_image')[0]!,
          {
            ...actions('stage1_review', 'resolved')[0]!,
            item_id: 'item-review',
          },
        ],
      }),
    )

    expect(view.steps.map((step) => step.label)).toContain('开场帧')
  })

  it('打包中：打包是当前步骤', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'fl2v',
        status: 'running',
        actionItems: [
          { ...actions('stage1_review', 'resolved')[0]!, item_id: 'item-review' },
          { ...actions('director_start_image', 'resolved')[0]!, item_id: 'item-start' },
        ],
        progress: {
          stage: 'packing',
          node: 'director_pack',
          status: 'started',
          startedAt: start,
          previousNode: 'director_segment_write',
        },
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.stage2).toBe('done')
    expect(byKey.pack).toBe('current')
    expect(byKey.segment_loop).toBe('pending')
  })

  it('段循环：已通过 2/3，段循环是当前步骤并显示 x/y', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'fl2v',
        segments: segments(2, 3),
        actionItems: [{ ...actions('segment_video')[0]!, item_id: 'item-seg' }],
        artifacts: [artifact('director_pack', 'director/initial.mmxpack.zip')],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.pack).toBe('done')
    expect(byKey.segment_loop).toBe('current')
    expect(byKey.delivery).toBe('pending')
    expect(view.steps.find((step) => step.key === 'segment_loop')?.detail).toBe('2/3')
  })

  it('全部段通过后等待交付：交付是当前步骤', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'fl2v',
        segments: segments(3, 3),
        actionItems: [{ ...actions('director_delivery')[0]!, item_id: 'item-delivery' }],
        artifacts: [artifact('director_pack', 'director/initial.mmxpack.zip')],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.segment_loop).toBe('done')
    expect(byKey.delivery).toBe('current')
  })

  it('交付归档完成：会话已完成，全部步骤 done', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'fl2v',
        status: 'completed',
        segments: segments(3, 3),
        actionItems: [
          { ...actions('director_delivery', 'resolved')[0]!, item_id: 'item-delivery' },
        ],
        artifacts: [
          artifact('director_pack', 'director/initial.mmxpack.zip'),
          artifact('director_final_pack', 'director/final.mmxpack.zip'),
          artifact('video', 'final.mp4'),
        ],
      }),
    )

    expect(view.steps.every((step) => step.state === 'done')).toBe(true)
    expect(view.steps.find((step) => step.key === 'segment_loop')?.detail).toBe('3/3')
  })

  it('等待审查时开场帧还排在待开始', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'i2v',
        actionItems: actions('stage1_review'),
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.stage1).toBe('done')
    expect(byKey.review).toBe('current')
    expect(byKey.start_frame).toBe('pending')
  })

  it('失败会话保留最后状态', () => {
    const view = buildProgress(
      input({
        route: 'director',
        directorMode: 'fl2v',
        status: 'failed',
        segments: segments(1, 3),
        artifacts: [artifact('director_pack', 'director/initial.mmxpack.zip')],
      }),
    )

    const byKey = Object.fromEntries(view.steps.map((step) => [step.key, step.state]))
    expect(byKey.pack).toBe('done')
    expect(byKey.segment_loop).toBe('current')
    expect(byKey.delivery).toBe('pending')
  })
})

// #276 / 2026-11-21 layout：「等待你操作」那一行的中文说明与入口分区——按 kind
// 翻译，段号/件号取待办自带索引；入口必须与卡片实际所在分区一致。
describe('pendingAction', () => {
  it('按 kind 给出说清「哪一段、哪一步」的中文说明', () => {
    expect(pendingAction(actions('stage1_review'), [])?.text).toBe('审查阶段 1 结果')
    expect(pendingAction(actions('frame_submission'), [])?.text).toBe('提交首帧/尾帧素材')
    expect(pendingAction(actions('director_start_image'), [])?.text).toBe('提交开场首帧')
    expect(pendingAction(actions('director_delivery'), [])?.text).toBe('交付成片与最终包')
    // 补交口（#284）：循环走完之后的阻塞决定点，不能退化成通用文案。
    expect(pendingAction(actions('asset_extra'), [])?.text).toBe('确认要不要补交素材视频/音频')
  })

  it('段号 / 件号进说明；素材语义名从 refs 台账取', () => {
    const seg = [{ ...actions('segment_video')[0]!, segment_index: 3 }]
    expect(pendingAction(seg, [])?.text).toBe('提交第 3 段视频')

    const segImg = [{ ...actions('director_segment_image')[0]!, segment_index: 2 }]
    expect(pendingAction(segImg, [])?.text).toBe('提交第 2 段首帧图')

    const asset = assetAction(2)
    const refs = [
      {
        asset_id: 'sess-1/a2',
        kind: 'picture',
        name: '台灯',
        number: 2,
        status: 'passed',
        source: 'derived',
      },
    ]
    expect(pendingAction([asset], refs)?.text).toBe('交回第 2 件素材图 · 台灯')
    expect(pendingAction([asset], [])?.text).toBe('交回第 2 件素材图')
  })

  it('入口分区与卡片实际所在的那一格一致', () => {
    // 段视频表单挂在「分段」；交付卡挂在「交付」；其余卡片都挂在「审查与待办」。
    expect(pendingAction(actions('segment_video'), [])?.target).toBe('segments')
    expect(pendingAction(actions('director_delivery'), [])?.target).toBe('delivery')
    for (const kind of [
      'stage1_review',
      'frame_submission',
      'asset_image',
      'asset_extra',
      'director_start_image',
      'director_segment_image',
    ]) {
      expect(pendingAction(actions(kind), [])?.target).toBe('review')
    }
  })

  it('没有待处理的人工待办时为 null（已决议的不算）', () => {
    expect(pendingAction([], [])).toBeNull()
    expect(pendingAction(actions('stage1_review', 'resolved'), [])).toBeNull()
  })

  it('未知 kind 的 pending 待办回退一句通用说明，不静默丢提示', () => {
    // 后端先加了一种前端这版还不认识的待办：整行「等待你操作」不能消失，
    // 否则会话卡在一个没有入口的待办上，用户却以为无事可做。
    // 入口为 null：前端还没有那类卡片，指过去只会是个空分区。
    expect(pendingAction(actions('future_review'), [])).toEqual({
      text: '有新的待办需要处理',
      target: null,
    })
  })

  it('同时存在已知与未知 pending 时，优先已知 kind 的精确文案', () => {
    const items = [...actions('future_review'), ...actions('stage1_review')]
    expect(pendingAction(items, [])?.text).toBe('审查阶段 1 结果')
  })
})

describe('pendingCountsByTarget', () => {
  it('按入口分区给分区 tab 计数：只数 pending，未知 kind 不计入任何一格', () => {
    const items = [
      ...actions('stage1_review'),
      ...actions('asset_image'),
      ...actions('segment_video'),
      ...actions('director_delivery'),
      ...actions('future_review'),
    ]
    expect(pendingCountsByTarget(items)).toEqual({ review: 2, segments: 1, delivery: 1 })
  })

  it('已决议的不计入；没有待办时为空对象', () => {
    expect(pendingCountsByTarget(actions('stage1_review', 'resolved'))).toEqual({})
    expect(pendingCountsByTarget([])).toEqual({})
  })
})
