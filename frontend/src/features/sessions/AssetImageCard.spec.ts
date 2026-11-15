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
const sessionUrl = 'http://localhost/api/v1/sessions/sess-1'
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
    status: 'pending',
    source: 'derived',
    ...overrides,
  }
}

// 严格串行：当前件之前的那一件必然已定稿（状态按台账真实值给，不靠编号推）。
const refs: RefOut[] = [
  makeRef({ status: 'passed' }),
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

function mountCard(item = makeItem(), artifacts: ArtifactInfo[] = [], ledger: RefOut[] = refs) {
  return mount(AssetImageCard, {
    props: { sessionId, item, refs: ledger, artifacts },
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

  it('前序放弃件不算已定稿：只数 status=passed 的行', async () => {
    const wrapper = mountCard(
      makeItem({ asset_index: 3 }),
      [],
      [
        makeRef({ name: '橘猫', number: 1, status: 'abandoned' }),
        makeRef({ name: '台灯', number: 2, status: 'passed' }),
        makeRef({ name: '旧信', number: 3, status: 'pending' }),
      ],
    )
    await settle()

    expect(wrapper.text()).toContain('已定稿 1 / 共 3 件')
    expect(wrapper.text()).toContain('其中已放弃 1 件')
    expect(wrapper.text()).toContain('已定稿：台灯')
    // 放弃件不该被念成已定稿（按编号比就会把它算进去）。
    expect(wrapper.text()).not.toContain('已定稿：橘猫')
  })

  it('不通过时把差异清单摆出来，并给出两条并列出路', async () => {
    const wrapper = mountCard(
      makeItem({
        reason: '[偏差] 1 处（描述与图不符）：\n  - 灯罩：描述「墨绿色灯罩」→ 图中「米白色灯罩」',
      }),
    )
    await settle()

    expect(wrapper.text()).toContain('校对结论：不通过')
    expect(wrapper.text()).toContain('墨绿色灯罩')
    expect(wrapper.text()).toContain('米白色灯罩')
    expect(wrapper.text()).toContain('重新生成并交回')
    // 重开的是同一件：卡片标题仍然是第 2 件。
    expect(wrapper.text()).toContain('第 2 件素材图')
    // 两条出路并列可见（#288）：默认是「改词强化原意再掷」，原词再掷在旁边。
    expect(wrapper.text()).toContain('改词强化原意再掷（默认）')
    expect(wrapper.text()).toContain('原词再掷')
    // 默认键的文案必须说清「只做加法」，否则用户会以为它照图改设计。
    expect(wrapper.text()).toContain('只做加法')
  })
})

describe('AssetImageCard 校对不通过的两条出路', () => {
  it('默认出路（改词强化原意再掷）发 reword_reroll，请求立即返回并通知父组件', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-1', kind: 'asset_reword', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard(makeItem({ reason: '[偏差] 1 处：灯罩颜色不符' }))
    await settle()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '改词强化原意再掷（默认）')!
      .trigger('click')
    await settle()

    expect(bodies).toEqual([{ decision: 'reword_reroll' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })

  it('原词再掷发 reroll（不带 asset_id，也不上传任何文件）', async () => {
    const bodies: unknown[] = []
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json({}, { status: 201 })
      }),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-2', kind: 'asset_loop', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard(makeItem({ reason: '[偏差] 1 处：灯罩颜色不符' }))
    await settle()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '原词再掷')!
      .trigger('click')
    await settle()

    expect(uploads).toBe(0)
    expect(bodies).toEqual([{ decision: 'reroll' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })
})

describe('AssetImageCard 生图档位与读图模型（#292）', () => {
  it('显示本件的生图档位：直接用服务端渲染好的显示名', async () => {
    const wrapper = mountCard(
      makeItem(),
      [],
      [
        makeRef({
          name: '台灯',
          number: 2,
          image_model: 'qwen',
          image_model_label: 'Qwen-Image 2.1',
        }),
      ],
    )
    await settle()

    // 前端不另写名字表：卡片照着详情里的显示名念，改档的称呼只需服务端改一处。
    expect(wrapper.get('[data-testid="asset-image-model"]').text()).toContain(
      '生图档：Qwen-Image 2.1',
    )
  })

  it('台账行没有档位字段时显示「未知」，不编造一档', async () => {
    const wrapper = mountCard(makeItem(), [], [makeRef({ number: 2, name: '台灯' })])
    await settle()

    expect(wrapper.get('[data-testid="asset-image-model"]').text()).toContain('生图档：未知')
  })

  it('校对结论行注明本次读图模型；缺字段时说「未知」', async () => {
    const withModel = mountCard(makeItem({ reason: '[偏差] 1 处', vision_model: 'qwen3.7-plus' }))
    await settle()
    expect(withModel.get('[data-testid="proofread-vision-model"]').text()).toContain(
      '读图模型：qwen3.7-plus',
    )

    // 旧会话 / 没读到模型：同一行说「未知」，不是空着让人以为是加载中。
    const withoutModel = mountCard(makeItem({ reason: '[偏差] 1 处' }))
    await settle()
    expect(withoutModel.get('[data-testid="proofread-vision-model"]').text()).toContain(
      '读图模型：未知',
    )
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

describe('AssetImageCard 放弃这一件', () => {
  it('先把代价摊在确认里，确认后才发决议（只带 decision，不带 asset_id）', async () => {
    const bodies: unknown[] = []
    const keys: (string | null)[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        keys.push(request.headers.get('idempotency-key'))
        return HttpResponse.json(
          { job_id: 'job-1', kind: 'asset_loop', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
      // 决议后回读快照：回执优先用服务端写进已决议待办的 reason（与事件流同源）。
      http.get(sessionUrl, () =>
        HttpResponse.json({
          action_items: [
            {
              item_id: 'item-asset',
              kind: 'asset_image',
              status: 'resolved',
              created_at: '2026-11-13T00:00:00Z',
              resolved_at: '2026-11-13T00:01:00Z',
              reason: '服务端记下的放弃代价：这一格没有图可填。',
            },
          ],
        }),
      ),
    )
    const wrapper = mountCard()
    await settle()

    // 点之前不吓人：代价还没铺开，也没有发请求。
    const abandon = wrapper.findAll('button').find((button) => button.text() === '放弃这一件')!
    expect(abandon).toBeTruthy()
    expect(wrapper.text()).not.toContain('无图指称')
    expect(bodies).toHaveLength(0)

    await abandon.trigger('click')
    expect(bodies).toHaveLength(0)
    // 后果在点「确认放弃」之前可见（#279 US10）：这一件是第 2 件「台灯」。
    expect(wrapper.text()).toContain('分镜表与段词里对「台灯」的指称会变成无图指称')
    expect(wrapper.text()).toContain('已经定稿的文本不会被自动改写')

    const confirm = wrapper.findAll('button').find((button) => button.text() === '确认放弃')!
    await confirm.trigger('click')
    await settle()

    expect(bodies).toEqual([{ decision: 'abandon_asset' }])
    expect(keys[0]).toBeTruthy()
    expect(wrapper.emitted('submitted')).toHaveLength(1)
    // 回执用服务端写进已决议待办的那句（不是前端再抄一遍）；确认面板收起。
    expect(wrapper.text()).toContain('服务端记下的放弃代价')
    expect(wrapper.findAll('button').some((button) => button.text() === '确认放弃')).toBe(false)
  })

  it('快照读不到时，回执退回本地镜像的那句', async () => {
    server.use(
      http.post(resolveUrl, () =>
        HttpResponse.json(
          { job_id: 'job-1', kind: 'asset_loop', status: 'running', started_at: '' },
          { status: 202 },
        ),
      ),
      http.get(sessionUrl, () => HttpResponse.error()),
    )
    const wrapper = mountCard()
    await settle()

    const abandon = wrapper.findAll('button').find((button) => button.text() === '放弃这一件')!
    await abandon.trigger('click')
    const confirm = wrapper.findAll('button').find((button) => button.text() === '确认放弃')!
    await confirm.trigger('click')
    await settle()

    expect(wrapper.text()).toContain('已放弃「台灯」这一件')
  })

  it('取消不发请求，代价收起', async () => {
    let requests = 0
    server.use(
      http.post(resolveUrl, () => {
        requests += 1
        return HttpResponse.json({}, { status: 202 })
      }),
    )
    const wrapper = mountCard()
    await settle()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '放弃这一件')!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '取消')!
      .trigger('click')

    expect(requests).toBe(0)
    expect(wrapper.text()).not.toContain('无图指称')
    expect(wrapper.findAll('button').some((button) => button.text() === '放弃这一件')).toBe(true)
  })

  it('放弃失败时显示后端文案，不伪装成功', async () => {
    server.use(
      http.post(resolveUrl, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '素材图待办载荷不合法',
            status: 422,
            code: 'asset_image_invalid',
            detail: '素材台账里没有第 2 件参考素材。',
            instance: '',
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const wrapper = mountCard()
    await settle()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '放弃这一件')!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '确认放弃')!
      .trigger('click')
    await settle()

    expect(wrapper.text()).toContain('素材台账里没有第 2 件参考素材。')
    expect(wrapper.emitted('submitted')).toBeUndefined()
    expect(wrapper.text()).not.toContain('已放弃「台灯」这一件')
  })
})

describe('AssetImageCard 四选的后两条：按图改文案／保留原描述（#289）', () => {
  const failed = () =>
    makeItem({
      reason: '[偏差] 1 处（描述与图不符）：\n  - 灯罩：描述「墨绿色灯罩」→ 图中「米白色灯罩」',
      proofread: {
        deviations: [{ aspect: '灯罩', prompt_quote: '墨绿色灯罩', observed: '米白色灯罩' }],
        omissions: [
          { aspect: '桌面', fact: '桌角有一圈水渍' },
          { aspect: '窗户', fact: '窗框是白色的' },
        ],
      },
    })

  it('四条出路齐备，定稿两条的文案说清「只改描述、不改生图词」', async () => {
    const wrapper = mountCard(failed())
    await settle()

    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).toContain('改词强化原意再掷（默认）')
    expect(labels).toContain('原词再掷')
    expect(labels).toContain('按图改文案（只改描述）')
    expect(labels).toContain('明知故犯保留原描述')
    // 四选之外的第五个动作（放弃）仍在，但它不属于校对四选。
    expect(labels).toContain('放弃这一件')

    expect(wrapper.text()).toContain('生图词逐字不动')
    expect(wrapper.text()).toContain('当场定稿')
    // 补漏条数在选之前就要看得见：并入发生在定稿那一刻，而定稿后这张卡就不在了。
    expect(wrapper.get('[data-testid="asset-facts-count"]').text()).toContain(
      '本次校对的 2 条补漏会在定稿时按图补入描述',
    )
  })

  it('按图改文案发 accept_image_facts（不带图、请求立即返回）', async () => {
    const bodies: unknown[] = []
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json({}, { status: 201 })
      }),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-3', kind: 'asset_loop', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard(failed())
    await settle()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '按图改文案（只改描述）')!
      .trigger('click')
    await settle()

    expect(uploads).toBe(0)
    expect(bodies).toEqual([{ decision: 'accept_image_facts' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })

  it('保留原描述发 keep_description（不带图、请求立即返回）', async () => {
    const bodies: unknown[] = []
    let uploads = 0
    server.use(
      http.post(uploadUrl, () => {
        uploads += 1
        return HttpResponse.json({}, { status: 201 })
      }),
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-4', kind: 'asset_loop', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard(failed())
    await settle()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '明知故犯保留原描述')!
      .trigger('click')
    await settle()

    expect(uploads).toBe(0)
    expect(bodies).toEqual([{ decision: 'keep_description' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })

  it('没有补漏时不显示条数那一行（不编造一个 0 条）', async () => {
    const wrapper = mountCard(
      makeItem({
        reason: '[偏差] 1 处',
        proofread: {
          deviations: [{ aspect: '灯罩', prompt_quote: '墨绿色灯罩', observed: '米白色灯罩' }],
          omissions: [],
        },
      }),
    )
    await settle()

    expect(wrapper.find('[data-testid="asset-facts-count"]').exists()).toBe(false)
  })
})

describe('AssetImageCard 恢复阶段 1 原文（#291）', () => {
  const diff =
    '--- 改前（磁盘上的生图词）\n+++ 改后（阶段 1 原文）\n-一盏黄铜台灯立在书桌右角，灯罩是墨绿色。\n+一盏黄铜台灯立在书桌右角，灯罩是米白色。'

  it('无账差异时出现恢复动作并展示改前改后 diff', async () => {
    const wrapper = mountCard(makeItem({ restore_available: true, restore_diff: diff }))
    await settle()

    expect(wrapper.text()).toContain('恢复阶段 1 原文')
    // diff 由服务端算好带下来：改前改后两行都要看得见。
    expect(wrapper.text()).toContain('一盏黄铜台灯立在书桌右角，灯罩是墨绿色。')
    expect(wrapper.text()).toContain('一盏黄铜台灯立在书桌右角，灯罩是米白色。')
    expect(wrapper.find('[data-testid="asset-restore"]').exists()).toBe(true)
  })

  it('不出现时没有任何恢复入口（默认字段为 false）', async () => {
    const wrapper = mountCard(makeItem())
    await settle()

    expect(wrapper.text()).not.toContain('恢复阶段 1 原文')
    expect(wrapper.find('[data-testid="asset-restore"]').exists()).toBe(false)
  })

  it('恢复不是校对四选之一：四选的个数与文案不因它变化', async () => {
    const plain = mountCard(makeItem({ reason: '[偏差] 1 处：灯罩颜色不符' }))
    await settle()
    const withRestore = mountCard(
      makeItem({
        reason: '[偏差] 1 处：灯罩颜色不符',
        restore_available: true,
        restore_diff: diff,
      }),
    )
    await settle()

    const exitTexts = (wrapper: ReturnType<typeof mountCard>) =>
      wrapper
        .findAll('button')
        .map((button) => button.text())
        .filter((text) => text === '改词强化原意再掷（默认）' || text === '原词再掷')

    expect(exitTexts(withRestore)).toEqual(exitTexts(plain))
    expect(exitTexts(withRestore)).toHaveLength(2)
  })

  it('点恢复发 restore_prompt（不带 asset_id），请求返回后通知父组件', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'job-restore', kind: 'asset_restore', status: 'running', started_at: '' },
          { status: 202 },
        )
      }),
    )
    const wrapper = mountCard(makeItem({ restore_available: true, restore_diff: diff }))
    await settle()

    await wrapper.find('[data-testid="asset-restore"]').trigger('click')
    await settle()

    expect(bodies).toEqual([{ decision: 'restore_prompt' }])
    expect(wrapper.emitted('submitted')).toHaveLength(1)
  })
})
