import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { ActionItemOut, ArtifactInfo } from '@/api/client'
import { resetSessionToken } from '@/api/client'
import { server } from '@/test/server'

import ReviewTab from './ReviewTab.vue'

// 审查分区（#232）：组件边界是它的 props 与出站请求。这里直接挂组件、不经过工作台
// 壳与 SSE——审查卡与修订表单的行为是这一票的可观察面。

enableAutoUnmount(afterEach)

const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-new/resolve'

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function makeArtifact(name: string, kind: string): ArtifactInfo {
  return {
    artifact_id: `sess-1/${name}`,
    name,
    kind,
    bytes: 10,
    created_at: '2026-11-11T00:00:00Z',
  }
}

// 内容端点：MSW 的 `:param` 不跨斜杠，产物标识本身带斜杠，所以用正则匹配整段
// 标识、按最后一段文件名回固定文本。
function serveArtifactTexts(texts: Record<string, string>): void {
  server.use(
    http.get(/\/api\/v1\/artifacts\/.+\/content$/, ({ request }) => {
      const name = decodeURIComponent(new URL(request.url).pathname).split('/').at(-2)
      const text = texts[name ?? '']
      return text === undefined
        ? HttpResponse.json({ title: '产物不存在' }, { status: 404 })
        : HttpResponse.text(text)
    }),
  )
}

function pendingReview(): ActionItemOut {
  return {
    item_id: 'item-new',
    kind: 'stage1_review',
    status: 'pending',
    created_at: '2026-11-11T00:01:00Z',
    resolved_at: '',
  }
}

function resolvedReview(): ActionItemOut {
  return {
    item_id: 'item-old',
    kind: 'stage1_review',
    status: 'resolved',
    decision: 'revise',
    created_at: '2026-11-11T00:00:00Z',
    resolved_at: '2026-11-11T00:00:30Z',
    revisions: [{ layer: 'story', text: '结局需要更克制的收束' }],
  }
}

function mountReview(actionItems: ActionItemOut[], artifacts: ArtifactInfo[]) {
  // 审查分区不依赖路由/Pinia：它是纯展示 + 两个出站请求，直接挂组件最贴近可观察面。
  return mount(ReviewTab, { props: { sessionId: 'sess-1', actionItems, artifacts } })
}

beforeEach(() => resetSessionToken())

describe('ReviewTab 中间产物与修订历史（D4-AC8）', () => {
  it('分文件展示阶段 1 产物，并回显已决议待办的修订历史', async () => {
    serveArtifactTexts({
      'script.md': '# 分场剧本（桩管线第 2 稿）',
      'shot-table.md': '[Shot 1] At 00:00.000, a medium shot.',
      'first-frame-prompt.md': '首帧词：雨夜街口',
      'last-frame-prompt.md': '尾帧词：收起伞',
    })
    const wrapper = mountReview(
      [pendingReview(), resolvedReview()],
      [
        makeArtifact('script.md', 'script'),
        makeArtifact('shot-table.md', 'shot_table'),
        makeArtifact('first-frame-prompt.md', 'frame_prompt'),
        makeArtifact('last-frame-prompt.md', 'frame_prompt'),
      ],
    )
    await settle()

    const text = wrapper.text()
    // 四个文件名与正文都可见（帧链会话才有两帧词）。
    expect(text).toContain('script.md')
    expect(text).toContain('shot-table.md')
    expect(text).toContain('first-frame-prompt.md')
    expect(text).toContain('last-frame-prompt.md')
    expect(text).toContain('桩管线第 2 稿')
    expect(text).toContain('雨夜街口')
    expect(text).toContain('收起伞')

    // 修订历史来自已决议待办的 revisions，层次按中文展示（内部值是 story）。
    expect(text).toContain('修订历史')
    expect(text).toContain('剧情分镜')
    expect(text).toContain('结局需要更克制的收束')
  })

  it('T2VA 只有剧本与分镜表时，不展示帧词文件', async () => {
    serveArtifactTexts({
      'script.md': '纯文字剧本',
      'shot-table.md': '纯文字分镜',
    })
    const wrapper = mountReview(
      [pendingReview()],
      [makeArtifact('script.md', 'script'), makeArtifact('shot-table.md', 'shot_table')],
    )
    await settle()

    const text = wrapper.text()
    expect(text).toContain('纯文字剧本')
    expect(text).not.toContain('first-frame-prompt.md')
    expect(text).not.toContain('last-frame-prompt.md')
  })

  it('按 kind 认中间产物：登记表换了文件名也照常显示', async () => {
    serveArtifactTexts({ 'draft-notes.md': '换名后的剧本正文' })
    const wrapper = mountReview([pendingReview()], [makeArtifact('draft-notes.md', 'script')])
    await settle()

    expect(wrapper.text()).toContain('换名后的剧本正文')
    expect(wrapper.text()).toContain('剧本')
  })
})

