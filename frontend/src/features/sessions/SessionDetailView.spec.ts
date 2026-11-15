import { enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken } from '@/api/client'
import { mountWithPlugins } from '@/test/mount'
import { makeDetail, makeJob } from '@/test/sessionFixtures'
import { server } from '@/test/server'

import SessionDetailView from './SessionDetailView.vue'
import { SESSION_STREAM_FETCH } from './useSessionStream'

const detailUrl = 'http://localhost/api/v1/sessions/sess-1'
const jobsUrl = 'http://localhost/api/v1/sessions/sess-1/jobs'
const cancelUrl = 'http://localhost/api/v1/sessions/sess-1/jobs/job-1/cancel'

// 用例结束统一卸载：订阅器随卸载 stop，否则 500ms 的合并窗口计时器会跨用例触发重拉。
enableAutoUnmount(afterEach)

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

interface StreamHandle {
  fetchImpl: typeof fetch
  push(text: string): void
  close(): void
}

// 可控事件流：工作台只在生产代码里用全局 fetch，测试经 provide 注入这一份。
function makeStreamFetch(): StreamHandle {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  const fetchImpl = (async () =>
    new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    })) as typeof fetch
  return {
    fetchImpl,
    push(text: string) {
      controller.enqueue(new TextEncoder().encode(text))
    },
    close() {
      controller.close()
    },
  }
}

async function mountDetail(stream: StreamHandle) {
  const mounted = mountWithPlugins(SessionDetailView, {
    withRouter: true,
    provide: { [SESSION_STREAM_FETCH]: stream.fetchImpl },
  })
  await mounted.router!.push('/sessions/sess-1')
  await mounted.router!.isReady()
  await settle()
  return mounted
}

beforeEach(() => resetSessionToken())

describe('SessionDetailView 四分区（页内 tab）', () => {
  it('四个分区默认概览，切换不改变 URL，刷新（重挂载）回概览', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            action_items: [
              {
                item_id: 'item-1',
                kind: 'stage1_review',
                status: 'pending',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '',
                reason: '',
              },
            ],
            artifacts: [
              {
                artifact_id: 'sess-1/script.md',
                name: 'script.md',
                kind: 'script',
                bytes: 42,
                created_at: '2026-11-11T00:00:00Z',
              },
            ],
            assets: [
              {
                asset_id: 'sess-1/abc123',
                name: 'photo.png',
                kind: 'image',
                bytes: 1024,
                created_at: '2026-11-11T00:00:00Z',
              },
            ],
          }),
        ),
      ),
      // 审查分区按文件读正文（content 端点）；产物标识带斜杠，用正则承接。
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () => HttpResponse.text('雨夜旧信·剧本')),
    )
    const stream = makeStreamFetch()
    const { wrapper, router } = await mountDetail(stream)

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((tab) => tab.text())).toEqual(['概览与进度', '审查与待办', '素材', '产物'])
    expect(tabs[0].attributes('aria-selected')).toBe('true')
    expect(wrapper.text()).toContain('剧情')

    await tabs[1].trigger('click')
    await settle()
    expect(wrapper.find('[role="tabpanel"]').text()).toContain('雨夜旧信·剧本')
    expect(wrapper.findAll('button').some((b) => b.text() === '通过')).toBe(true)
    expect(wrapper.find('#workbench-panel-overview').exists()).toBe(false)

    await tabs[2].trigger('click')
    expect(wrapper.find('[role="tabpanel"]').text()).toContain('photo.png')

    await tabs[3].trigger('click')
    expect(wrapper.find('[role="tabpanel"]').text()).toContain('script.md')

    // 分区是页内状态：切了三个分区，URL 仍是原始深链。
    expect(router!.currentRoute.value.fullPath).toBe('/sessions/sess-1')

    // 刷新 = 新的 Pinia，分区选择不落盘，回到概览。
    const again = await mountDetail(makeStreamFetch())
    expect(again.wrapper.findAll('[role="tab"]')[0].attributes('aria-selected')).toBe('true')
  })

  it('切换会话：分区回概览，按新会话重拉快照', async () => {
    server.use(
      http.get(detailUrl, () => HttpResponse.json(makeDetail({ plot: '第一个会话' }))),
      http.get('http://localhost/api/v1/sessions/sess-2', () =>
        HttpResponse.json(makeDetail({ session_id: 'sess-2', plot: '第二个会话' })),
      ),
    )
    const { wrapper, router } = await mountDetail(makeStreamFetch())

    await wrapper.findAll('[role="tab"]')[1]!.trigger('click')
    expect(wrapper.findAll('[role="tab"]')[1]!.attributes('aria-selected')).toBe('true')

    await router!.push('/sessions/sess-2')
    await settle()

    expect(wrapper.find('h1').text()).toContain('第二个会话')
    expect(wrapper.findAll('[role="tab"]')[0]!.attributes('aria-selected')).toBe('true')
  })
})

