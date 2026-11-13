import { flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  resetSessionToken,
  type ActionItemOut,
  type ArtifactInfo,
  type Asset,
  type SegmentOut,
} from '@/api/client'
import { server } from '@/test/server'

import SegmentsTab from './SegmentsTab.vue'

// 分段分区（D6-1 只读展示 + D6-4 段视频回交）：段号、时间区间、覆盖镜头、摘要、
// 状态与「x / y」进度都直接来自快照；回交只发 resolve，之后由快照（SSE 失效信号）
// 把新状态拉回来，组件不自己轮询。
function makeSegment(overrides: Partial<SegmentOut> = {}): SegmentOut {
  return {
    index: 1,
    start_s: 0,
    end_s: 6,
    shots_in_segment: [1, 2],
    summary: '开场：雨夜街口',
    end_hook: '人物停在旧楼门口',
    status: 'pending',
    prompt_artifact_id: '',
    video_asset_id: '',
    rejection_reason: '',
    assets: [],
    ...overrides,
  }
}

function makeItem(overrides: Partial<ActionItemOut> = {}): ActionItemOut {
  return {
    item_id: 'item-seg-1',
    kind: 'segment_video',
    status: 'pending',
    created_at: '2026-11-11T00:00:00Z',
    resolved_at: '',
    reason: '',
    segment_index: 1,
    ...overrides,
  }
}

function makeArtifact(name: string, kind: string): ArtifactInfo {
  return {
    artifact_id: `sess-1/${name}`,
    name,
    kind,
    bytes: 128,
    created_at: '2026-11-11T00:00:00Z',
  }
}

function mountTab(
  overrides: {
    mode?: string
    segments?: SegmentOut[]
    assets?: Asset[]
    actionItems?: ActionItemOut[]
    artifacts?: ArtifactInfo[]
  } = {},
) {
  return mount(SegmentsTab, {
    props: {
      sessionId: 'sess-1',
      mode: 'base',
      segments: [makeSegment()],
      assets: [],
      actionItems: [],
      artifacts: [],
      ...overrides,
    },
  })
}

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function stubClipboard(writeText: ReturnType<typeof vi.fn>): void {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
}

beforeEach(() => resetSessionToken())

describe('SegmentsTab 分段分区', () => {
  it('逐段显示段号、时间区间、覆盖镜头、摘要与状态，并给出「已通过 / 总数」', () => {
    const wrapper = mountTab({
      segments: [
        makeSegment(),
        makeSegment({
          index: 2,
          start_s: 6,
          end_s: 12,
          shots_in_segment: [3],
          summary: '收束：抬头看向楼内',
          end_hook: '灯亮起',
          status: 'passed',
        }),
      ],
    })

    const text = wrapper.text()
    expect(text).toContain('第 1 段')
    expect(text).toContain('0–6 秒')
    expect(text).toContain('镜头 1、镜头 2')
    expect(text).toContain('开场：雨夜街口')
    expect(text).toContain('待开始')
    expect(text).toContain('第 2 段')
    expect(text).toContain('6–12 秒')
    expect(text).toContain('已通过')
    expect(text).toContain('已通过 1 / 总数 2')
  })

  it('段循环的四种状态都有中文文案，未知状态原样显示', () => {
    const wrapper = mountTab({
      segments: [
        makeSegment({ status: 'pending' }),
        makeSegment({ index: 2, status: 'waiting_video' }),
        makeSegment({ index: 3, status: 'validating' }),
        makeSegment({ index: 4, status: 'passed' }),
        makeSegment({ index: 5, status: 'unknown-status' }),
      ],
    })

    const text = wrapper.text()
    expect(text).toContain('待开始')
    expect(text).toContain('等待交视频')
    expect(text).toContain('校验中')
    expect(text).toContain('已通过 1 / 总数 5')
    expect(text).toContain('unknown-status')
  })

  it('语义闸门拒绝后显示具体原因，并保留重交表单', () => {
    const wrapper = mountTab({
      segments: [
        makeSegment({
          status: 'waiting_video',
          rejection_reason: '尾帧未达到本段末态：尾帧画面：人物还在走动｜要求：人物停在门口',
        }),
      ],
      actionItems: [makeItem()],
    })

    const text = wrapper.text()
    expect(text).toContain('上次提交未通过')
    expect(text).toContain('尾帧未达到本段末态')
    expect(text).toContain('人物停在门口')
    // 被拒的段仍可重交：待办表单还在。
    expect(text).toContain('提交本段视频')
  })

  it('当前段高亮落在第一个未通过的段上；没有覆盖镜头时显示占位', () => {
    const wrapper = mountTab({
      segments: [
        makeSegment({ status: 'passed' }),
        makeSegment({ index: 2, start_s: 6, end_s: 12, shots_in_segment: [] }),
      ],
    })

    expect(wrapper.find('[data-segment-index="1"]').classes()).not.toContain('segment--current')
    expect(wrapper.find('[data-segment-index="2"]').classes()).toContain('segment--current')
    expect(wrapper.find('[data-segment-index="2"]').text()).toContain('—')
  })

  it('段通过后当前段高亮右移到下一段（完全由快照驱动）', async () => {
    const wrapper = mountTab({
      segments: [makeSegment(), makeSegment({ index: 2, start_s: 6, end_s: 12 })],
    })

    expect(wrapper.find('[data-segment-index="1"]').classes()).toContain('segment--current')
    expect(wrapper.find('[data-segment-index="2"]').classes()).not.toContain('segment--current')

    await wrapper.setProps({
      segments: [
        makeSegment({ status: 'passed' }),
        makeSegment({ index: 2, start_s: 6, end_s: 12, status: 'waiting_video' }),
      ],
    })

    expect(wrapper.find('[data-segment-index="1"]').classes()).not.toContain('segment--current')
    expect(wrapper.find('[data-segment-index="2"]').classes()).toContain('segment--current')
  })

  it('规划未完成时显示「规划中」占位与 0 / 0 进度，而不是空分区', () => {
    const wrapper = mountTab({ segments: [] })

    expect(wrapper.text()).toContain('规划中')
    expect(wrapper.text()).toContain('已通过 0 / 总数 0')
    expect(wrapper.find('ol').exists()).toBe(false)
  })
})