describe('ReviewTab 通过与提意见（D4-AC9）', () => {
  it('「通过」提交 decision=approve，不带 revisions，并通知工作台刷新', async () => {
    serveArtifactTexts({ 'script.md': '剧本' })
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountReview([pendingReview()], [makeArtifact('script.md', 'script')])
    await settle()

    const approve = wrapper.findAll('button').find((b) => b.text() === '通过')!
    expect(approve).toBeTruthy()
    await approve.trigger('click')
    await settle()

    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toEqual({ decision: 'approve' })
    expect(wrapper.emitted('resolved')).toHaveLength(1)
  })

  it('提意见：空文本即时提示且不发请求；填写后单条提交并清空输入', async () => {
    serveArtifactTexts({ 'script.md': '剧本' })
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountReview([pendingReview()], [makeArtifact('script.md', 'script')])
    await settle()

    // jsdom 不实现「submit 按钮点击 → 表单提交」的默认动作（与 Enter→click 同类），
    // 所以这里在表单边界触发 submit——真实浏览器里的点击由 E2E 覆盖。
    const submit = wrapper.findAll('button').find((b) => b.text() === '提交意见')!
    expect(submit).toBeTruthy()
    await wrapper.find('form').trigger('submit')
    await settle()

    // 空文本由前端即时拦下：不打扰服务端，但服务端仍是权威（下面一条测 422）。
    expect(wrapper.text()).toContain('修订内容不能为空')
    expect(bodies).toHaveLength(0)

    await wrapper.find('select[name="revision-layer"]').setValue('人物设定')
    await wrapper.find('textarea[name="revision-text"]').setValue('人物服装改成米色风衣')
    await wrapper.find('form').trigger('submit')
    await settle()

    // 一轮一条：载荷只有这一条修订，层次用中文别名（服务端归一）。
    expect(bodies).toEqual([
      {
        decision: 'revise',
        revisions: [{ layer: '人物设定', text: '人物服装改成米色风衣' }],
      },
    ])
    expect(wrapper.emitted('resolved')).toHaveLength(1)
    expect(
      (wrapper.find('textarea[name="revision-text"]').element as HTMLTextAreaElement).value,
    ).toBe('')
  })

  it('服务端 422 的 detail 原样呈现（后端仍是权威）', async () => {
    serveArtifactTexts({ 'script.md': '剧本' })
    server.use(
      http.post(resolveUrl, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '请求无效',
            status: 422,
            detail: '不认识的修订层次：火星',
            instance: resolveUrl,
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const wrapper = mountReview([pendingReview()], [makeArtifact('script.md', 'script')])
    await settle()

    const approve = wrapper.findAll('button').find((b) => b.text() === '通过')!
    await approve.trigger('click')
    await settle()

    expect(wrapper.find('[role="alert"]').text()).toContain('不认识的修订层次：火星')
    expect(wrapper.emitted('resolved')).toBeUndefined()
  })
})