describe('SessionDetailView 运行控制（spec §6.3）', () => {
  it('运行中显示 job.progress 的当前节点；取消是协作式并重拉快照', async () => {
    let detail = makeDetail({ status: 'running', current_job: makeJob() })
    let cancels = 0
    server.use(
      http.get(detailUrl, () => HttpResponse.json(detail)),
      http.post(cancelUrl, () => {
        cancels += 1
        detail = makeDetail({
          status: 'running',
          current_job: makeJob({ cancel_requested: true }),
        })
        return HttpResponse.json(makeJob({ cancel_requested: true }), { status: 202 })
      }),
    )
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)

    stream.push(
      `id: 4\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 4,
        type: 'job.progress',
        at: new Date().toISOString(),
        session_id: 'sess-1',
        payload: { stage: 'stage1', node: 'storyboard', status: 'started' },
      })}\n\n`,
    )
    await settle()
    // 「正在执行」行显示中文节点名（#272），不再直接给内部名。
    expect(wrapper.text()).toContain('阶段 1 · 分镜')
    expect(wrapper.text()).not.toContain('storyboard')

    const cancel = wrapper.findAll('button').find((b) => b.text() === '取消')!
    expect(cancel).toBeTruthy()
    await cancel.trigger('click')
    await settle()

    expect(cancels).toBe(1)
    // 文案不许承诺立即停止：取消是节点边界生效（ADR 0032）。
    expect(wrapper.text()).toContain('当前步骤结束后停止')
    expect(wrapper.findAll('button').some((b) => b.text() === '取消')).toBe(false)
  })

  it('刷新/重连后仍显示当前阶段与节点：服务端补发的旧进度也生效（#271）', async () => {
    // 刷新时快照序号 4 就是订阅游标；游标之后没有事件，节点只能来自服务端补发的
    // 那条 seq <= 4 的 job.progress。
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({ status: 'running', current_job: makeJob(), last_event_seq: 4 }),
        ),
      ),
    )
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)

    expect(wrapper.text()).toContain('等待进度…')

    stream.push(
      `id: 3\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 3,
        type: 'job.progress',
        at: new Date().toISOString(),
        session_id: 'sess-1',
        payload: { stage: 'stage1', node: 'storyboard', status: 'started' },
      })}\n\n`,
    )
    await settle()
    expect(wrapper.text()).toContain('阶段 1 · 分镜')
    expect(wrapper.text()).not.toContain('等待进度…')
  })

  it('失败/取消/中断的会话显示「重试」，点击即再 POST /jobs 并回到运行态', async () => {
    let detail = makeDetail({ status: 'failed' })
    let starts = 0
    server.use(
      http.get(detailUrl, () => HttpResponse.json(detail)),
      http.post(jobsUrl, () => {
        starts += 1
        detail = makeDetail({ status: 'running', current_job: makeJob() })
        return HttpResponse.json(makeJob(), { status: 202 })
      }),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')!
    expect(retry).toBeTruthy()
    await retry.trigger('click')
    await settle()

    expect(starts).toBe(1)
    expect(wrapper.text()).toContain('运行中')
  })

  it('已创建显示「开始」兜底；完成/等待人工没有运行按钮', async () => {
    server.use(http.get(detailUrl, () => HttpResponse.json(makeDetail({ status: 'created' }))))
    const created = await mountDetail(makeStreamFetch())
    expect(created.wrapper.findAll('button').some((b) => b.text() === '开始')).toBe(true)

    const states = ['completed', 'awaiting_human'] as const
    for (const status of states) {
      server.use(http.get(detailUrl, () => HttpResponse.json(makeDetail({ status }))))
      const { wrapper } = await mountDetail(makeStreamFetch())
      for (const label of ['开始', '重试', '取消']) {
        expect(wrapper.findAll('button').some((b) => b.text() === label)).toBe(false)
      }
    }
  })

  it('实时连接断开时给出可见提示（状态不靠事件投影，重连由订阅器自行完成）', async () => {
    server.use(http.get(detailUrl, () => HttpResponse.json(makeDetail({ status: 'running' }))))
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)
    stream.close()
    await settle()

    expect(wrapper.text()).toContain('正在重连')
  })
})

