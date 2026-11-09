import { flushPromises } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '@/test/server'
import { mountWithPlugins } from '@/test/mount'

import DiagnosticsView from './DiagnosticsView.vue'

const url = 'http://localhost/api/v1/health'
const okBody = { status: 'ok', service: 'minimax-h3-prompt', version: '0.1.0' }

// MSW + Vue Query 的完成要跨几个微任务与一次宏任务，统一 flush。
async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

describe('DiagnosticsView 四态与客户端状态', () => {
  it('加载中显示 role=status 文字，成功后显示真实 health', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get(url, async () => {
        await gate
        return HttpResponse.json(okBody)
      }),
    )

    const { wrapper } = mountWithPlugins(DiagnosticsView)
    await flushPromises()

    const status = wrapper.find('[role="status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('加载中')

    release()
    await settle()

    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('minimax-h3-prompt')
    expect(wrapper.text()).toContain('服务正常')
  })

  it('成功后展开/收起详情（Pinia 客户端状态），控件是原生 button', async () => {
    server.use(http.get(url, () => HttpResponse.json(okBody)))

    const { wrapper } = mountWithPlugins(DiagnosticsView)
    await settle()

    const toggle = wrapper.findAll('button').find((b) => b.text().includes('展开详情'))!
    // 原生 <button> 天生支持键盘 Enter/Space；真实按键在 E2E（#211）验证。
    expect(toggle.element.tagName).toBe('BUTTON')
    // 键盘可达：原生 button 的 tabIndex 为 0（可 Tab 聚焦）；真实 Enter 切换在 E2E 覆盖，
    // 因为 jsdom 不实现 Enter→click 的默认行为，这里硬断言按键切换等于伪造。
    expect((toggle.element as HTMLElement).tabIndex).toBe(0)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('pre').exists()).toBe(false)

    await toggle.trigger('click')
    expect(wrapper.find('pre').exists()).toBe(true)

    const collapse = wrapper.findAll('button').find((b) => b.text().includes('收起详情'))!
    expect(collapse.attributes('aria-expanded')).toBe('true')
    await collapse.trigger('click')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('错误显示 role=alert，点重试后恢复成功', async () => {
    let calls = 0
    server.use(
      http.get(url, () => {
        calls += 1
        if (calls === 1) {
          return HttpResponse.json(
            {
              type: 'about:blank',
              title: '服务器内部错误',
              status: 500,
              detail: 'boom',
              instance: '/api/v1/health',
            },
            { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json(okBody)
      }),
    )

    const { wrapper } = mountWithPlugins(DiagnosticsView)
    await settle()

    expect(wrapper.find('[role="alert"]').exists()).toBe(true)

    const retry = wrapper.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await settle()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('minimax-h3-prompt')
  })
})
