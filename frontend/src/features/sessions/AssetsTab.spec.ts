import { enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, uploadSessionAsset } from '@/api/client'
import { server } from '@/test/server'
import type { ActionItemOut, Asset, RefOut } from '@/api/client'

import AssetsTab from './AssetsTab.vue'

// 素材分区的用例只关心「渲染出什么、点了发什么」；依赖的 API 用 MSW 逐条注入。
enableAutoUnmount(afterEach)

const uploadUrl = 'http://localhost/api/v1/sessions/sess-1/assets'

function makeAsset(overrides: Partial<Asset> = {}): Asset {
  return {
    asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
    name: '素材.png',
    kind: 'image',
    bytes: 1024,
    created_at: '2026-11-11T00:00:00Z',
    ...overrides,
  }
}

function makeRef(overrides: Partial<RefOut> = {}): RefOut {
  return {
    name: '橘猫',
    kind: 'picture',
    number: 1,
    asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
    status: 'pending',
    source: 'derived',
    ...overrides,
  }
}

/** 未交图的派生行：台账里有名字与编号，但还没有正本（asset_id 为空）。 */
function derivedRef(number: number, name: string, overrides: Partial<RefOut> = {}): RefOut {
  return makeRef({ number, name, asset_id: '', status: 'pending', source: 'derived', ...overrides })
}

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

beforeEach(() => resetSessionToken())

describe('AssetsTab 素材列表与预览', () => {
  it('按类型渲染预览：图片 <img>、视频 <video>、音频 <audio>，且不提供删除', async () => {
    const assets = [
      makeAsset({ asset_id: 'sess-1/img000000000000', name: 'first.png', kind: 'image' }),
      makeAsset({ asset_id: 'sess-1/vid000000000000', name: 'clip.webm', kind: 'video' }),
      makeAsset({ asset_id: 'sess-1/aud000000000000', name: 'voice.mp3', kind: 'audio' }),
    ]
    const wrapper = mount(AssetsTab, {
      props: { sessionId: 'sess-1', assets },
    })
    await settle()

    // 媒体请求走 Cookie 认证的媒体端点；src 里只带素材标识，不带令牌（ADR 0033）。
    const image = wrapper.find('img')
    expect(image.exists()).toBe(true)
    expect(image.attributes('src')).toBe('http://localhost/api/v1/media/sess-1/img000000000000')
    expect(image.attributes('alt')).toBe('first.png')

    const video = wrapper.find('video')
    expect(video.exists()).toBe(true)
    expect(video.attributes('src')).toBe('http://localhost/api/v1/media/sess-1/vid000000000000')

    const audio = wrapper.find('audio')
    expect(audio.exists()).toBe(true)
    expect(audio.attributes('src')).toBe('http://localhost/api/v1/media/sess-1/aud000000000000')

    // 三种素材名都在列表里；D4-AC13 明确不提供删除。
    for (const name of ['first.png', 'clip.webm', 'voice.mp3']) {
      expect(wrapper.text()).toContain(name)
    }
    expect(wrapper.findAll('button').some((button) => button.text().includes('删除'))).toBe(false)
  })

  it('空列表给出提示，不渲染媒体元素', async () => {
    const wrapper = mount(AssetsTab, {
      props: { sessionId: 'sess-1', assets: [] },
    })
    await settle()

    expect(wrapper.text()).toContain('还没有素材')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.find('audio').exists()).toBe(false)
  })
})