describe('SessionDetailView 深链快照', () => {
  it('直接打开深链（刷新恢复）先 GET 详情并呈现 brief/状态', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(makeDetail({ status: 'awaiting_human', plot: '雨夜旧信' })),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    expect(wrapper.text()).toContain('雨夜旧信')
    expect(wrapper.text()).toContain('等待人工')
    expect(wrapper.text()).toContain('5') // brief.duration
  })

  it('加载失败显示 role=alert 并可重试', async () => {
    let calls = 0
    server.use(
      http.get(detailUrl, () => {
        calls += 1
        if (calls === 1) {
          return HttpResponse.json(
            {
              type: 'about:blank',
              title: '资源不存在',
              status: 404,
              detail: 'nope',
              instance: detailUrl,
            },
            { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json(makeDetail({ plot: '恢复' }))
      }),
    )

    const { wrapper } = await mountDetail(makeStreamFetch())

    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    const retry = wrapper.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await settle()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('恢复')
  })
})

describe('SessionDetailView 审查与修订回路（#232）', () => {
  it('提交意见后重拉快照：新待办回到同一闸门，历史累积且产物刷新', async () => {
    const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-1/resolve'
    let content = '# 分场剧本（第 1 稿）'
    const detailFor = (actionItems: NonNullable<ReturnType<typeof makeDetail>['action_items']>) =>
      makeDetail({
        status: 'awaiting_human',
        action_items: actionItems,
        artifacts: [
          {
            artifact_id: 'sess-1/script.md',
            name: 'script.md',
            kind: 'script',
            bytes: 20,
            created_at: '2026-11-11T00:00:00Z',
          },
        ],
      })
    let detail = detailFor([
      {
        item_id: 'item-1',
        kind: 'stage1_review',
        status: 'pending',
        created_at: '2026-11-11T00:00:00Z',
        resolved_at: '',
        reason: '',
      },
    ])
    const bodies: unknown[] = []
    server.use(
      http.get(detailUrl, () => HttpResponse.json(detail)),
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () => HttpResponse.text(content)),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        // 重跑完成（这里直接给最终快照）：旧待办已决议并带修订，新待办的 item_id 不同。
        content = '# 分场剧本（第 2 稿）'
        detail = detailFor([
          {
            item_id: 'item-1',
            kind: 'stage1_review',
            status: 'resolved',
            decision: 'revise',
            created_at: '2026-11-11T00:00:00Z',
            resolved_at: '2026-11-11T00:00:30Z',
            reason: '',
            revisions: [{ layer: 'character', text: '服装改成米色风衣' }],
          },
          {
            item_id: 'item-2',
            kind: 'stage1_review',
            status: 'pending',
            created_at: '2026-11-11T00:01:00Z',
            resolved_at: '',
            reason: '',
          },
        ])
        return HttpResponse.json(null, { status: 202 })
      }),
    )

    const { wrapper } = await mountDetail(makeStreamFetch())
    await wrapper.findAll('[role="tab"]')[1]!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('第 1 稿')

    await wrapper.find('select[name="revision-layer"]').setValue('人物设定')
    await wrapper.find('textarea[name="revision-text"]').setValue('服装改成米色风衣')
    await wrapper.find('form').trigger('submit')
    await settle()
    await settle()

    expect(bodies).toHaveLength(1)
    // 新待办回同一闸门（仍可「通过」），历史里能看到刚提的那条，产物已是新一稿。
    expect(wrapper.findAll('button').some((b) => b.text() === '通过')).toBe(true)
    expect(wrapper.text()).toContain('修订历史')
    expect(wrapper.text()).toContain('人物设定')
    expect(wrapper.text()).toContain('服装改成米色风衣')
    expect(wrapper.text()).toContain('第 2 稿')
    expect(wrapper.text()).not.toContain('第 1 稿')
  })
})

