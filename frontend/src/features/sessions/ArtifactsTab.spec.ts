import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSessionToken, type ArtifactInfo } from '@/api/client'
import { server } from '@/test/server'

import ArtifactsTab from './ArtifactsTab.vue'

// D4-7（#234）：产物分区。产物正文走 content 端点，复制走剪贴板，下载走
// `<a download>` 的 content 地址；媒体产物用通用 `<video>`（不按文件名特判）。
enableAutoUnmount(afterEach)

function makeArtifact(name: string, kind: string): ArtifactInfo {
  return {
    artifact_id: `sess-1/${name}`,
    name,
    kind,
    bytes: 128,
    created_at: '2026-11-11T00:00:00Z',
  }
}

const ARTIFACTS: ArtifactInfo[] = [
  makeArtifact('script.md', 'script'),
  makeArtifact('shot-table.md', 'shot_table'),
  makeArtifact('video-prompt.md', 'video_prompt'),
  makeArtifact('summary-zh.md', 'summary_markdown'),
  makeArtifact('summary-zh.json', 'summary_json'),
]

const CONTENT: Record<string, string> = {
  'script.md': '分场剧本正文',
  'shot-table.md': '分镜表正文',
  'video-prompt.md': '# H3 视频提示词\n[Shot 1] At 00:00.000, a medium shot.',
  'summary-zh.md': '中文摘要正文',
  'summary-zh.json': '{"title":"摘要"}',
}

const writeText = vi.fn<(text: string) => Promise<void>>()

function serveContent(): void {
  server.use(
    http.get(/\/api\/v1\/artifacts\/.+\/content$/, ({ request }) => {
      // 产物标识可能整段被编码成 `sess-1%2Fscript.md`，也可能保留斜杠：
      // 都按「最后一段是文件名」取。
      const encodedId = new URL(request.url).pathname.split('/').at(-2) ?? ''
      const id = decodeURIComponent(encodedId)
      const name = id.split('/').at(-1) ?? ''
      return HttpResponse.text(CONTENT[name] ?? '')
    }),
  )
}

async function mountTab(artifacts: ArtifactInfo[] = ARTIFACTS) {
  const wrapper = mount(ArtifactsTab, { props: { sessionId: 'sess-1', artifacts } })
  await flushPromises()
  return { wrapper }
}

beforeEach(() => {
  resetSessionToken()
  writeText.mockReset()
  writeText.mockResolvedValue(undefined)
  // jsdom 不实现剪贴板；组件只认 navigator.clipboard.writeText 这一条路。
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
  serveContent()
})

describe('产物分区', () => {
  it('按中间产物与交付文本分组，并从 content 端点读回正文', async () => {
    const { wrapper } = await mountTab()

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('分场剧本正文')
      expect(wrapper.text()).toContain('分镜表正文')
      expect(wrapper.text()).toContain('# H3 视频提示词')
      expect(wrapper.text()).toContain('中文摘要正文')
    })

    expect(wrapper.get('[data-group="intermediate"]').text()).toContain('分场剧本')
    expect(wrapper.get('[data-group="deliverable"]').text()).toContain('最终提示词')
  })

  it('复制最终提示词写入剪贴板并给出成功反馈', async () => {
    const { wrapper } = await mountTab()
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('# H3 视频提示词')
    })

    const copyButton = wrapper.get('[data-copy="video-prompt.md"]')
    await copyButton.trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(CONTENT['video-prompt.md'])
    expect(copyButton.text()).toContain('已复制')
  })

  it('下载链接指向 content 端点并带 download 文件名', async () => {
    const { wrapper } = await mountTab()
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('# H3 视频提示词')
    })

    const link = wrapper.get('[data-download="video-prompt.md"]')
    expect(link.attributes('href')).toBe(
      'http://localhost/api/v1/artifacts/sess-1/video-prompt.md/content',
    )
    expect(link.attributes('download')).toBe('video-prompt.md')

    // 下载由浏览器接管，前端只承诺「点击后给出成功反馈」。
    // jsdom 不实现导航：先挂一个 preventDefault，避免用例打出一屏 NotFound 噪声。
    link.element.addEventListener('click', (event) => event.preventDefault())
    await link.trigger('click')
    expect(wrapper.text()).toContain('已开始下载')
  })

  it('媒体产物通用播放：按 kind 渲染 video，不按文件名特判', async () => {
    const { wrapper } = await mountTab([...ARTIFACTS, makeArtifact('any-name.mov', 'video')])
    await flushPromises()

    const video = wrapper.get('video')
    expect(video.attributes('controls')).toBeDefined()
    expect(video.attributes('src')).toBe(
      'http://localhost/api/v1/artifacts/sess-1/any-name.mov/content',
    )
  })

  it('没有产物时显示空态', async () => {
    const { wrapper } = await mountTab([])
    expect(wrapper.text()).toContain('暂无产物')
  })
})