describe('AssetsTab 上传（不走台账）', () => {
  it('选文件即上传到 D3 端点，成功后通知父组件刷新快照', async () => {
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json(makeAsset({ name: '新素材.png' }), { status: 201 })
      }),
    )
    const wrapper = mount(AssetsTab, {
      props: { sessionId: 'sess-1', assets: [] },
    })

    const input = wrapper.find('input[type="file"]')
    const file = new File([new Uint8Array([1, 2, 3])], '新素材.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()

    expect(uploads).toBe(1)
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('上传失败显示 role=alert 且不通知刷新', async () => {
    server.use(
      http.post(uploadUrl, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '不支持的素材类型',
            status: 415,
            detail: '魔数不在白名单',
            instance: uploadUrl,
          },
          { status: 415, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const wrapper = mount(AssetsTab, {
      props: { sessionId: 'sess-1', assets: [] },
    })

    const input = wrapper.find('input[type="file"]')
    const file = new File([new Uint8Array([9, 9, 9])], 'bad.bin', {
      type: 'application/octet-stream',
    })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()

    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.emitted('refresh')).toBeUndefined()
  })
})

describe('AssetsTab ref 三态（#283）', () => {
  it('created：没有上传框与语义名输入，只写「阶段 1 完成后会列出 N 件」', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'created',
        phase: 'created',
        assets: [],
        refs: [],
      },
    })
    await settle()

    expect(wrapper.find('[data-testid="assets-guidance"]').text()).toContain(
      '阶段 1 完成后这里会列出本片需要的',
    )
    // 起跑前不收图（#279）：上传框、语义名输入、容量条都不该出现。
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.find('input[name="name"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="参考素材容量"]').exists()).toBe(false)
  })

  it('asset_loop：列出全部 N 行，当前件指向待办卡，放弃件没有图', async () => {
    const refs = [
      derivedRef(1, '红裙女孩', { asset_id: 'sess-1/pass000000000001', status: 'passed' }),
      derivedRef(2, '旧木船', { status: 'abandoned' }),
      derivedRef(3, '码头'),
      derivedRef(4, '海雾'),
    ]
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'awaiting_human',
        phase: 'asset_loop',
        currentNumber: 3,
        assets: [makeAsset({ asset_id: 'sess-1/pass000000000001', name: '红裙女孩.png' })],
        refs,
      },
    })
    await settle()

    const rows = wrapper.findAll('.asset-list .asset')
    expect(rows).toHaveLength(4)
    expect(rows.map((row) => row.attributes('data-number'))).toEqual(['1', '2', '3', '4'])

    // 已定稿：缩略图 + 「已定稿」。
    expect(rows[0]!.attributes('data-status')).toBe('passed')
    expect(rows[0]!.find('img').exists()).toBe(true)
    expect(rows[0]!.text()).toContain('已定稿')

    // 已放弃：显示「已放弃」，且不画缩略图（这一件永远不会有正本）。
    expect(rows[1]!.attributes('data-status')).toBe('abandoned')
    expect(rows[1]!.text()).toContain('已放弃')
    expect(rows[1]!.find('img').exists()).toBe(false)

    // 当前件：既没定稿也没放弃的第一件，且能直接跳到待办卡。
    expect(rows[2]!.attributes('data-current')).toBe('true')
    expect(rows[2]!.text()).toContain('正在等你交图')
    expect(rows[3]!.attributes('data-current')).toBeUndefined()
    expect(rows[3]!.text()).toContain('还没轮到')
    expect(wrapper.findAll('.asset__goto')).toHaveLength(1)

    await wrapper.find('.asset__goto').trigger('click')
    expect(wrapper.emitted('gotoReview')).toHaveLength(1)
  })

  it('已交图待校对的那件显示「校对中」，且不再算「在等你交」', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'running',
        phase: 'asset_loop',
        assets: [],
        refs: [
          derivedRef(1, '红裙女孩', { status: 'registered', asset_id: 'sess-1/a000000000000001' }),
          derivedRef(2, '码头'),
        ],
      },
    })
    await settle()

    const rows = wrapper.findAll('.asset-list .asset')
    expect(rows[0]!.text()).toContain('校对中')
    expect(rows[0]!.attributes('data-current')).toBeUndefined()
    expect(wrapper.find('.asset__goto').exists()).toBe(false)
  })

  it('asset_loop：放弃件不算「当前件」，指针落在它后面那件上', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'awaiting_human',
        phase: 'asset_loop',
        currentNumber: 3,
        assets: [],
        refs: [
          derivedRef(1, '红裙女孩', { status: 'passed', asset_id: 'sess-1/a000000000000001' }),
          derivedRef(2, '旧木船', { status: 'abandoned' }),
          derivedRef(3, '码头'),
        ],
      },
    })
    await settle()

    const rows = wrapper.findAll('.asset-list .asset')
    expect(rows[1]!.attributes('data-current')).toBeUndefined()
    expect(rows[2]!.attributes('data-current')).toBe('true')
    expect(rows[2]!.text()).toContain('正在等你交图')
  })

  it('整个 ref 分区没有任何改名改号入口（只读）', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'awaiting_human',
        phase: 'asset_loop',
        assets: [],
        refs: [derivedRef(1, '红裙女孩')],
      },
    })
    await settle()

    // 语义名与编号由资产图词决定：不给输入框、不给改号按钮、不给上传口。
    expect(wrapper.findAll('input')).toHaveLength(0)
    expect(wrapper.find('[data-testid="assets-readonly"]').text()).toContain('只读')
    for (const forbidden of ['改名', '重命名', '改编号', '删除']) {
      expect(wrapper.findAll('button').some((button) => button.text().includes(forbidden))).toBe(
        false,
      )
    }
  })

  it('循环结束：列出 N 行与容量条，不再有当前件', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'running',
        phase: 'after',
        assets: [],
        refs: [
          derivedRef(1, '红裙女孩', { status: 'passed', asset_id: 'sess-1/a000000000000001' }),
          derivedRef(2, '旧木船', { status: 'passed', asset_id: 'sess-1/a000000000000002' }),
          makeRef({
            kind: 'video',
            number: 1,
            name: '运镜参考',
            asset_id: 'sess-1/v000000000000001',
            status: 'registered',
            source: 'uploaded',
          }),
        ],
      },
    })
    await settle()

    expect(wrapper.find('[data-testid="assets-summary"]').text()).toContain('本片共 2 件参考素材')
    expect(wrapper.find('[data-testid="assets-summary"]').text()).toContain('已定稿 2 件')
    expect(wrapper.find('.asset__goto').exists()).toBe(false)
    expect(wrapper.findAll('.asset[data-current="true"]')).toHaveLength(0)
    expect(wrapper.find('[aria-label="参考素材容量"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('运镜参考')
  })

  it('零资产：循环结束态说明本片没有参考素材，不报没有素材', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'running',
        phase: 'after',
        assets: [],
        refs: [],
      },
    })
    await settle()

    expect(wrapper.find('[data-testid="assets-summary"]').text()).toContain('本片没有参考素材')
    expect(wrapper.text()).not.toContain('本会话还没有素材')
  })
})

