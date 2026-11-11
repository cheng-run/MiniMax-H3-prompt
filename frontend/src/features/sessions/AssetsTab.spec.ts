import { enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken } from '@/api/client'
import { server } from '@/test/server'
import type { Asset } from '@/api/client'

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