// D6-1（#244）：长片多一个只读「分段」分区；短视频的四分区不变。
describe('SessionDetailView 分段分区', () => {
  // 分段分区的出现判据是会话时长（>10 秒），不是「段列表非空」。
  const longBrief = { ...makeDetail().brief, duration: 18 }
  const segments = [
    {
      index: 1,
      start_s: 0,
      end_s: 6,
      shots_in_segment: [1, 2],
      summary: '开场：雨夜街口',
      end_hook: '人物停在旧楼门口',
      status: 'pending',
      prompt_artifact_id: '',
      video_asset_id: '',
      semantic_warning: '',
    },
    {
      index: 2,
      start_s: 6,
      end_s: 12,
      shots_in_segment: [3],
      summary: '收束：抬头看向楼内',
      end_hook: '灯亮起',
      status: 'pending',
      prompt_artifact_id: '',
      video_asset_id: '',
      semantic_warning: '',
    },
  ]

  it('有段列表时出现「分段」，展示只读段表与「已通过 / 总数」', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(makeDetail({ status: 'awaiting_human', brief: longBrief, segments })),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((tab) => tab.text())).toEqual([
      '概览与进度',
      '审查与待办',
      '分段',
      '素材',
      '产物',
    ])

    await tabs[2]!.trigger('click')
    await settle()

    const panel = wrapper.find('#workbench-panel-segments')
    expect(panel.text()).toContain('第 1 段')
    expect(panel.text()).toContain('0–6 秒')
    expect(panel.text()).toContain('镜头 1、镜头 2')
    expect(panel.text()).toContain('开场：雨夜街口')
    expect(panel.text()).toContain('已通过 0 / 总数 2')
    // 只读：分区里没有编辑/确认/合并入口。
    expect(panel.findAll('button')).toHaveLength(0)
  })

  it('段视频提交后由快照失效通道重拉详情，无需手动刷新', async () => {
    let detailCalls = 0
    const waitingSegments = [{ ...segments[0]!, status: 'waiting_video' }, { ...segments[1]! }]
    const asset = {
      asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
      name: 'shot-01.mp4',
      kind: 'video',
      bytes: 1024,
      created_at: '2026-11-11T00:00:00Z',
    }
    const item = {
      item_id: 'item-seg-1',
      kind: 'segment_video',
      status: 'pending',
      created_at: '2026-11-11T00:00:00Z',
      resolved_at: '',
      reason: '',
      segment_index: 1,
    }
    server.use(
      http.get(detailUrl, () => {
        detailCalls += 1
        return HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            brief: longBrief,
            segments: waitingSegments,
            assets: [asset],
            action_items: [item],
          }),
        )
      }),
      http.post('http://localhost/api/v1/sessions/sess-1/action-items/item-seg-1/resolve', () =>
        HttpResponse.json(null, { status: 202 }),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())
    expect(detailCalls).toBe(1)

    const tabs = wrapper.findAll('[role="tab"]')
    await tabs[2]!.trigger('click')
    await settle()

    const select = wrapper.find('#workbench-panel-segments select[name="segment-video"]')
    expect(select.exists()).toBe(true)
    await select.setValue(asset.asset_id)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交视频')!
      .trigger('click')
    await settle()

    // 提交后父组件的失效通道立刻重拉详情；SSE 的事件失效走的是同一个口。
    expect(detailCalls).toBe(2)
  })

  it('长片但分段规划还没完成时，分区也在并显示「规划中」', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(makeDetail({ status: 'running', brief: longBrief })),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((tab) => tab.text())).toEqual([
      '概览与进度',
      '审查与待办',
      '分段',
      '素材',
      '产物',
    ])

    await tabs[2]!.trigger('click')
    await settle()

    const panel = wrapper.find('#workbench-panel-segments')
    expect(panel.text()).toContain('规划中')
    expect(panel.text()).toContain('已通过 0 / 总数 0')
  })

  it('没有段列表（短视频）时不出现这一格', async () => {
    server.use(http.get(detailUrl, () => HttpResponse.json(makeDetail({ status: 'completed' }))))
    const { wrapper } = await mountDetail(makeStreamFetch())

    expect(wrapper.findAll('[role="tab"]').map((tab) => tab.text())).toEqual([
      '概览与进度',
      '审查与待办',
      '素材',
      '产物',
    ])
    expect(wrapper.find('#workbench-panel-segments').exists()).toBe(false)
  })

  it('ref 会话即使 ≤10 秒也出现「分段」：单段直出显示 1 段并带只读子集', async () => {
    const refBrief = { ...makeDetail().brief, mode: 'ref', duration: 8 }
    const single = {
      index: 1,
      start_s: 0,
      end_s: 8,
      shots_in_segment: [1, 2],
      summary: '整片直出',
      end_hook: '',
      status: 'passed',
      prompt_artifact_id: 'sess-1/segments/segment-01.md',
      video_asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
      semantic_warning: '',
      assets: [{ number: 1, name: '橘猫', original_number: 2 }],
    }
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            mode: 'ref',
            brief: refBrief,
            segments: [single],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    // 判据是路线：ref 一律走段循环，≤ 单次生成上限的会话是「1 段」而不是长片。
    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((tab) => tab.text())).toContain('分段')
    await tabs.find((tab) => tab.text() === '分段')!.trigger('click')
    await settle()

    const panel = wrapper.find('#workbench-panel-segments')
    expect(panel.text()).toContain('第 1 段')
    expect(panel.text()).toContain('已通过 1 / 总数 1')
    expect(panel.text()).toContain('Picture 1 橘猫')
    // 路线传到卡片：ref 的已通过段有「换一段」。
    expect(panel.findAll('button').some((button) => button.text() === '换一段')).toBe(true)
  })
})