describe('SegmentsTab 段内参考素材（#258）', () => {
  it('ref 段显示本段只读参考素材：段内重编号 Picture 号 + 语义名', () => {
    const wrapper = mountTab({
      segments: [
        makeSegment({
          assets: [
            { number: 1, name: '橘猫', original_number: 2 },
            { number: 2, name: '台灯', original_number: 5 },
          ],
        }),
      ],
    })

    const text = wrapper.text()
    expect(text).toContain('本段参考素材')
    expect(text).toContain('Picture 1 橘猫')
    expect(text).toContain('Picture 2 台灯')
    // 只读：子集由系统算，界面没有任何手改入口。
    expect(wrapper.find('[data-segment-assets] input').exists()).toBe(false)
    expect(wrapper.find('[data-segment-assets] select').exists()).toBe(false)
  })

  it('没有段内子集的段（base）不显示这一行', () => {
    const wrapper = mountTab({ segments: [makeSegment({ assets: [] })] })

    expect(wrapper.find('[data-segment-assets]').exists()).toBe(false)
  })
})

describe('SegmentsTab 换一段（#258）', () => {
  function hasReopenButton(wrapper: ReturnType<typeof mountTab>): boolean {
    return wrapper.findAll('button').some((button) => button.text() === '换一段')
  }

  it('ref 的已通过段显示「换一段」', () => {
    const wrapper = mountTab({
      mode: 'ref',
      segments: [makeSegment({ status: 'passed' })],
    })

    expect(hasReopenButton(wrapper)).toBe(true)
  })

  it('base 的已通过段不显示「换一段」', () => {
    const wrapper = mountTab({
      mode: 'base',
      segments: [makeSegment({ status: 'passed' })],
    })

    expect(hasReopenButton(wrapper)).toBe(false)
  })

  it('ref 未通过的段不显示「换一段」', () => {
    const wrapper = mountTab({
      mode: 'ref',
      segments: [makeSegment({ status: 'waiting_video' })],
    })

    expect(hasReopenButton(wrapper)).toBe(false)
  })

  it('点「换一段」发 reopen 请求（带幂等键）并通知父组件刷新', async () => {
    const headers: (string | null)[] = []
    server.use(
      http.post(reopenUrl, ({ request }) => {
        headers.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json(makeItem({ status: 'pending', segment_index: 1 }))
      }),
    )

    const wrapper = mountTab({
      mode: 'ref',
      segments: [makeSegment({ status: 'passed' })],
    })
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '换一段')!
      .trigger('click')
    await settle()

    expect(headers).toHaveLength(1)
    expect(headers[0]).toBeTruthy()
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('reopen 被拒时原样呈现服务端原因', async () => {
    const detail = '会话有任务在跑，等它停下再换段。'
    server.use(
      http.post(reopenUrl, () =>
        HttpResponse.json(
          { type: 'about:blank', title: '冲突', status: 409, detail },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const wrapper = mountTab({
      mode: 'ref',
      segments: [makeSegment({ status: 'passed' })],
    })
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '换一段')!
      .trigger('click')
    await settle()

    expect(wrapper.text()).toContain(detail)
    // 失败后按钮仍可用：状态没变，用户可以再点一次。
    expect(hasReopenButton(wrapper)).toBe(true)
  })
})

describe('SegmentsTab 段提示词与已交视频', () => {
  it('提示词可查看、可复制，已通过段的视频可在页内播放', async () => {
    server.use(
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, () =>
        HttpResponse.text('第 1 段提示词：雨夜街口，镜头缓推。', {
          headers: { 'Content-Type': 'text/plain' },
        }),
      ),
    )
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)

    const wrapper = mountTab({
      segments: [
        makeSegment({
          status: 'passed',
          prompt_artifact_id: 'sess-1/segments/shot-01.md',
          video_asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
        }),
        makeSegment({ index: 2, start_s: 6, end_s: 12, status: 'waiting_video' }),
      ],
    })

    // 查看提示词：点开才读正文（content 端点），点开前不请求。
    expect(wrapper.text()).not.toContain('镜头缓推')
    const viewButton = wrapper.findAll('button').find((button) => button.text() === '查看提示词')!
    await viewButton.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('第 1 段提示词：雨夜街口，镜头缓推。')

    const copyButton = wrapper.findAll('button').find((button) => button.text() === '复制提示词')!
    await copyButton.trigger('click')
    await settle()
    expect(writeText).toHaveBeenCalledWith('第 1 段提示词：雨夜街口，镜头缓推。')
    expect(wrapper.findAll('button').some((button) => button.text() === '已复制')).toBe(true)

    // 已交视频在页内播放（媒体元素由浏览器自己取，走素材媒体地址）。
    const video = wrapper.find('video')
    expect(video.exists()).toBe(true)
    expect(video.attributes('src')).toContain('/api/v1/media/sess-1/')
    expect(video.attributes('src')).toContain('aaaaaaaaaaaaaaaa')
  })

  it('未写词的段没有提示词入口', () => {
    const wrapper = mountTab({ segments: [makeSegment({ prompt_artifact_id: '' })] })

    expect(wrapper.findAll('button').some((button) => button.text() === '查看提示词')).toBe(false)
  })
})

const mp4Asset: Asset = {
  asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
  name: 'shot-01.mp4',
  kind: 'video',
  bytes: 1024,
  created_at: '2026-11-11T00:00:00Z',
}
const mpegAsset: Asset = {
  asset_id: 'sess-1/bbbbbbbbbbbbbbbb',
  name: 'shot-01.mpeg',
  kind: 'video',
  bytes: 2048,
  created_at: '2026-11-11T00:00:00Z',
}
const mp4Asset2: Asset = {
  asset_id: 'sess-1/ffffffffffffffff',
  name: 'shot-01-retry.mp4',
  kind: 'video',
  bytes: 3072,
  created_at: '2026-11-11T00:00:00Z',
}
const webmAsset: Asset = {
  asset_id: 'sess-1/cccccccccccccccc',
  name: 'clip.webm',
  kind: 'video',
  bytes: 4096,
  created_at: '2026-11-11T00:00:00Z',
}
const imageAsset: Asset = {
  asset_id: 'sess-1/dddddddddddddddd',
  name: 'photo.png',
  kind: 'image',
  bytes: 128,
  created_at: '2026-11-11T00:00:00Z',
}

const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-seg-1/resolve'
const reopenUrl = 'http://localhost/api/v1/sessions/sess-1/segments/1/reopen'
const uploadUrl = 'http://localhost/api/v1/sessions/sess-1/assets'

describe('SegmentsTab 段视频回交', () => {
  it('素材选择只列本会话的 mp4；提交 body 带 video_asset_id 并通知父组件刷新', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )

    const wrapper = mountTab({
      segments: [makeSegment({ status: 'waiting_video' })],
      assets: [mp4Asset, mpegAsset, webmAsset, imageAsset],
      actionItems: [makeItem()],
    })

    const select = wrapper.find('select[name="segment-video"]')
    expect(select.exists()).toBe(true)
    const optionTexts = select.findAll('option').map((option) => option.text())
    expect(optionTexts).toContain('shot-01.mp4')
    // 只有 mp4 容器能作为段视频（服务端按 media_type 硬判）；图片/音频与其他容器不列。
    expect(optionTexts).not.toContain('shot-01.mpeg')
    expect(optionTexts).not.toContain('clip.webm')
    expect(optionTexts).not.toContain('photo.png')

    await select.setValue(mp4Asset.asset_id)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交视频')!
      .trigger('click')
    await settle()

    expect(bodies).toEqual([{ video_asset_id: mp4Asset.asset_id }])
    expect(wrapper.emitted('refresh')).toHaveLength(1)
    expect(wrapper.text()).toContain('已提交，正在校验')
  })

  it('也可以直接上传一个新的 mp4：上传后自动选中，再提交走同一条 resolve', async () => {
    const uploaded: Asset = {
      asset_id: 'sess-1/eeeeeeeeeeeeeeee',
      name: 'fresh.mp4',
      kind: 'video',
      bytes: 8192,
      created_at: '2026-11-11T00:00:00Z',
    }
    const bodies: unknown[] = []
    server.use(
      http.post(uploadUrl, () => HttpResponse.json(uploaded)),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )

    const wrapper = mountTab({
      segments: [makeSegment({ status: 'waiting_video' })],
      assets: [],
      actionItems: [makeItem()],
    })

    const input = wrapper.find('input[type="file"]')
    const file = new File(['video-bytes'], 'fresh.mp4', { type: 'video/mp4' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()

    // 上传成功即选中（不必回头再挑一次）；父组件刷新后素材列表里也会有它。
    expect((wrapper.find('select[name="segment-video"]').element as HTMLSelectElement).value).toBe(
      uploaded.asset_id,
    )
    expect(wrapper.emitted('refresh')).toHaveLength(1)

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交视频')!
      .trigger('click')
    await settle()

    expect(bodies).toEqual([{ video_asset_id: uploaded.asset_id }])
  })

  it('422 原样呈现服务端原因，表单保留可直接换素材重交', async () => {
    const detail = '本段视频不合格：帧数 90 不在 4 秒档（107 帧）允许的范围内。'
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        if (bodies.length === 1) {
          return HttpResponse.json(
            { type: 'about:blank', title: '段视频不合格', status: 422, detail },
            { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json(null, { status: 202 })
      }),
    )

    const wrapper = mountTab({
      segments: [makeSegment({ status: 'waiting_video' })],
      assets: [mp4Asset, mp4Asset2],
      actionItems: [makeItem()],
    })

    await wrapper.find('select[name="segment-video"]').setValue(mp4Asset.asset_id)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交视频')!
      .trigger('click')
    await settle()

    // 服务端给的 422 原因原样显示，不翻译成笼统文案。
    expect(wrapper.text()).toContain(detail)

    // 会话仍停在该待办：选择器还在，换一个素材可以立刻重交。
    const select = wrapper.find('select[name="segment-video"]')
    expect(select.exists()).toBe(true)
    await select.setValue(mp4Asset2.asset_id)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '提交视频')!
      .trigger('click')
    await settle()

    expect(bodies).toHaveLength(2)
    expect(bodies[1]).toEqual({ video_asset_id: mp4Asset2.asset_id })
    expect(wrapper.text()).toContain('已提交，正在校验')
  })
})

