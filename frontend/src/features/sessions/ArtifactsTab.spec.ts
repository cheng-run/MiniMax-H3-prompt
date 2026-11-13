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

  it('按 kind 分组：登记表换了文件名也照常显示，不抄文件名白名单', async () => {
    server.use(
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () => HttpResponse.text('换名后的剧本正文')),
    )
    const { wrapper } = await mountTab([
      makeArtifact('draft-notes.md', 'script'),
      makeArtifact('summary-final.md', 'summary_markdown'),
    ])

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('换名后的剧本正文')
    })
    expect(wrapper.get('[data-group="intermediate"]').text()).toContain('剧本')
    expect(wrapper.get('[data-group="deliverable"]').text()).toContain('中文摘要')
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

  it('段提示词（segment_prompt）有分组有标签，不许静默消失', async () => {
    // D6-AC8：新 kind 若不在分组表里，`artifactGroup` 返回 null，整件产物从界面上消失。
    server.use(
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () => HttpResponse.text('第 1 段：雨夜街口')),
    )
    const { wrapper } = await mountTab([
      ...ARTIFACTS,
      makeArtifact('segments/shot-01.md', 'segment_prompt'),
    ])

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('第 1 段：雨夜街口')
    })
    expect(wrapper.get('[data-group="deliverable"]').text()).toContain('段提示词')
    expect(wrapper.find('[data-copy="segments/shot-01.md"]').exists()).toBe(true)
  })

  it('整片注入卡（injection_card）进交付文本，只读可复制、没有编辑入口', async () => {
    // D7-4（#257）：卡是产物分区里的一件参考文本，随流程自动产出——页面上没有
    // 「生成卡片」按钮，也不提供编辑控件。
    server.use(
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () =>
        HttpResponse.text('# 注入卡\n| ref_image_0 | 橘猫 |'),
      ),
    )
    const { wrapper } = await mountTab([makeArtifact('injection-card.md', 'injection_card')])

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('ref_image_0')
    })
    expect(wrapper.get('[data-group="deliverable"]').text()).toContain('注入卡')
    expect(wrapper.find('[data-copy="injection-card.md"]').exists()).toBe(true)
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.find('[contenteditable="true"]').exists()).toBe(false)
  })

  it('ref 三件只读产物各有分组：资产图词、多参分镜表、拼接顺序卡都不静默消失', async () => {
    // D7 §11 / §19：登记表新增三类 kind 后，分组表漏登记 → `artifactGroup` 返回 null，
    // 整件产物从产物分区消失（用户拿不到资产图词、分镜表与拼接顺序卡）。
    server.use(
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () => HttpResponse.text('只读产物正文')),
    )
    const { wrapper } = await mountTab([
      makeArtifact('assets/picture-01.md', 'asset_prompt'),
      makeArtifact('segments/ref-shot-table.md', 'ref_shot_table'),
      makeArtifact('segments/order-card.md', 'concat_order_card'),
    ])

    await vi.waitFor(() => {
      expect(wrapper.findAll('pre.file-text')).toHaveLength(3)
    })
    expect(wrapper.get('[data-group="intermediate"]').text()).toContain('资产图词')
    expect(wrapper.get('[data-group="intermediate"]').text()).toContain('多参分镜表')
    // 拼接顺序卡是交付前必须照做的清单，与注入卡同类，归交付文本（见 artifactKinds 注释）。
    expect(wrapper.get('[data-group="deliverable"]').text()).toContain('拼接顺序卡')
  })

  it('阅读顺序把 ref 两件插在阶段 1 相邻位置：分镜表 → 多参分镜表 → 帧词 → 资产图词', async () => {
    server.use(http.get(/\/api\/v1\/artifacts\/.+\/content$/, () => HttpResponse.text('正文')))
    // 故意打乱传入顺序：排序必须由 kind 顺序决定，不是「传入的先后」。
    const { wrapper } = await mountTab([
      makeArtifact('assets/picture-01.md', 'asset_prompt'),
      makeArtifact('first-frame-prompt.md', 'frame_prompt'),
      makeArtifact('segments/ref-shot-table.md', 'ref_shot_table'),
      makeArtifact('shot-table.md', 'shot_table'),
    ])

    await vi.waitFor(() => {
      expect(wrapper.findAll('pre.file-text')).toHaveLength(4)
    })
    const names = wrapper
      .findAll('[data-group="intermediate"] article .file-name')
      .map((node) => node.text())
    expect(names).toEqual([
      'shot-table.md',
      'segments/ref-shot-table.md',
      'first-frame-prompt.md',
      'assets/picture-01.md',
    ])
  })

  it('没有产物时显示空态', async () => {
    const { wrapper } = await mountTab([])
    expect(wrapper.text()).toContain('暂无产物')
  })
})

// D8-2（#262）：导演台段词是交付文本，按 kind 自动进产物分区，不需要前端抄文件名。
describe('ArtifactsTab 导演台段词（#262）', () => {
  it('director_segment_prompt 归到交付文本并显示中文标签', async () => {
    const { wrapper } = await mountTab([
      makeArtifact('director/segment-01.md', 'director_segment_prompt'),
    ])

    expect(wrapper.text()).toContain('导演台段词')
    expect(wrapper.text()).toContain('director/segment-01.md')
  })
})