// D4-7（#234）：产物分区接进工作台。完成态（阶段 2 成功）直接给出交付文本，
// 没有「交付确认」第二道闸门（D4 spec §10 / Q7）。
describe('SessionDetailView 产物分区', () => {
  it('完成态展示交付文本：可复制、可下载，且无交付确认闸门', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'completed',
            artifacts: [
              {
                artifact_id: 'sess-1/video-prompt.md',
                name: 'video-prompt.md',
                kind: 'video_prompt',
                bytes: 120,
                created_at: '2026-11-11T00:00:00Z',
              },
              {
                artifact_id: 'sess-1/summary-zh.md',
                name: 'summary-zh.md',
                kind: 'summary_markdown',
                bytes: 60,
                created_at: '2026-11-11T00:00:00Z',
              },
            ],
          }),
        ),
      ),
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, ({ request }) => {
        const id = decodeURIComponent(new URL(request.url).pathname.split('/').at(-2) ?? '')
        return HttpResponse.text(id.endsWith('video-prompt.md') ? '# 最终提示词正文' : '摘要正文')
      }),
    )

    const { wrapper } = await mountDetail(makeStreamFetch())
    await wrapper.findAll('[role="tab"]')[3]!.trigger('click')
    await settle()

    expect(wrapper.text()).toContain('已完成')
    expect(wrapper.text()).toContain('# 最终提示词正文')
    expect(wrapper.text()).toContain('摘要正文')

    expect(wrapper.find('[data-copy="video-prompt.md"]').exists()).toBe(true)
    expect(wrapper.get('[data-download="video-prompt.md"]').attributes('href')).toBe(
      'http://localhost/api/v1/artifacts/sess-1/video-prompt.md/content',
    )

    // 无交付确认：完成即终态，界面上不存在「确认交付／交付确认」按钮。
    expect(wrapper.findAll('button').some((b) => /确认交付|交付确认/.test(b.text()))).toBe(false)
  })
})

