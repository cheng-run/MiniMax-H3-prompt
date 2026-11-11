import { flushPromises } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken } from '@/api/client'
import { mountWithPlugins } from '@/test/mount'
import { makeDetail, makeJob } from '@/test/sessionFixtures'
import { server } from '@/test/server'

import SessionDetailView from './SessionDetailView.vue'

const detailUrl = 'http://localhost/api/v1/sessions/sess-1'
const jobsUrl = 'http://localhost/api/v1/sessions/sess-1/jobs'

async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

beforeEach(() => resetSessionToken())

describe('SessionDetailView 深链快照', () => {
  it('直接打开深链（刷新恢复）先 GET 详情并呈现 brief/状态/待办/产物/素材', async () => {
    const detail = makeDetail({
      status: 'awaiting_human',
      plot: '雨夜旧信',
      action_items: [
        {
          item_id: 'item-1',
          kind: 'stage1_approval',
          status: 'pending',
          created_at: '2026-11-11T00:00:00Z',
          resolved_at: '',
        },
      ],
      artifacts: [
        {
          artifact_id: 'sess-1/script.md',
          name: 'script.md',
          kind: 'text',
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
    })
    server.use(http.get(detailUrl, () => HttpResponse.json(detail)))

    const { wrapper, router } = mountWithPlugins(SessionDetailView, { withRouter: true })
    await router!.push('/sessions/sess-1')
    await router!.isReady()
    await settle()

    expect(wrapper.text()).toContain('雨夜旧信')
    expect(wrapper.text()).toContain('等待人工')
    expect(wrapper.text()).toContain('5') // brief.duration
    expect(wrapper.text()).toContain('stage1_approval') // 待办原始呈现
    expect(wrapper.text()).toContain('script.md') // 产物
    expect(wrapper.text()).toContain('photo.png') // 素材
  })

  it('created 状态显示「开始」兜底，点击即起跑并重拉为运行中', async () => {
    let gets = 0
    server.use(
      http.get(detailUrl, () => {
        gets += 1
        return HttpResponse.json(makeDetail({ status: gets === 1 ? 'created' : 'running' }))
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )

    const { wrapper, router } = mountWithPlugins(SessionDetailView, { withRouter: true })
    await router!.push('/sessions/sess-1')
    await router!.isReady()
    await settle()

    expect(wrapper.text()).toContain('已创建')
    const start = wrapper.findAll('button').find((b) => b.text().includes('开始'))!
    expect(start).toBeTruthy()

    await start.trigger('click')
    await settle()

    expect(gets).toBeGreaterThanOrEqual(2)
    expect(wrapper.text()).toContain('运行中')
  })

  it('非 created 状态不显示「开始」按钮', async () => {
    server.use(http.get(detailUrl, () => HttpResponse.json(makeDetail({ status: 'running' }))))

    const { wrapper, router } = mountWithPlugins(SessionDetailView, { withRouter: true })
    await router!.push('/sessions/sess-1')
    await router!.isReady()
    await settle()

    expect(wrapper.findAll('button').some((b) => b.text().includes('开始'))).toBe(false)
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

    const { wrapper, router } = mountWithPlugins(SessionDetailView, { withRouter: true })
    await router!.push('/sessions/sess-1')
    await router!.isReady()
    await settle()

    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    const retry = wrapper.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await settle()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('恢复')
  })
})