describe('SegmentsTab 段级注入卡（#257）', () => {
  it('有卡的段把卡面与段词同屏显示，只读、没有编辑入口', async () => {
    server.use(
      http.get(/\/api\/v1\/artifacts\/.+\/content$/, ({ request }) => {
        const path = new URL(request.url).pathname
        return HttpResponse.text(
          path.includes('injection-card')
            ? '# 第 1 段接线卡\n| ref_image_0 | 橘猫 |'
            : '第 1 段提示词：雨夜街口。',
        )
      }),
    )
    const wrapper = mountTab({
      segments: [makeSegment({ prompt_artifact_id: 'sess-1/segments/segment-01.md' })],
      artifacts: [makeArtifact('segments/injection-card-01.md', 'segment_injection_card')],
    })

    await settle()
    expect(wrapper.text()).toContain('第 1 段接线卡')
    expect(wrapper.text()).toContain('ref_image_0')
    // 只读：卡面不是可编辑控件，也没有「保存/生成卡片」之类的按钮。
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.find('[contenteditable="true"]').exists()).toBe(false)
    expect(wrapper.findAll('button').some((button) => button.text().includes('生成'))).toBe(false)
  })

  it('没有该段卡的段不显示卡块（按段号取产物，不做空卡）', async () => {
    const wrapper = mountTab({
      segments: [makeSegment()],
      artifacts: [makeArtifact('segments/injection-card-02.md', 'segment_injection_card')],
    })

    await settle()
    expect(wrapper.find('[data-injection-card]').exists()).toBe(false)
  })
})