// D8-2（#262）：导演台恒有「分段」——段数是规划产物，不看 10 秒阈值。
describe('SessionDetailView 导演台分段分区（#262）', () => {
  it('短视频导演台会话也有分段分区，并显示帧网格与总长说明', async () => {
    const brief = {
      ...makeDetail().brief,
      mode: 'director',
      duration: 4,
      director_mode: 't2v',
      director_node_id: 'node-42',
    }
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            mode: 'director',
            brief,
            director_notes: ['总帧目标 96 帧，实际落位 90 帧（差 6 帧）：段表按 17k 网格执行'],
            segments: [
              {
                index: 1,
                start_s: 0,
                end_s: 3,
                shots_in_segment: [],
                summary: '开场：雨夜街口',
                end_hook: '人物走进门廊',
                status: 'pending',
                prompt_artifact_id: 'sess-1/director/segment-01.md',
                video_asset_id: '',
                semantic_warning: '',
                segment_type: 'opening',
                guided: false,
                start_frame: 0,
                end_frame: 90,
                declared_frames: 90,
                export_frames: 90,
                give_images: [],
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((tab) => tab.text())).toContain('分段')

    const index = tabs.findIndex((tab) => tab.text() === '分段')
    await tabs[index]!.trigger('click')
    await settle()

    const panel = wrapper.find('#workbench-panel-segments')
    expect(panel.text()).toContain('90 → 90')
    expect(panel.text()).toContain('差 6 帧')
  })
})

// #272：概览顶部常驻步骤带。所有路线都渲染，但只列本路线实际步骤。
describe('SessionDetailView 步骤带（#272）', () => {
  const longBrief = { ...makeDetail().brief, duration: 18, variant: 'T2VA' }
  const segments = Array.from({ length: 3 }, (_, index) => ({
    index: index + 1,
    start_s: index * 6,
    end_s: (index + 1) * 6,
    shots_in_segment: [index + 1],
    summary: '',
    end_hook: '',
    status: index === 0 ? 'waiting_video' : 'pending',
    prompt_artifact_id: '',
    video_asset_id: '',
    semantic_warning: '',
  }))

  it('长片等待交段：列出本路线全部步骤，分段循环当前并显示 x/y，不逐段展开', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            brief: longBrief,
            segments,
            action_items: [
              {
                item_id: 'item-seg-1',
                kind: 'segment_video',
                status: 'pending',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '',
                reason: '',
                segment_index: 1,
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const band = wrapper.find('ol[aria-label="步骤进度"]')
    expect(band.exists()).toBe(true)
    expect(band.text()).toContain('阶段 1')
    expect(band.text()).toContain('审查')
    expect(band.text()).toContain('阶段 2')
    expect(band.text()).toContain('分段规划')
    expect(band.text()).toContain('分段循环')
    expect(band.text()).toContain('拼接')
    // T2VA 没有帧提交；步骤条目不按 3 段展开。
    expect(band.text()).not.toContain('帧提交')
    expect(wrapper.findAll('[data-step]')).toHaveLength(7)

    const loop = wrapper.find('[data-step="segment_loop"]')
    expect(loop.attributes('data-state')).toBe('current')
    expect(loop.text()).toContain('0/3')
  })

  it('短视频只列短视频步骤：没有分段规划、分段循环或拼接', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            action_items: [
              {
                item_id: 'item-1',
                kind: 'stage1_review',
                status: 'pending',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '',
                reason: '',
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const band = wrapper.find('ol[aria-label="步骤进度"]')
    expect(band.text()).toContain('阶段 1')
    expect(band.text()).toContain('审查')
    expect(band.text()).not.toContain('分段规划')
    expect(band.text()).not.toContain('分段循环')
    expect(band.text()).not.toContain('拼接')
    expect(wrapper.find('[data-step="review"]').attributes('data-state')).toBe('current')
  })

  it('运行中显示中文节点名、已耗时与上一步', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(makeDetail({ status: 'running', current_job: makeJob() })),
      ),
    )
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)

    const fiveSecondsAgo = new Date(Date.now() - 5_000).toISOString()
    const sixSecondsAgo = new Date(Date.now() - 6_000).toISOString()
    stream.push(
      `id: 2\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 2,
        type: 'job.progress',
        at: sixSecondsAgo,
        session_id: 'sess-1',
        payload: { stage: 'stage1', node: 'storyboard', status: 'done' },
      })}\n\n`,
    )
    stream.push(
      `id: 3\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 3,
        type: 'job.progress',
        at: fiveSecondsAgo,
        session_id: 'sess-1',
        payload: { stage: 'stage1', node: 'producer', status: 'started' },
      })}\n\n`,
    )
    await settle()

    const band = wrapper.find('nav[aria-label="步骤带"]')
    expect(band.text()).toContain('制片')
    expect(band.text()).toContain('上一步：分镜')
    expect(band.text()).toMatch(/已耗时 \d+ 秒/)
    expect(wrapper.find('[data-step="stage1"]').attributes('data-state')).toBe('current')
  })

  it('刷新后服务端补发的旧进度也能撑起步骤带（#271 + #272）', async () => {
    // 快照序号 4 就是订阅游标；游标之后没有事件，节点只能来自补发的 seq <= 4 的进度。
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({ status: 'running', current_job: makeJob(), last_event_seq: 4 }),
        ),
      ),
    )
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)

    const fiveSecondsAgo = new Date(Date.now() - 5_000).toISOString()
    stream.push(
      `id: 3\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 3,
        type: 'job.progress',
        at: fiveSecondsAgo,
        session_id: 'sess-1',
        payload: { stage: 'stage1', node: 'parallel_visual', status: 'started' },
      })}\n\n`,
    )
    await settle()

    const band = wrapper.find('nav[aria-label="步骤带"]')
    expect(band.text()).toContain('视听设计')
    expect(band.text()).toMatch(/已耗时 \d+ 秒/)
    expect(wrapper.find('[data-step="stage1"]').attributes('data-state')).toBe('current')
  })

  it('刷新后只补发一条 done：步骤带只显示「上一步」，不把它同时当成当前（#272）', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({ status: 'running', current_job: makeJob(), last_event_seq: 4 }),
        ),
      ),
    )
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)

    const doneAt = new Date(Date.now() - 3_000).toISOString()
    stream.push(
      `id: 3\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 3,
        type: 'job.progress',
        at: doneAt,
        session_id: 'sess-1',
        payload: { stage: 'stage1', node: 'director', status: 'done' },
      })}\n\n`,
    )
    await settle()

    const band = wrapper.find('nav[aria-label="步骤带"]')
    // 上一步是中文节点名，且它不再同时出现在「当前」那一句里。
    expect(band.text()).toContain('上一步：导演')
    expect(band.text()).not.toContain('当前：')
    // 「正在执行」行也不该把已跑完的节点当成当前节点。
    expect(wrapper.text()).toContain('等待进度…')
    expect(wrapper.text()).not.toContain('阶段 1 · 导演')
  })

  it('失败会话步骤带保留最后状态，也不新增步骤带专属徽章', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'failed',
            brief: longBrief,
            segments,
            action_items: [
              {
                item_id: 'item-seg-1',
                kind: 'segment_video',
                status: 'pending',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '',
                reason: '',
                segment_index: 1,
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    // 会话状态仍是七态之一（失败），步骤带自己不加第二个状态词。
    expect(wrapper.text()).toContain('失败')
    expect(wrapper.find('[data-step="segment_loop"]').attributes('data-state')).toBe('current')
    expect(wrapper.find('ol[aria-label="步骤进度"]').text()).not.toContain('已失败')
  })
})