describe('AssetsTab ref 容量条（#283 AC4）', () => {
  it('任一档超限即红，并点名超的是哪一档', async () => {
    const pictures = Array.from({ length: 10 }, (_, index) =>
      derivedRef(index + 1, `图${index}`, {
        asset_id: `sess-1/p${index}`,
        status: 'passed',
      }),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'running',
        phase: 'after',
        assets: [],
        refs: pictures,
      },
    })

    const picture = wrapper.find('[data-kind="picture"]')
    expect(picture.attributes('data-over')).toBe('true')
    expect(picture.classes()).toContain('capacity__over')
    expect(picture.text()).toContain('10/9')
    // 总文件 10/12 仍未超：12 是独立上限，不是三型之和。
    expect(wrapper.find('[data-kind="files"]').attributes('data-over')).toBe('false')
    const hint = wrapper.find('.capacity-hint')
    expect(hint.text()).toContain('图 10/9')
    // 只说超的那一档：没超的档不该出现在点名文案里。
    expect(hint.text()).not.toContain('视频')
  })

  it('已放弃的行不计入用量（它们永远不会有正本）', async () => {
    const refs = Array.from({ length: 9 }, (_, index) =>
      derivedRef(index + 1, `图${index}`, { asset_id: `sess-1/p${index}`, status: 'passed' }),
    )
    refs.push(derivedRef(10, '画不出的猫', { status: 'abandoned' }))
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'running',
        phase: 'after',
        assets: [],
        refs,
      },
    })

    const picture = wrapper.find('[data-kind="picture"]')
    expect(picture.text()).toContain('9/9')
    expect(picture.attributes('data-over')).toBe('false')
    expect(wrapper.find('.capacity-hint').exists()).toBe(false)
  })
})

