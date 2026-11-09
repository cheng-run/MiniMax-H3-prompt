import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import type { Component, Plugin } from 'vue'
import { createMemoryHistory } from 'vue-router'

import { createAppRouter } from '@/router'

/**
 * 统一挂载：每个用例一份新的 Pinia 与 Vue Query 客户端（后者由 VueQueryPlugin 在
 * 未传 client 时自建），互不串状态。需要真实路由的用例传 withRouter。
 */
export function mountWithPlugins(component: Component, { withRouter = false } = {}) {
  const pinia = createPinia()
  const plugins: Plugin[] = [pinia, VueQueryPlugin]
  const router = withRouter ? createAppRouter(createMemoryHistory()) : undefined
  if (router) {
    plugins.push(router)
  }

  const wrapper = mount(component, { global: { plugins } })
  return { wrapper, pinia, router }
}