// #273：步骤带覆盖 ref 与导演台两条路线——只列本路线步骤，状态由快照 + 流上进度推。
describe('SessionDetailView 步骤带（#273：ref / 导演台）', () => {
  const briefOf = (overrides: Partial<ReturnType<typeof makeDetail>['brief']>) => ({
    ...makeDetail().brief,
    ...overrides,
  })

  it('ref 等待交第 2 件素材：素材循环是当前步骤并显示 1/2', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            mode: 'ref',
            status: 'awaiting_human',
            brief: briefOf({
              mode: 'ref',
              duration: 8,
              refs: [
                {
                  asset_id: 'sess-1/a1',
                  kind: 'picture',
                  name: '橘猫',
                  number: 1,
                  status: 'passed',
                  source: 'derived',
                },
                {
                  asset_id: 'sess-1/a2',
                  kind: 'picture',
                  name: '台灯',
                  number: 2,
                  status: 'passed',
                  source: 'derived',
                },
              ],
            }),
            action_items: [
              {
                item_id: 'item-asset-1',
                kind: 'asset_image',
                status: 'resolved',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '2026-11-11T00:01:00Z',
                reason: '',
                asset_index: 1,
              },
              {
                item_id: 'item-asset-2',
                kind: 'asset_image',
                status: 'pending',
                created_at: '2026-11-11T00:01:00Z',
                resolved_at: '',
                reason: '',
                asset_index: 2,
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const band = wrapper.find('ol[aria-label="步骤进度"]')
    expect(band.text()).toContain('素材循环')
    expect(band.text()).toContain('分段循环')
    expect(band.text()).not.toContain('帧提交')
    expect(band.text()).not.toContain('分段规划')
    expect(band.text()).not.toContain('拼接')
    expect(wrapper.find('[data-step="asset_loop"]').attributes('data-state')).toBe('current')
    expect(wrapper.find('[data-step="asset_loop"]').text()).toContain('1/2')
  })

  it('导演台 fl2v 等待开场帧：开场帧是当前步骤，列出打包与交付', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            mode: 'director',
            status: 'awaiting_human',
            brief: briefOf({ mode: 'director', director_mode: 'fl2v', duration: 14 }),
            artifacts: [
              {
                artifact_id: 'sess-1/script.md',
                name: 'script.md',
                kind: 'script',
                bytes: 10,
                created_at: '2026-11-11T00:00:00Z',
              },
            ],
            action_items: [
              {
                item_id: 'item-review',
                kind: 'stage1_review',
                status: 'resolved',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '2026-11-11T00:01:00Z',
                reason: '',
              },
              {
                item_id: 'item-start',
                kind: 'director_start_image',
                status: 'pending',
                created_at: '2026-11-11T00:01:00Z',
                resolved_at: '',
                reason: '',
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const band = wrapper.find('ol[aria-label="步骤进度"]')
    expect(band.text()).toContain('开场帧')
    expect(band.text()).toContain('打包')
    expect(band.text()).toContain('段循环')
    expect(band.text()).toContain('交付')
    const byKey = Object.fromEntries(
      wrapper
        .findAll('[data-step]')
        .map((step) => [step.attributes('data-step'), step.attributes('data-state')]),
    )
    expect(byKey.review).toBe('done')
    expect(byKey.start_frame).toBe('current')
    expect(byKey.pack).toBe('pending')
  })

  it('导演台 t2v 不列开场帧', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            mode: 'director',
            status: 'awaiting_human',
            brief: briefOf({ mode: 'director', director_mode: 't2v' }),
            action_items: [
              {
                item_id: 'item-review',
                kind: 'stage1_review',
                status: 'pending',
                created_at: '2026-11-11T00:00:00Z',
                resolved_at: '',
                reason: '',
              },
            ],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const band = wrapper.find('ol[aria-label="步骤进度"]')
    expect(band.text()).not.toContain('开场帧')
    expect(wrapper.find('[data-step="review"]').attributes('data-state')).toBe('current')
  })

  it('导演台运行中：段循环当前，当前节点翻成带段号的中文名', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            mode: 'director',
            status: 'running',
            current_job: makeJob(),
            brief: briefOf({ mode: 'director', director_mode: 'fl2v', duration: 14 }),
            segments: Array.from({ length: 2 }, (_, index) => ({
              index: index + 1,
              start_s: index * 7,
              end_s: (index + 1) * 7,
              shots_in_segment: [index + 1],
              summary: '',
              end_hook: '',
              status: index === 0 ? 'passed' : 'pending',
              prompt_artifact_id: '',
              video_asset_id: '',
              semantic_warning: '',
            })),
          }),
        ),
      ),
    )
    const stream = makeStreamFetch()
    const { wrapper } = await mountDetail(stream)

    const fiveSecondsAgo = new Date(Date.now() - 5_000).toISOString()
    const sixSecondsAgo = new Date(Date.now() - 6_000).toISOString()
    stream.push(
      `id: 2\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 2,
        type: 'job.progress',
        at: sixSecondsAgo,
        session_id: 'sess-1',
        payload: { stage: 'segmenting', node: 'director_firstframe-02', status: 'done' },
      })}\n\n`,
    )
    stream.push(
      `id: 3\nevent: job.progress\ndata: ${JSON.stringify({
        seq: 3,
        type: 'job.progress',
        at: fiveSecondsAgo,
        session_id: 'sess-1',
        payload: { stage: 'segmenting', node: 'director_tailframe-02', status: 'started' },
      })}\n\n`,
    )
    await settle()

    const band = wrapper.find('nav[aria-label="步骤带"]')
    expect(band.text()).toContain('第 2 段尾帧词')
    expect(band.text()).toContain('上一步：第 2 段首帧词')
    expect(wrapper.find('[data-step="segment_loop"]').attributes('data-state')).toBe('current')
    expect(wrapper.find('[data-step="segment_loop"]').text()).toContain('1/2')
  })
})

