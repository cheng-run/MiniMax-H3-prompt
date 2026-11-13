import { flushPromises, type VueWrapper } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSessionToken } from '@/api/client'
import { mountWithPlugins } from '@/test/mount'
import { makeDetail, makeJob } from '@/test/sessionFixtures'
import { server } from '@/test/server'

import SessionCreateForm from './SessionCreateForm.vue'

const createUrl = 'http://localhost/api/v1/sessions'
const jobsUrl = 'http://localhost/api/v1/sessions/sess-1/jobs'

// MSW + 组件状态跨几个微任务与一次宏任务，统一 flush（沿用 DiagnosticsView.spec）。
async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

function submitButton(wrapper: VueWrapper) {
  return wrapper.find('button[type="submit"]')
}

// 每次用例都从「没取过令牌」开始，避免模块缓存掩盖引导端点的调用次数。
beforeEach(() => resetSessionToken())

describe('SessionCreateForm 表单字段与默认值', () => {
  it('默认值符合 spec §5：时长 5、变体 FL2VA、风格 Cinematic、语言 Chinese', () => {
    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })

    expect((wrapper.find('input[name="duration"]').element as HTMLInputElement).value).toBe('5')
    expect((wrapper.find('select[name="variant"]').element as HTMLSelectElement).value).toBe(
      'FL2VA',
    )
    expect((wrapper.find('input[name="style"]').element as HTMLInputElement).value).toBe(
      'Cinematic',
    )
    expect((wrapper.find('input[name="language"]').element as HTMLInputElement).value).toBe(
      'Chinese',
    )
  })

  it('补充说明与草稿在高级折叠区，不出现参考素材/画布/视频输出字段', () => {
    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })

    const details = wrapper.find('details')
    expect(details.exists()).toBe(true)
    expect(details.find('textarea[name="clarifications"]').exists()).toBe(true)
    expect(details.find('textarea[name="draft"]').exists()).toBe(true)

    // V1 明确不出现这些输入（D4-AC1 / D4-AC17）。
    expect(wrapper.find('[name="refs"]').exists()).toBe(false)
    expect(wrapper.find('[name="canvases"]').exists()).toBe(false)
    expect(wrapper.find('[name="video_output"]').exists()).toBe(false)
  })
})

describe('SessionCreateForm 校验', () => {
  it('剧情为空时拦截提交且不发请求', async () => {
    let createCalls = 0
    server.use(
      http.post(createUrl, () => {
        createCalls += 1
        return HttpResponse.json(makeDetail(), { status: 201 })
      }),
    )

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(wrapper.find('[role="alert"]').text()).toContain('剧情')
    expect(createCalls).toBe(0)
  })

  it('时长越界即时提示并阻止提交（4–60 之外）', async () => {
    let createCalls = 0
    server.use(
      http.post(createUrl, () => {
        createCalls += 1
        return HttpResponse.json(makeDetail(), { status: 201 })
      }),
    )

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    server.use(http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })))

    const duration = wrapper.find('input[name="duration"]')
    await duration.setValue('61')
    expect(wrapper.text()).toContain('4–60')

    await wrapper.find('form').trigger('submit')
    await settle()
    expect(createCalls).toBe(0)

    await duration.setValue('3')
    await wrapper.find('form').trigger('submit')
    await settle()
    expect(createCalls).toBe(0)

    // 长片不再由前端拦住：12 秒能提交（服务端上界 60）。
    await duration.setValue('12')
    await wrapper.find('form').trigger('submit')
    await settle()
    expect(createCalls).toBe(1)
  })
})

describe('SessionCreateForm 创建即起跑', () => {
  it('显式发送 duration=5 与 variant=FL2VA，起跑后跳转工作台', async () => {
    let created: Record<string, unknown> | null = null
    let createKey: string | null = null
    let jobKey: string | null = null
    server.use(
      http.post(createUrl, async ({ request }) => {
        createKey = request.headers.get('idempotency-key')
        created = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeDetail({ session_id: 'sess-1' }), { status: 201 })
      }),
      http.post(jobsUrl, ({ request }) => {
        jobKey = request.headers.get('idempotency-key')
        return HttpResponse.json(makeJob(), { status: 202 })
      }),
    )

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(created).toMatchObject({
      mode: 'base',
      plot: '雨夜旧信',
      duration: 5,
      variant: 'FL2VA',
      style: 'Cinematic',
      language: 'Chinese',
    })
    expect(createKey).toBeTruthy()
    expect(jobKey).toBeTruthy()
    expect(router!.currentRoute.value.path).toBe('/sessions/sess-1')
  })

  it('创建成功但起跑失败仍跳转详情（会话停在 created，交给兜底「开始」）', async () => {
    // 起跑失败会留一条 console.warn 信号；这里验证的是「仍然跳转」，把噪声收掉。
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    server.use(
      http.post(createUrl, () =>
        HttpResponse.json(makeDetail({ session_id: 'sess-1' }), { status: 201 }),
      ),
      http.post(jobsUrl, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '服务器内部错误',
            status: 500,
            detail: 'boom',
            instance: jobsUrl,
          },
          { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    await wrapper.find('form').trigger('submit')
    await settle()

    // 会话已经建出来了，不能把用户留在表单上再建一条；详情页负责「开始」兜底。
    expect(router!.currentRoute.value.path).toBe('/sessions/sess-1')
    warn.mockRestore()
  })

  it('提交进行中禁用按钮，重复提交不会发出第二条创建请求', async () => {
    let createCalls = 0
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.post(createUrl, async () => {
        createCalls += 1
        await gate
        return HttpResponse.json(makeDetail({ session_id: 'sess-1' }), { status: 201 })
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()
    // 再点一次（模拟双击）不应再发请求。
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(createCalls).toBe(1)

    release()
    await settle()
  })

  it('失败后重试复用同一个幂等键（双击不产生两条会话）', async () => {
    const keys: (string | null)[] = []
    let attempt = 0
    server.use(
      http.post(createUrl, ({ request }) => {
        attempt += 1
        keys.push(request.headers.get('idempotency-key'))
        if (attempt === 1) {
          return HttpResponse.json(
            {
              type: 'about:blank',
              title: '服务器内部错误',
              status: 500,
              detail: 'boom',
              instance: createUrl,
            },
            { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json(makeDetail({ session_id: 'sess-1' }), { status: 201 })
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()
    // 第一次失败后仍停在表单，错误可见。
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(keys).toHaveLength(2)
    expect(keys[0]).toBeTruthy()
    expect(keys[1]).toBe(keys[0])
  })
})
