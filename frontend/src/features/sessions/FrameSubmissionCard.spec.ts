import { flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, type ActionItemOut, type ArtifactInfo, type Asset } from '@/api/client'
import { server } from '@/test/server'

import FrameSubmissionCard from './FrameSubmissionCard.vue'

// 帧提交卡（D4 spec §9）：出现时展示首/尾帧提示词，帧槽只列本会话素材；
// 双空提交是「显式跳过」，必须先过一道降级确认。
const item: ActionItemOut = {
  item_id: 'item-frame',
  kind: 'frame_submission',
  status: 'pending',
  created_at: '2026-11-11T00:00:00Z',
  resolved_at: '',
  reason: '',
}

const firstImage: Asset = {
  asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
  name: 'first.png',
  kind: 'image',
  bytes: 1024,
  created_at: '2026-11-11T00:00:00Z',
}
const lastImage: Asset = {
  asset_id: 'sess-1/bbbbbbbbbbbbbbbb',
  name: 'last.png',
  kind: 'image',
  bytes: 2048,
  created_at: '2026-11-11T00:00:00Z',
}
const video: Asset = {
  asset_id: 'sess-1/cccccccccccccccc',
  name: 'clip.webm',
  kind: 'video',
  bytes: 4096,
  created_at: '2026-11-11T00:00:00Z',
}
const assets = [firstImage, lastImage, video]

function framePrompt(name: string): ArtifactInfo {
  return {
    artifact_id: `sess-1/${name}`,
    name,
    kind: 'frame_prompt',
    bytes: 128,
    created_at: '2026-11-11T00:00:00Z',
  }
}

const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-frame/resolve'

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function mountCard(artifacts: ArtifactInfo[] = []) {
  return mount(FrameSubmissionCard, {
    props: { sessionId: 'sess-1', item, assets, artifacts },
  })
}

beforeEach(() => resetSessionToken())

describe('FrameSubmissionCard 帧提示词与选择器', () => {
  it('展示首/尾帧提示词；两个帧槽都只列本会话的图片素材', async () => {
    server.use(
      // 生成客户端把标识里的斜杠编码成 %2F（服务端解码后再路由）；MSW 的 `:param`
      // 正好匹配这一整段，命中后按文件区分返回哪一段提示词。
      http.get('http://localhost/api/v1/artifacts/:artifact/content', ({ params }) => {
        const artifact = String(params.artifact)
        return HttpResponse.text(
          artifact.includes('first') ? '首帧提示词：雨夜街口中景' : '尾帧提示词：人物收伞回望',
          { headers: { 'Content-Type': 'text/plain' } },
        )
      }),
    )
    const wrapper = mountCard([
      framePrompt('first-frame-prompt.md'),
      framePrompt('last-frame-prompt.md'),
    ])
    await settle()

    expect(wrapper.text()).toContain('首帧提示词：雨夜街口中景')
    expect(wrapper.text()).toContain('尾帧提示词：人物收伞回望')

    const selects = wrapper.findAll('select')
    expect(selects).toHaveLength(2)
    for (const select of selects) {
      const optionTexts = select.findAll('option').map((option) => option.text())
      expect(optionTexts).toContain('first.png')
      expect(optionTexts).toContain('last.png')
      // 帧槽在服务端只接受图片（frame_invalid），选择器不列视频/音频。
      expect(optionTexts).not.toContain('clip.webm')
    }
  })
})

describe('FrameSubmissionCard 提交', () => {
  it('选好首/尾帧后提交，body 带上本会话素材标识并通知父组件刷新', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()

    const selects = wrapper.findAll('select')
    await selects[0]!.setValue(firstImage.asset_id)
    await selects[1]!.setValue(lastImage.asset_id)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交帧并继续')!
      .trigger('click')
    await settle()

    expect(bodies).toEqual([{ frames: { first: firstImage.asset_id, last: lastImage.asset_id } }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
    expect(wrapper.text()).toContain('阶段 2')
  })

  it('双空提交先弹降级确认，确认后才发请求（首尾都为 null = 显式跳过）', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交帧并继续')!
      .trigger('click')
    await settle()

    // 还没发请求：必须先让用户看见「降级为纯文字」。
    expect(bodies).toHaveLength(0)
    expect(wrapper.text()).toContain('降级为纯文字')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '确认提交')!
      .trigger('click')
    await settle()

    expect(bodies).toEqual([{ frames: { first: null, last: null } }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })
})