// #276：步骤带上移为详情页常驻；「等待你操作」同层全局可见；取消仍只在概览分区。
describe('SessionDetailView 步骤带与等待提示全局化（#276）', () => {
  const longBrief = { ...makeDetail().brief, duration: 18, variant: 'T2VA' }
  const segments = Array.from({ length: 3 }, (_, index) => ({
    index: index + 1,
    start_s: index * 6,
    end_s: (index + 1) * 6,
    shots_in_segment: [index + 1],
    summary: '',
    end_hook: '',
    status: 'pending',
    prompt_artifact_id: '',
    video_asset_id: '',
    semantic_warning: '',
  }))

  // 「等待你操作」用的同一条待办：多个用例都要它，抽一处免得改动时漏掉某一份。
  const pendingSegmentVideo = (): NonNullable<
    ReturnType<typeof makeDetail>['action_items']
  >[number] => ({
    item_id: 'item-seg-2',
    kind: 'segment_video',
    status: 'pending',
    created_at: '2026-11-11T00:00:00Z',
    resolved_at: '',
    reason: '',
    segment_index: 2,
  })

  async function clickTab(
    wrapper: Awaited<ReturnType<typeof mountDetail>>['wrapper'],
    label: string,
  ) {
    const tab = wrapper.findAll('[role="tab"]').find((item) => item.text() === label)!
    await tab.trigger('click')
    await settle()
  }

  it('切到任一分区，步骤带仍然渲染且当前步骤正确', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            brief: longBrief,
            segments,
            action_items: [pendingSegmentVideo()],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    // 概览之外的分区：分段 / 产物 / 审查。步骤带不在概览 panel 里，切过去都仍在。
    for (const label of ['审查与待办', '分段', '产物']) {
      await clickTab(wrapper, label)
      expect(wrapper.find('nav[aria-label="步骤带"]').exists()).toBe(true)
      expect(wrapper.find('[data-step="segment_loop"]').attributes('data-state')).toBe('current')
    }
  })

  it('存在待办时任何分区都显示只读「等待你操作」并说清第几段', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({
            status: 'awaiting_human',
            brief: longBrief,
            segments,
            action_items: [pendingSegmentVideo()],
          }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    expect(wrapper.find('.pending-hint').text()).toBe('等待你操作：提交第 2 段视频')
    for (const label of ['审查与待办', '分段', '产物']) {
      await clickTab(wrapper, label)
      expect(wrapper.find('.pending-hint').text()).toBe('等待你操作：提交第 2 段视频')
    }
  })

  it('非概览分区不出现取消按钮：有副作用的写操作仍只在概览', async () => {
    server.use(
      http.get(detailUrl, () =>
        HttpResponse.json(
          makeDetail({ status: 'running', brief: longBrief, current_job: makeJob() }),
        ),
      ),
    )
    const { wrapper } = await mountDetail(makeStreamFetch())

    const hasCancel = () => wrapper.findAll('button').some((button) => button.text() === '取消')
    expect(hasCancel()).toBe(true)

    for (const label of ['审查与待办', '分段', '产物']) {
      await clickTab(wrapper, label)
      expect(hasCancel()).toBe(false)
    }
  })
})
