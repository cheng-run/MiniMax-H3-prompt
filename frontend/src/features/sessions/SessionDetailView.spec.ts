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
      'id: 4\nevent: job.progress\ndata: {"stage":"stage1","node":"storyboard","status":"started"}\n\n',
    )
    await settle()
    expect(wrapper.text()).toContain('storyboard')

    const cancel = wrapper.findAll('button').find((b) => b.text() === '取消')!
    expect(cancel).toBeTruthy()
    await cancel.trigger('click')
    await settle()

    expect(cancels).toBe(1)
    // 文案不许承诺立即停止：取消是节点边界生效（ADR 0032）。
    expect(wrapper.text()).toContain('当前步骤结束后停止')
    expect(wrapper.findAll('button').some((b) => b.text() === '取消')).toBe(false)
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
      rejection_reason: '',
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
      rejection_reason: '',
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
