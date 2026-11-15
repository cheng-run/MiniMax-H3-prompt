import { flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, type ActionItemOut, type Asset, type RefOut } from '@/api/client'
import { server } from '@/test/server'

import AssetExtraCard from './AssetExtraCard.vue'

// 素材视频/音频补交待办卡（#279 §补交口 / #284）：交完了，或明确「没有，就这样」。
const item: ActionItemOut = {
  item_id: 'item-extra',
  kind: 'asset_extra',
  status: 'pending',
  created_at: '2026-11-15T00:00:00Z',
  resolved_at: '',
  reason: '',
}

const refs: RefOut[] = [
  {
    name: '橘猫',
    kind: 'picture',
    number: 1,
    asset_id: 'sess-1/pppppppppppppppp',
    status: 'passed',
    source: 'derived',
  },
  {
    name: '运镜参考',
    kind: 'video',
    number: 1,
    asset_id: 'sess-1/vvvvvvvvvvvvvvvv',
    status: 'registered',
    source: 'uploaded',
  },
]

const uploaded: Asset = {
  asset_id: 'sess-1/wwwwwwwwwwwwwwww',
  name: 'voice.wav',
  kind: 'audio',
  bytes: 4096,
  created_at: '2026-11-15T00:00:00Z',
}

const uploadUrl = 'http://localhost/api/v1/sessions/sess-1/assets'
const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-extra/resolve'

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function mountCard(initialRefs: RefOut[] = refs) {
  return mount(AssetExtraCard, {
    props: { sessionId: 'sess-1', item, refs: initialRefs },
  })
}

function button(wrapper: ReturnType<typeof mountCard>, label: string) {
  return wrapper.findAll('button').find((entry) => entry.text() === label)!
}

async function chooseFile(wrapper: ReturnType<typeof mountCard>): Promise<void> {
  const input = wrapper.find('input[type="file"]')
  const file = new File([new Uint8Array([1, 2, 3])], 'voice.wav', { type: 'audio/wav' })
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
}

beforeEach(() => resetSessionToken())

describe('AssetExtraCard 台账回显', () => {
  it('列出已交回的视频/音频与库内编号，不含 Picture', () => {
    const wrapper = mountCard()
    const text = wrapper.text()
    expect(text).toContain('运镜参考')
    expect(text).toContain('<Video 1>')
    expect(text).not.toContain('橘猫')
  })

  it('一件补交都没有时「交完这些」不可点，「没有，就这样」始终可点', () => {
    const wrapper = mountCard([])
    expect(button(wrapper, '交完这些，继续').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '没有，就这样').attributes('disabled')).toBeUndefined()
  })
})

describe('AssetExtraCard 上传', () => {
  it('缺语义名不发上传请求，并说明要先起名', async () => {
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json(uploaded, { status: 201 })
      }),
    )
    const wrapper = mountCard([])
    await chooseFile(wrapper)
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(uploads).toBe(0)
    expect(wrapper.text()).toContain('语义名')
  })

  it('带用户起的语义名上传，成功后可提交', async () => {
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json(uploaded, { status: 201 })
      }),
    )
    const wrapper = mountCard([])
    await wrapper.find('#asset-extra-name').setValue('红裙女孩的音色')
    await chooseFile(wrapper)
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(uploads).toBe(1)
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
    expect(button(wrapper, '交完这些，继续').attributes('disabled')).toBeUndefined()
  })
})

describe('AssetExtraCard 决议', () => {
  it('交完了：decision=submit_extras 与最后一件 asset_id', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(uploadUrl, () => HttpResponse.json(uploaded, { status: 201 })),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-1', kind: 'advance', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard([])
    await wrapper.find('#asset-extra-name').setValue('红裙女孩的音色')
    await chooseFile(wrapper)
    await wrapper.find('form').trigger('submit')
    await settle()

    await button(wrapper, '交完这些，继续').trigger('click')
    await settle()

    expect(bodies).toEqual([{ decision: 'submit_extras', asset_id: uploaded.asset_id }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })

  it('没有，就这样：decision=no_extras，不带 asset_id；重载后也能直接点', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    // 台账里已有上一次交回的一件：刷新页面后按钮仍然是可点的。
    const wrapper = mountCard(refs)
    await button(wrapper, '没有，就这样').trigger('click')
    await settle()

    expect(bodies).toEqual([{ decision: 'no_extras' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })

  it('容量超限时把服务端点名的那一档原样展示', async () => {
    server.use(
      http.post(resolveUrl, () =>
        HttpResponse.json(
          {
            status: 422,
            code: 'ref_capacity_exceeded',
            detail:
              '补交素材：参考素材超出官方单次请求上限：\n- 视频 4 个 > 3 个：<Video 4>「迟到的视频」',
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const wrapper = mountCard(refs)
    await button(wrapper, '交完这些，继续').trigger('click')
    await settle()

    expect(wrapper.text()).toContain('视频 4 个 > 3 个')
    expect(wrapper.emitted('submitted')).toBeUndefined()
  })
})
