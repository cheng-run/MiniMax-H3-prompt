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

describe('SessionCreateForm 路线选择', () => {
  it('默认 base；选 ref 隐藏变体下拉，提交 mode=ref 与固定 T2VA', async () => {
    let created: Record<string, unknown> | null = null
    server.use(
      http.post(createUrl, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeDetail({ session_id: 'sess-1', status: 'created' }), {
          status: 201,
        })
      }),
      http.post(jobsUrl, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '资源状态冲突',
            status: 409,
            detail: '先上传参考素材',
            instance: jobsUrl,
            code: 'asset_required',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    expect(
      (wrapper.find('input[name="mode"][value="base"]').element as HTMLInputElement).checked,
    ).toBe(true)
    expect(wrapper.find('select[name="variant"]').exists()).toBe(true)

    await wrapper.find('input[name="mode"][value="ref"]').setValue()
    expect(wrapper.find('select[name="variant"]').exists()).toBe(false)

    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(created).toMatchObject({ mode: 'ref', variant: 'T2VA', plot: '雨夜旧信' })
    // 起跑被 409 拒绝（零素材）不算创建失败：会话已存在，仍进工作台。
    expect(router!.currentRoute.value.path).toBe('/sessions/sess-1')
    warn.mockRestore()
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

describe('SessionCreateForm 导演台路线（D8-1）', () => {
  function captureCreate() {
    let created: Record<string, unknown> | null = null
    server.use(
      http.post(createUrl, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeDetail({ session_id: 'sess-1', status: 'created' }), {
          status: 201,
        })
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )
    return () => created
  }

  it('默认不出现导演台字段；选 director 后隐藏变体并问齐模式与三个打包参数', async () => {
    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })

    expect(wrapper.find('[name="director_mode"]').exists()).toBe(false)
    expect(wrapper.find('[name="director_node_id"]').exists()).toBe(false)
    expect(wrapper.find('select[name="variant"]').exists()).toBe(true)

    await wrapper.find('input[name="mode"][value="director"]').setValue()

    expect(wrapper.find('select[name="variant"]').exists()).toBe(false)
    expect(wrapper.find('select[name="director_mode"]').exists()).toBe(true)
    expect(wrapper.find('input[name="director_node_id"]').exists()).toBe(true)
    expect(wrapper.find('input[name="director_cache_name"]').exists()).toBe(true)
    // 引导模式省略时服务端落 guide：前端与它同一条默认。
    expect(
      (wrapper.find('select[name="director_continuity_mode"]').element as HTMLSelectElement).value,
    ).toBe('guide')
  })

  it('提交 mode=director 与固定 T2VA，只带选中的导演台参数', async () => {
    const created = captureCreate()
    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    await wrapper.find('textarea[name="plot"]').setValue('雨夜街口的追逐')
    await wrapper.find('input[name="mode"][value="director"]').setValue()
    await wrapper.find('select[name="director_mode"]').setValue('i2v')
    await wrapper.find('input[name="director_node_id"]').setValue('node-42')
    await wrapper.find('input[name="director_cache_name"]').setValue('noir')
    await wrapper.find('select[name="director_continuity_mode"]').setValue('continue')

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(created()).toMatchObject({
      mode: 'director',
      variant: 'T2VA',
      director_mode: 'i2v',
      director_node_id: 'node-42',
      director_cache_name: 'noir',
      director_continuity_mode: 'continue',
    })
  })

  it('导演台模式与节点 id 未填时拦截提交', async () => {
    let createCalls = 0
    server.use(
      http.post(createUrl, () => {
        createCalls += 1
        return HttpResponse.json(makeDetail(), { status: 201 })
      }),
    )
    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    await wrapper.find('input[name="mode"][value="director"]').setValue()

    await wrapper.find('form').trigger('submit')
    await settle()
    expect(createCalls).toBe(0)
    expect(wrapper.text()).toContain('生成方式')

    await wrapper.find('select[name="director_mode"]').setValue('fl2v')
    await wrapper.find('form').trigger('submit')
    await settle()
    expect(createCalls).toBe(0)
    expect(wrapper.text()).toContain('节点 id')
  })
})