describe('AssetsTab 导演台 r2v：复用 ref 的台账界面（#268）', () => {
  it('r2v 显示语义名输入与容量条，列表按台账渲染', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'director',
        directorMode: 'r2v',
        status: 'created',
        assets: [makeAsset({ name: '橘猫.png' })],
        refs: [makeRef({ number: 1 })],
      },
    })
    await settle()

    expect(wrapper.find('input[name="name"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="参考素材容量"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('橘猫')
    expect(wrapper.text()).toContain('picture 1')
  })

  it('t2v/i2v/fl2v 没有台账：不显示语义名输入与容量条', async () => {
    for (const directorMode of ['t2v', 'i2v', 'fl2v']) {
      const wrapper = mount(AssetsTab, {
        props: {
          sessionId: 'sess-1',
          mode: 'director',
          directorMode,
          status: 'created',
          assets: [makeAsset({ name: '镜头.png' })],
          refs: [],
        },
      })
      await settle()

      expect(wrapper.find('input[name="name"]').exists()).toBe(false)
      expect(wrapper.find('[aria-label="参考素材容量"]').exists()).toBe(false)
      expect(wrapper.text()).toContain('镜头.png')
    }
  })
})

describe('AssetsTab r2v 上传（起跑前登记，形态不变）', () => {
  it('按台账显示语义名/类型/编号，上传成功后通知刷新', async () => {
    server.use(
      http.post(uploadUrl, () =>
        HttpResponse.json(makeAsset({ name: '橘猫.png' }), { status: 201 }),
      ),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'director',
        directorMode: 'r2v',
        status: 'created',
        assets: [makeAsset({ name: '橘猫.png' })],
        refs: [makeRef({ number: 2 })],
      },
    })
    await wrapper.find('input[name="name"]').setValue('橘猫')
    const input = wrapper.find('input[type="file"]')
    const file = new File([new Uint8Array([1, 2, 3])], '橘猫.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()

    expect(wrapper.emitted('refresh')).toHaveLength(1)
    // 列表用台账的名字与编号，不是上传文件名。
    expect(wrapper.text()).toContain('橘猫')
    expect(wrapper.text()).toContain('picture 2')
  })

  it('语义名随 multipart 一起发出（FormData 层）', async () => {
    // jsdom 的 FormData 与 Node fetch 的 undici 不是同一个实现，请求体在 MSW 里读不
    // 出来；这里拦 fetch 直接看最终交给它的 FormData，判据仍在客户端行为上。
    const original = globalThis.fetch
    const bodies: FormData[] = []
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/api/v1/auth/token')) {
        return new Response(JSON.stringify({ token: 'token' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      bodies.push(init?.body as FormData)
      return new Response(JSON.stringify(makeAsset({ name: '橘猫.png' })), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch
    try {
      await uploadSessionAsset(
        'sess-1',
        new File([new Uint8Array([1])], '橘猫.png', { type: 'image/png' }),
        '橘猫',
      )
    } finally {
      globalThis.fetch = original
    }

    expect(bodies).toHaveLength(1)
    expect(bodies[0].get('name')).toBe('橘猫')
    expect((bodies[0].get('file') as File).name).toBe('橘猫.png')
  })

  it('语义名为空时不上传，直接提示', async () => {
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json(makeAsset(), { status: 201 })
      }),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'director',
        directorMode: 'r2v',
        status: 'created',
        assets: [],
        refs: [],
      },
    })
    const input = wrapper.find('input[type="file"]')
    const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()

    expect(uploads).toBe(0)
    expect(wrapper.find('[role="alert"]').text()).toContain('语义名')
  })

  it('起跑后隐藏上传与删除，并说明原因（r2v 时间锁照旧）', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'director',
        directorMode: 'r2v',
        status: 'running',
        assets: [makeAsset()],
        refs: [makeRef({ status: 'passed' })],
      },
    })

    expect((wrapper.find('input[type="file"]').element as HTMLInputElement).disabled).toBe(true)
    expect(wrapper.findAll('button').some((button) => button.text().includes('删除'))).toBe(false)
    expect(wrapper.find('[data-testid="assets-locked"]').exists()).toBe(true)
  })

  it('删除调 DELETE 端点并通知父组件刷新', async () => {
    let deletedUrl: string | null = null
    server.use(
      http.delete('http://localhost/api/v1/sessions/sess-1/assets/sess-1/aaaaaaaaaaaaaaaa', () => {
        deletedUrl = 'called'
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'director',
        directorMode: 'r2v',
        status: 'created',
        assets: [makeAsset()],
        refs: [makeRef()],
      },
    })

    const remove = wrapper.findAll('button').find((button) => button.text().includes('删除'))
    await remove!.trigger('click')
    await settle()

    expect(deletedUrl).toBe('called')
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })
})

