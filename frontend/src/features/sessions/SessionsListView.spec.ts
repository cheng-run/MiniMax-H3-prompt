import { flushPromises } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken } from '@/api/client'
import { mountWithPlugins } from '@/test/mount'
import { makeSummary } from '@/test/sessionFixtures'
import { server } from '@/test/server'

import { SESSION_STATUSES, statusLabel } from './status'
import SessionsListView from './SessionsListView.vue'

const listUrl = 'http://localhost/api/v1/sessions'

async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

beforeEach(() => resetSessionToken())

describe('SessionsListView 列表与徽标', () => {
  it('渲染七态徽标与深链，简介与状态文字都可见', async () => {
    const items = SESSION_STATUSES.map((status, index) =>
      makeSummary({ session_id: `sess-${index}`, status, plot: `剧情-${index}` }),
    )
    server.use(http.get(listUrl, () => HttpResponse.json({ items })))

    const { wrapper, router } = mountWithPlugins(SessionsListView, { withRouter: true })
    await router!.isReady()
    await settle()

    for (const status of SESSION_STATUSES) {
      expect(wrapper.text()).toContain(statusLabel(status))
    }
    expect(wrapper.text()).toContain('剧情-0')
    expect(wrapper.find('a[href="/sessions/sess-0"]').exists()).toBe(true)
  })

  it('分页：下一页带游标，上一页回到首页', async () => {
    const requestedCursors: (string | null)[] = []
    server.use(
      http.get(listUrl, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        requestedCursors.push(cursor)
        if (cursor === null) {
          return HttpResponse.json({
            items: [makeSummary({ session_id: 'page-1', plot: '第一页' })],
            next_cursor: 'cursor-2',
          })
        }
        return HttpResponse.json({
          items: [makeSummary({ session_id: 'page-2', plot: '第二页' })],
          next_cursor: null,
        })
      }),
    )

    const { wrapper, router } = mountWithPlugins(SessionsListView, { withRouter: true })
    await router!.isReady()
    await settle()

    expect(wrapper.text()).toContain('第一页')
    const next = wrapper.findAll('button').find((b) => b.text().includes('下一页'))!
    await next.trigger('click')
    await settle()

    expect(requestedCursors).toContain('cursor-2')
    expect(wrapper.text()).toContain('第二页')

    const prev = wrapper.findAll('button').find((b) => b.text().includes('上一页'))!
    expect(prev.attributes('disabled')).toBeUndefined()
    await prev.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('第一页')
  })

  it('空列表给出提示而不是一片空白', async () => {
    server.use(http.get(listUrl, () => HttpResponse.json({ items: [] })))

    const { wrapper, router } = mountWithPlugins(SessionsListView, { withRouter: true })
    await router!.isReady()
    await settle()

    expect(wrapper.text()).toContain('还没有会话')
  })

  it('加载失败显示 role=alert 并可重试', async () => {
    let calls = 0
    server.use(
      http.get(listUrl, () => {
        calls += 1
        if (calls === 1) {
          return HttpResponse.json(
            {
              type: 'about:blank',
              title: '服务器内部错误',
              status: 500,
              detail: 'boom',
              instance: listUrl,
            },
            { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json({ items: [makeSummary({ session_id: 'sess-1', plot: '恢复' })] })
      }),
    )

    const { wrapper, router } = mountWithPlugins(SessionsListView, { withRouter: true })
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

describe('SessionsListView 新建入口', () => {
  it('点「新建会话」在列表页展开表单', async () => {
    server.use(http.get(listUrl, () => HttpResponse.json({ items: [] })))

    const { wrapper, router } = mountWithPlugins(SessionsListView, { withRouter: true })
    await router!.isReady()
    await settle()

    expect(wrapper.find('textarea[name="plot"]').exists()).toBe(false)
    const open = wrapper.findAll('button').find((b) => b.text().includes('新建会话'))!
    await open.trigger('click')
    await flushPromises()

    expect(wrapper.find('textarea[name="plot"]').exists()).toBe(true)
  })
})
