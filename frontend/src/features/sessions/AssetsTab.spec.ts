import { enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, uploadSessionAsset } from '@/api/client'
import { server } from '@/test/server'
import type { Asset, RefOut } from '@/api/client'

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
    ...overrides,
  }
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

describe('AssetsTab 上传', () => {
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

describe('AssetsTab ref：语义名、台账与容量', () => {
  it('按台账显示语义名/类型/编号，上传成功后通知刷新', async () => {
    server.use(
      http.post(uploadUrl, () =>
        HttpResponse.json(makeAsset({ name: '橘猫.png' }), { status: 201 }),
      ),
    )
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
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
      props: { sessionId: 'sess-1', mode: 'ref', status: 'created', assets: [], refs: [] },
    })
    const input = wrapper.find('input[type="file"]')
    const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()

    expect(uploads).toBe(0)
    expect(wrapper.find('[role="alert"]').text()).toContain('语义名')
  })

  it('容量任一档超限即红（9 图 / 3 视频 / 3 音频 / 12 文件）', async () => {
    const pictures = Array.from({ length: 10 }, (_, index) =>
      makeRef({ name: `图${index}`, number: index + 1, asset_id: `sess-1/${index}` as string }),
    )
    const wrapper = mount(AssetsTab, {
      props: { sessionId: 'sess-1', mode: 'ref', status: 'created', assets: [], refs: pictures },
    })

    const picture = wrapper.find('[data-kind="picture"]')
    expect(picture.attributes('data-over')).toBe('true')
    expect(picture.classes()).toContain('capacity__over')
    expect(picture.text()).toContain('10/9')
    // 总文件 10/12 仍未超：12 是独立上限，不是三型之和。
    const files = wrapper.find('[data-kind="files"]')
    expect(files.attributes('data-over')).toBe('false')
    expect(wrapper.find('[role="status"]').text()).toContain('上限')
  })

  it('起跑后隐藏上传与删除，并说明原因', async () => {
    const wrapper = mount(AssetsTab, {
      props: {
        sessionId: 'sess-1',
        mode: 'ref',
        status: 'running',
        assets: [makeAsset()],
        refs: [makeRef()],
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
        mode: 'ref',
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
