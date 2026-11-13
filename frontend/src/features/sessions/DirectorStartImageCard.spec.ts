import { flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, type ActionItemOut, type Asset } from '@/api/client'
import { server } from '@/test/server'

import DirectorStartImageCard from './DirectorStartImageCard.vue'

// 导演台开场首帧交接卡（D8 §4.4 / §7）：一个待办收两张图，首帧必填、全片尾帧可选；
// 画幅不符**不默认放行**——409 之后必须显式确认才重发。
const item: ActionItemOut = {
  item_id: 'item-start',
  kind: 'director_start_image',
  status: 'pending',
  created_at: '2026-11-13T00:00:00Z',
  resolved_at: '',
  reason: '',
}

const startImage: Asset = {
  asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
  name: 'start.png',
  kind: 'image',
  bytes: 1024,
  created_at: '2026-11-13T00:00:00Z',
}
const endImage: Asset = {
  asset_id: 'sess-1/bbbbbbbbbbbbbbbb',
  name: 'film-end.png',
  kind: 'image',
  bytes: 2048,
  created_at: '2026-11-13T00:00:00Z',
}
const video: Asset = {
  asset_id: 'sess-1/cccccccccccccccc',
  name: 'clip.webm',
  kind: 'video',
  bytes: 4096,
  created_at: '2026-11-13T00:00:00Z',
}
const assets = [startImage, endImage, video]

const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-start/resolve'
const uploadUrl = 'http://localhost/api/v1/sessions/sess-1/assets'

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function mountCard() {
  return mount(DirectorStartImageCard, {
    props: { sessionId: 'sess-1', item, assets },
  })
}

function button(wrapper: ReturnType<typeof mountCard>, label: string) {
  return wrapper.findAll('button').find((entry) => entry.text() === label)!
}

beforeEach(() => resetSessionToken())

describe('DirectorStartImageCard 选择器', () => {
  it('两个图槽都只列本会话的图片素材（视频不出现）', () => {
    const wrapper = mountCard()

    const selects = wrapper.findAll('select')
    expect(selects).toHaveLength(2)
    for (const select of selects) {
      const options = select.findAll('option').map((option) => option.text())
      expect(options).toContain('start.png')
      expect(options).toContain('film-end.png')
      expect(options).not.toContain('clip.webm')
    }
  })
})

describe('DirectorStartImageCard 提交', () => {
  it('缺开场首帧时不发请求，并点名首帧必填', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()

    await button(wrapper, '提交开场首帧并继续').trigger('click')
    await settle()

    expect(bodies).toHaveLength(0)
    expect(wrapper.text()).toContain('开场首帧')
  })

  it('交两张图时按服务端契约的字段形状提交，且不带画幅确认', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()

    const selects = wrapper.findAll('select')
    await selects[0]!.setValue(startImage.asset_id)
    await selects[1]!.setValue(endImage.asset_id)
    await button(wrapper, '提交开场首帧并继续').trigger('click')
    await settle()

    expect(bodies).toEqual([
      {
        decision: 'submit_start_image',
        start_asset_id: startImage.asset_id,
        end_asset_id: endImage.asset_id,
      },
    ])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })

  it('不选全片尾帧时只发首帧（尾帧是可选的）', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()

    await wrapper.findAll('select')[0]!.setValue(startImage.asset_id)
    await button(wrapper, '提交开场首帧并继续').trigger('click')
    await settle()

    expect(bodies).toEqual([
      { decision: 'submit_start_image', start_asset_id: startImage.asset_id },
    ])
  })
})

describe('DirectorStartImageCard 画幅确认', () => {
  it('409 时先展示实际比例与画布比例，勾选确认后才带 confirm_aspect_mismatch 重发', async () => {
    const bodies: unknown[] = []
    let refused = false
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push(body)
        if (!refused) {
          refused = true
          return HttpResponse.json(
            {
              type: 'about:blank',
              title: '资源状态冲突',
              status: 409,
              detail:
                '\n[画幅预检] 关键帧画布 1280×736（比例 1.739）与 director 节声明的成片画幅 736×1280（比例 0.575）相差 202.4%，超过 5% 容差。',
              instance: resolveUrl,
              code: 'aspect_mismatch_unconfirmed',
            },
            { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()
    await wrapper.findAll('select')[0]!.setValue(startImage.asset_id)

    await button(wrapper, '提交开场首帧并继续').trigger('click')
    await settle()

    // 第一次请求**不带**确认：用户要先看见两个比例。
    expect(bodies).toEqual([
      { decision: 'submit_start_image', start_asset_id: startImage.asset_id },
    ])
    expect(wrapper.text()).toContain('1280×736')
    expect(wrapper.text()).toContain('736×1280')

    // 未勾选时「确认比例并提交」不可点：确认必须是显式的。
    await button(wrapper, '确认比例并提交').trigger('click')
    await settle()
    expect(bodies).toHaveLength(1)

    await wrapper.find('input[name="confirm-aspect-mismatch"]').setValue(true)
    await button(wrapper, '确认比例并提交').trigger('click')
    await settle()

    expect(bodies).toHaveLength(2)
    expect(bodies[1]).toEqual({
      decision: 'submit_start_image',
      start_asset_id: startImage.asset_id,
      confirm_aspect_mismatch: true,
    })
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })
})

describe('DirectorStartImageCard 卡内上传', () => {
  it('在卡里上传的图立刻可作首帧（不必去素材分区绕一圈）', async () => {
    const uploaded: Asset = {
      asset_id: 'sess-1/dddddddddddddddd',
      name: 'new-start.png',
      kind: 'image',
      bytes: 512,
      created_at: '2026-11-13T00:01:00Z',
    }
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json(uploaded, { status: 201 })
      }),
    )
    const wrapper = mountCard()

    const input = wrapper.find('input[name="start-frame-upload"]')
    Object.defineProperty(input.element, 'files', {
      value: [new File(['x'], 'new-start.png', { type: 'image/png' })],
    })
    await input.trigger('change')
    await settle()

    expect(uploads).toBe(1)
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
    const select = wrapper.findAll('select')[0]!
    expect(select.findAll('option').map((option) => option.text())).toContain('new-start.png')
    expect((select.element as HTMLSelectElement).value).toBe(uploaded.asset_id)
  })
})
