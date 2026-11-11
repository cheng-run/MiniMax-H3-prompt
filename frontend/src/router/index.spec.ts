import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'

import { createAppRouter } from './index'

describe('路由', () => {
  it('详情路由承接含斜杠的会话标识（<slug>/GEN001）', async () => {
    const router = createAppRouter(createMemoryHistory())
    await router.push('/sessions/雨夜旧信-abc123/GEN001')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('session-detail')
    expect(router.currentRoute.value.params.id).toBe('雨夜旧信-abc123/GEN001')
  })

  it('首页是会话列表', async () => {
    const router = createAppRouter(createMemoryHistory())
    await router.push('/')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('sessions')
  })
})
