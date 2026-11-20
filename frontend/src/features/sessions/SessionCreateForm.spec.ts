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

// 起步澄清问答面板（#339 / spec #332 E1）：点「创建」先拉问题、内联展开、逐条作答或
// 跳过，提交时带结构化问答对——渲染文本归后端（`render_clarification_block`），前端
// 只交 (question, answer)，不拼「1. Q → A」。
describe('SessionCreateForm 起步澄清', () => {
  const questionsUrl = 'http://localhost/api/v1/clarification-questions'

  // 生成失败的 problem+json：502 与网络错误同属「这一次拿不到问题」的降级信号。
  function failedResponse(status = 502) {
    return HttpResponse.json(
      {
        type: 'about:blank',
        title: '澄清问题生成失败',
        status,
        detail: '网关超时',
        instance: '/api/v1/clarification-questions',
        code: 'clarification_generation_failed',
      },
      { status, headers: { 'Content-Type': 'application/problem+json' } },
    )
  }

  // 默认把「拉问题 → 建会话 → 起跑」三段都接上，只在各用例覆盖自己关心的那一段。
  // ``questionsStatus`` 传数组＝按次给状态（重试用例要「先失败、后成功」）。
  function stubFlow({
    questions = ['窗在人物哪一侧？', '人物身体面朝哪边？'] as string[] | null,
    questionsStatus = 200,
    created = () => {},
  }: {
    questions?: string[] | null
    questionsStatus?: number | number[]
    created?: (body: Record<string, unknown> | null) => void
  } = {}) {
    const calls = { questions: 0, create: 0 }
    const statuses = Array.isArray(questionsStatus) ? questionsStatus : [questionsStatus]
    server.use(
      http.get(questionsUrl, () => {
        // 用掉的次数超过给定序列时沿用最后一个状态（序列只描述开头几次）。
        const status = statuses[Math.min(calls.questions, statuses.length - 1)]
        calls.questions += 1
        if (status >= 400) {
          return failedResponse(status)
        }
        return HttpResponse.json(questions ?? [])
      }),
      http.post(createUrl, async ({ request }) => {
        calls.create += 1
        created((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(makeDetail({ session_id: 'sess-1', status: 'created' }), {
          status: 201,
        })
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )
    return calls
  }

  // 面板上的回答框按问题顺序编号，用例据此逐条作答。
  function answer(wrapper: VueWrapper, index: number) {
    return wrapper.find(`[name="clarification-answer-${index}"]`)
  }

  it('点创建先拉问题并展开面板，逐条作答后提交结构化问答对', async () => {
    let body: Record<string, unknown> | null = null
    const calls = stubFlow({ created: (payload) => (body = payload) })

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()

    // 拉到了问题：面板展开，问题文字逐条可见；这时**还没有**建会话。
    expect(calls.questions).toBe(1)
    expect(calls.create).toBe(0)
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('窗在人物哪一侧？')
    expect(wrapper.text()).toContain('人物身体面朝哪边？')

    await answer(wrapper, 0).setValue('窗在人物右侧')
    await answer(wrapper, 1).setValue('人物面朝画面左侧')

    // 面板上的「继续创建」把问答对随会话提交。
    await wrapper.find('[data-action="clarification-submit"]').trigger('click')
    await settle()

    expect(calls.create).toBe(1)
    expect(body).toMatchObject({
      plot: '雨夜旧信',
      clarification_pairs: [
        { question: '窗在人物哪一侧？', answer: '窗在人物右侧' },
        { question: '人物身体面朝哪边？', answer: '人物面朝画面左侧' },
      ],
    })
    // 渲染文本归后端：前端绝不自己拼「1. Q → A」。
    expect(JSON.stringify(body)).not.toContain('→')
    expect(router!.currentRoute.value.path).toBe('/sessions/sess-1')
  })

  it('跳过 = 不提交任何问答对，直接建会话', async () => {
    let body: Record<string, unknown> | null = null
    const calls = stubFlow({ created: (payload) => (body = payload) })

    const { wrapper, router } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await router!.isReady()
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()
    expect(calls.create).toBe(0)

    await answer(wrapper, 0).setValue('窗在人物右侧')
    await wrapper.find('[data-action="clarification-skip"]').trigger('click')
    await settle()

    expect(calls.create).toBe(1)
    // 跳过是「一条都不交」：答过的那条也不带走。
    expect(body).not.toHaveProperty('clarification_pairs')
    expect(router!.currentRoute.value.path).toBe('/sessions/sess-1')
  })

  it('生成失败显示一行提示并允许无问题继续创建', async () => {
    let body: Record<string, unknown> | null = null
    const calls = stubFlow({ questionsStatus: 502, created: (payload) => (body = payload) })

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(calls.questions).toBe(1)
    expect(wrapper.text()).toContain('澄清问题生成失败，可直接开始')
    // 不展开作答面板，但提交入口还在（不能把用户卡在表单上）。
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(false)
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined()
    // 失败态有显式恢复入口（#359）：不必刷新页面重填表单就能再问一次。
    expect(wrapper.find('[data-action="clarification-retry"]').exists()).toBe(true)

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(calls.create).toBe(1)
    expect(body).not.toHaveProperty('clarification_pairs')
  })

  it('点重试会重新拉取澄清问题并展开面板', async () => {
    // 第一次 502（降级失败态），第二次成功：序列只描述前两次，之后沿用最后一个。
    const calls = stubFlow({ questionsStatus: [502, 200] })

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()
    expect(calls.questions).toBe(1)
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(false)

    await wrapper.find('[data-action="clarification-retry"]').trigger('click')
    await settle()

    // 重试真的重发了一次请求，并把面板展出来；这时仍然没有建会话。
    expect(calls.questions).toBe(2)
    expect(calls.create).toBe(0)
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('窗在人物哪一侧？')
    // 失败提示随面板展开退场，不再与面板并存。
    expect(wrapper.text()).not.toContain('澄清问题生成失败，可直接开始')
    expect(wrapper.find('[data-action="clarification-retry"]').exists()).toBe(false)
  })

  it('点重试再次失败时仍停在失败态，提示仍在且按钮可再点', async () => {
    const calls = stubFlow({ questionsStatus: 502 })

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()
    expect(wrapper.find('[data-action="clarification-retry"]').exists()).toBe(true)

    await wrapper.find('[data-action="clarification-retry"]').trigger('click')
    await settle()

    expect(calls.questions).toBe(2)
    expect(calls.create).toBe(0)
    expect(wrapper.text()).toContain('澄清问题生成失败，可直接开始')
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(false)
    // 失败后按钮回到可点状态：再点一次应当能发出第三次请求。
    const retry = wrapper.find('[data-action="clarification-retry"]')
    expect(retry.attributes('disabled')).toBeUndefined()
    await retry.trigger('click')
    await settle()
    expect(calls.questions).toBe(3)
  })

  it('拉取期间重试按钮进加载态，重复点击不会并行发多次请求', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    let questionCalls = 0
    let createCalls = 0
    server.use(
      http.get(questionsUrl, async () => {
        questionCalls += 1
        if (questionCalls === 1) {
          return failedResponse(502)
        }
        await gate
        return HttpResponse.json(['窗在人物哪一侧？'])
      }),
      http.post(createUrl, () => {
        createCalls += 1
        return HttpResponse.json(makeDetail({ session_id: 'sess-1' }), { status: 201 })
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()
    expect(questionCalls).toBe(1)

    await wrapper.find('[data-action="clarification-retry"]').trigger('click')
    await flushPromises()

    // 第二次请求还挂着：按钮已禁用，重复点击不会再发一次。
    const retry = wrapper.find('[data-action="clarification-retry"]')
    expect(retry.attributes('disabled')).toBeDefined()
    await retry.trigger('click')
    await flushPromises()
    expect(questionCalls).toBe(2)
    expect(createCalls).toBe(0)

    release()
    await settle()
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(true)
  })

  it('失败态下改剧情复位到空闲态：再点创建会重新拉问题而不是直接建会话', async () => {
    let body: Record<string, unknown> | null = null
    const calls = stubFlow({ questionsStatus: [502, 200], created: (payload) => (body = payload) })

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()
    // 失败态在：提示与重试按钮都在。
    expect(wrapper.text()).toContain('澄清问题生成失败，可直接开始')
    expect(calls.questions).toBe(1)

    // 改剧情＝换了主题，旧失败态不再代表这一次的提问结果，复位到空闲态。
    await wrapper.find('textarea[name="plot"]').setValue('清晨海港')
    expect(wrapper.text()).not.toContain('澄清问题生成失败，可直接开始')
    expect(wrapper.find('[data-action="clarification-retry"]').exists()).toBe(false)

    await wrapper.find('form').trigger('submit')
    await settle()

    // 复位后再点创建是「重新拉问题」，不是拿旧失败态直接建会话。
    expect(calls.questions).toBe(2)
    expect(calls.create).toBe(0)
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('窗在人物哪一侧？')

    // 面板上的「跳过」仍然能走到创建，且不携带问答对。
    await wrapper.find('[data-action="clarification-skip"]').trigger('click')
    await settle()
    expect(calls.create).toBe(1)
    expect(body).not.toHaveProperty('clarification_pairs')
  })

  it('拉问题期间按钮进入加载态，重复提交不会并行发两次请求', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    let questionCalls = 0
    let createCalls = 0
    server.use(
      http.get(questionsUrl, async () => {
        questionCalls += 1
        await gate
        return HttpResponse.json(['窗在人物哪一侧？'])
      }),
      http.post(createUrl, () => {
        createCalls += 1
        return HttpResponse.json(makeDetail({ session_id: 'sess-1' }), { status: 201 })
      }),
      http.post(jobsUrl, () => HttpResponse.json(makeJob(), { status: 202 })),
    )

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    // 问题还没回来：按钮已禁用、文案进入加载态，且**一次会话都没建**。
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined()
    expect(submitButton(wrapper).text()).toContain('正在')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(questionCalls).toBe(1)
    expect(createCalls).toBe(0)

    release()
    await settle()
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(true)
  })

  it('补充说明已填内容时答问答对先被拦下，不发创建请求', async () => {
    const calls = stubFlow()

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    await wrapper.find('textarea[name="clarifications"]').setValue('窗必须在人物右边')

    await wrapper.find('form').trigger('submit')
    await settle()
    await answer(wrapper, 0).setValue('窗在人物右侧')

    await wrapper.find('[data-action="clarification-submit"]').trigger('click')
    await settle()

    // 两边都有内容服务端会 422，但在客户端先拦下：用户不必等一次往返才知道。
    expect(calls.create).toBe(0)
    expect(wrapper.find('[role="alert"]').text()).toContain('二选一')
  })

  it('面板展开时点主按钮「创建并开始」也先被本地拦下（拦截在唯一提交口）', async () => {
    const calls = stubFlow()

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')
    await wrapper.find('textarea[name="clarifications"]').setValue('窗必须在人物右边')

    // 第一次提交拉问题、展开面板（还不会建会话）。
    await wrapper.find('form').trigger('submit')
    await settle()
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(true)
    await answer(wrapper, 0).setValue('窗在人物右侧')

    // 展开面板后点**主按钮**（表单 submit）而不是面板的「继续创建」：走 onSubmit 的
    // asking 分支 → createWithClarifications。拦截若只留在 submitClarifications，这条
    // 会直接发出创建请求、吃服务端 422；收进唯一提交口后本地就拦下。
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(calls.create).toBe(0)
    expect(wrapper.find('[role="alert"]').text()).toContain('二选一')
  })

  it('没有任何问题时不展开面板，直接建会话且不带问答对', async () => {
    let body: Record<string, unknown> | null = null
    const calls = stubFlow({ questions: [], created: (payload) => (body = payload) })

    const { wrapper } = mountWithPlugins(SessionCreateForm, { withRouter: true })
    await wrapper.find('textarea[name="plot"]').setValue('雨夜旧信')

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(calls.questions).toBe(1)
    expect(calls.create).toBe(1)
    expect(wrapper.find('[data-panel="clarification"]').exists()).toBe(false)
    expect(body).not.toHaveProperty('clarification_pairs')
  })
})