describe('AssetsTab 换一张图（#279 US11 / #282）', () => {
  // 生成客户端把素材标识里的斜杠编码成 %2F（服务端解码后再按 {asset_id:path} 路由），
  // MSW 的 `:assetId` 正好匹配这一整段；这里连带钉住「发出去的是哪个标识」。
  const reopenUrl = 'http://localhost/api/v1/sessions/sess-1/assets/:assetId/reopen'

  function makeReopenedItem(overrides: Partial<ActionItemOut> = {}): ActionItemOut {
    return {
      item_id: 'item-reopen',
      kind: 'asset_image',
      status: 'pending',
      created_at: '2026-11-15T00:00:00Z',
      resolved_at: '',
      reason: '',
      asset_index: 1,
      ...overrides,
    }
  }

  it('循环中已定稿行提供「换一张图」，POST 打回端点并通知刷新', async () => {
    const calls: { url: string; key: string | null; body: string }[] = []
    server.use(
      http.post(reopenUrl, async ({ request }) => {
        calls.push({
          url: request.url,
          key: request.headers.get('idempotency-key'),
          body: await request.text(),
        })
        return HttpResponse.json(makeReopenedItem())
      }),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        assets: [],
        mode: 'ref',
        phase: 'asset_loop',
        refs: [
          derivedRef(1, '橘猫', { asset_id: 'sess-1/aaaaaaaaaaaaaaaa', status: 'passed' }),
          derivedRef(2, '台灯'),
        ],
      },
    })

    // 只有已定稿那一行有出口：还没轮到的那件没有可换的图。
    const buttons = wrapper.findAll('button').filter((button) => button.text() === '换一张图')
    expect(buttons).toHaveLength(1)
    await buttons[0].trigger('click')
    await settle()

    expect(calls).toHaveLength(1)
    expect(calls[0].url).toContain('/assets/sess-1%2Faaaaaaaaaaaaaaaa/reopen')
    expect(calls[0].key).toBeTruthy()
    expect(calls[0].body).toBe('')
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('循环结束后不再提供换图入口（后端也会拒）', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        assets: [],
        mode: 'ref',
        phase: 'after',
        refs: [derivedRef(1, '橘猫', { asset_id: 'sess-1/aaaaaaaaaaaaaaaa', status: 'passed' })],
      },
    })

    expect(wrapper.findAll('button').some((button) => button.text() === '换一张图')).toBe(false)
  })

  it.each([
    [
      'asset_reopen_unsupported',
      '素材循环已经结束：换图会让注入卡与段内子集和这份台账对不上，那属于循环之外的动作。',
    ],
    [
      'asset_not_reopenable',
      '第 1 件已经放弃：放弃件没有图可换，要么在修订里改词重跑，要么让它保持无图。',
    ],
  ])('%s 的拒绝把后端原因原样显示出来', async (code, detail) => {
    server.use(
      http.post(reopenUrl, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '不能换图',
            status: 409,
            code,
            detail,
            instance: '',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        assets: [],
        mode: 'ref',
        phase: 'asset_loop',
        refs: [derivedRef(1, '橘猫', { asset_id: 'sess-1/aaaaaaaaaaaaaaaa', status: 'passed' })],
      },
    })

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '换一张图')!
      .trigger('click')
    await settle()

    expect(wrapper.find('[data-testid="reopen-error"]').text()).toContain(detail)
    // 两种拒绝各有出路，不许统一成「操作失败」。
    expect(wrapper.text()).not.toContain('请求失败，请稍后重试。')
    expect(wrapper.emitted('refresh')).toBeUndefined()
  })
})
