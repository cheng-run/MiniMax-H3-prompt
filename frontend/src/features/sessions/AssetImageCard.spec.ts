import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, type ActionItemOut, type ArtifactInfo, type RefOut } from '@/api/client'
import { server } from '@/test/server'

import AssetImageCard from './AssetImageCard.vue'

// 素材待办卡（D7 §4.4 / #256）：第 N 件 / 语义名 / 词全文可复制 / 上传框 / 校对结论。
// 没有放行键——用例把「页面上不存在通过按钮」也钉住。
enableAutoUnmount(afterEach)

const sessionId = 'sess-1'
const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-asset/resolve'
const uploadUrl = 'http://localhost/api/v1/sessions/sess-1/assets'

function makeItem(overrides: Partial<ActionItemOut> = {}): ActionItemOut {
  return {
    item_id: 'item-asset',
    kind: 'asset_image',
    status: 'pending',
    created_at: '2026-11-13T00:00:00Z',
    resolved_at: '',
    reason: '',
    asset_index: 2,
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

const refs: RefOut[] = [
  makeRef(),
  makeRef({ name: '台灯', number: 2, asset_id: 'sess-1/bbbbbbbbbbbbbbbb' }),
  makeRef({ name: '旧信', number: 3, asset_id: 'sess-1/cccccccccccccccc' }),
]

function promptArtifact(name: string): ArtifactInfo {
  return {
    artifact_id: `sess-1/${name}`,
    name,
    kind: 'asset_prompt',
    bytes: 128,
    created_at: '2026-11-13T00:00:00Z',
  }
}

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function mountCard(item = makeItem(), artifacts: ArtifactInfo[] = []) {
  return mount(AssetImageCard, {
    props: { sessionId, item, refs, artifacts },
  })
}

beforeEach(() => resetSessionToken())

describe('AssetImageCard 词与已定稿', () => {
  it('展示第 N 件、语义名、词全文（含复制按钮）与已定稿件', async () => {
    server.use(
      http.get('http://localhost/api/v1/artifacts/:artifact/content', () =>
        HttpResponse.text('# 资产图提示词 · 第 2 件\n\n一盏黄铜台灯立在书桌右角。', {
          headers: { 'Content-Type': 'text/plain' },
        }),
      ),
    )
    const wrapper = mountCard(makeItem(), [promptArtifact('assets/picture-02.md')])
    await settle()

    expect(wrapper.text()).toContain('第 2 件素材图 · 台灯')
    expect(wrapper.text()).toContain('一盏黄铜台灯立在书桌右角。')
    // 严格串行：当前件之前的那一件必然已定稿。
    expect(wrapper.text()).toContain('已定稿 1 / 共 3 件')
    expect(wrapper.text()).toContain('已定稿：橘猫')
    const copy = wrapper.findAll('button').find((button) => button.text() === '复制词')
    expect(copy).toBeTruthy()
    // 没有放行键：这个词不通过时只能改词重交。
    expect(wrapper.findAll('button').some((button) => button.text() === '通过')).toBe(false)
  })

  it('不通过时把差异清单摆出来，并明说要把同一件重交', async () => {
    const wrapper = mountCard(
      makeItem({
        reason: '[偏差] 1 处（描述与图不符）：\n  - 灯罩：描述「墨绿色灯罩」→ 图中「米白色灯罩」',
      }),
    )
    await settle()

    expect(wrapper.text()).toContain('校对结论：不通过')
    expect(wrapper.text()).toContain('墨绿色灯罩')
    expect(wrapper.text()).toContain('米白色灯罩')
    expect(wrapper.text()).toContain('重新生成一张')
    // 重开的是同一件：卡片标题仍然是第 2 件。
    expect(wrapper.text()).toContain('第 2 件素材图')
  })
})

describe('AssetImageCard 提交', () => {
  it('上传生成结果 → 用返回的素材标识决议 → 通知父组件刷新', async () => {
    let uploads = 0
    const bodies: unknown[] = []
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json(
          {
            asset_id: 'sess-1/dddddddddddddddd',
            name: 'gen.png',
            kind: 'image',
            bytes: 3,
            created_at: '2026-11-13T00:00:00Z',
          },
          { status: 201 },
        )
      }),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-1', kind: 'asset_loop', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard()

    const input = wrapper.find('input[type="file"]')
    const file = new File([new Uint8Array([1, 2, 3])], 'gen.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await settle()
    // jsdom 不实现「点提交按钮触发表单提交」（同 frontend.md 记的 Enter→click 那条坑），
    // 这里直接触发 form 的 submit；按钮本身由 Playwright 的真实浏览器验收。
    await wrapper.find('form').trigger('submit')
    await settle()

    // 生成结果走同一条素材上传（不带语义名：它不是参考素材，不进台账）；jsdom 下
    // 这条 fetch 的 multipart 被 polyfill 成 "[object FormData]"，读不出字段，
    // 所以这里只钉「交了一次」与「决议用的是它返回的标识」。
    expect(uploads).toBe(1)
    expect(bodies).toEqual([{ asset_id: 'sess-1/dddddddddddddddd' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
    expect(wrapper.text()).toContain('正在读图校对')
  })

  it('没选文件时提交按钮不可用（不空发请求）', async () => {
    let requests = 0
    server.use(
      http.post(uploadUrl, () => {
        requests += 1
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    const wrapper = mountCard()

    const submit = wrapper.findAll('button').find((button) => button.text() === '提交并校对')!
    expect(submit.attributes('disabled')).toBeDefined()
    await wrapper.find('form').trigger('submit')
    await settle()
    expect(requests).toBe(0)
  })
})
