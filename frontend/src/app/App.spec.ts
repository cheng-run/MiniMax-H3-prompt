import { flushPromises } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { mountWithPlugins } from '@/test/mount'

import App from './App.vue'

describe('App 应用壳', () => {
  it('渲染 header/nav/main 语义地标与跳转主内容链接', async () => {
    const { wrapper, router } = mountWithPlugins(App, { withRouter: true })
    await router!.isReady()
    await flushPromises()

    expect(wrapper.find('header').exists()).toBe(true)
    expect(wrapper.find('nav[aria-label="主导航"]').exists()).toBe(true)
    expect(wrapper.find('main#main-content').exists()).toBe(true)

    const skip = wrapper.find('a.skip-link')
    expect(skip.exists()).toBe(true)
    expect(skip.text()).toBe('跳到主内容')
    expect(skip.attributes('href')).toBe('#main-content')
  })

  it('未知路径渲染 404 视图，并提供返回首页的链接', async () => {
    const { wrapper, router } = mountWithPlugins(App, { withRouter: true })
    await router!.push('/does-not-exist')
    await flushPromises()

    // 2026-11-21 重做：404 文案换成「这个地址没有对应的页面」，断言跟着改；
    // 检查的语义没变——仍然要有一段说明 + 一条回首页的链接。
    expect(wrapper.text()).toContain('这个地址没有对应的页面')
    const homeLink = wrapper.findAll('a').find((a) => a.attributes('href') === '/')
    expect(homeLink).toBeTruthy()
  })
})
