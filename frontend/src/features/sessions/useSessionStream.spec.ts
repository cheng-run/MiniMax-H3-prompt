import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken } from '@/api/client'

import { useSessionStream, type InvalidateReason } from './useSessionStream'

// 可控事件流：测试自己决定什么时候推一帧、什么时候把连接掐掉。
interface FakeStream {
  response: Response
  push(text: string): void
  close(): void
}

function makeStream(headers: Record<string, string> = {}): FakeStream {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  const encoder = new TextEncoder()
  return {
    response: new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', ...headers },
    }),
    push(text: string) {
      controller.enqueue(encoder.encode(text))
    },
    close() {
      controller.close()
    },
  }
}

function makeFetch() {
  const calls: { url: string; init?: RequestInit }[] = []
  const queue: Response[] = []
  const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init })
    const next = queue.shift()
    if (!next) {
      throw new Error('没有排队的响应')
    }
    return next
  }) as typeof fetch
  return { impl, calls, queue }
}

function frame(id: number, event: string, payload: Record<string, unknown> = {}): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(payload)}\n\n`
}

// 服务端实际的事件包封（`Event.to_dict`）：payload 嵌在 `payload` 键下，时刻在 `at`。
function envelopeFrame(
  id: number,
  event: string,
  payload: Record<string, unknown>,
  at: string,
): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify({
    seq: id,
    type: event,
    at,
    session_id: 'sess-1',
    payload,
  })}\n\n`
}

// 进度包封的时刻：用例不依赖具体值，只要形状是真的。
const PROGRESS_AT = '2026-11-14T07:00:00.000000+00:00'

async function settle(ms = 15): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms))
}

beforeEach(() => resetSessionToken())

describe('useSessionStream（失效信号模式）', () => {
  it('令牌在请求头、URL 里没有；首订阅用快照序号作 Last-Event-ID', async () => {
    const stream = makeStream()
    const { impl, calls, queue } = makeFetch()
    queue.push(stream.response)
    const invalidations: InvalidateReason[] = []
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      snapshotCursor: () => 12,
      onInvalidate: (reason) => {
        invalidations.push(reason)
      },
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle()

    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe('http://localhost/api/v1/sessions/sess-1/events')
    expect(calls[0].url).not.toContain('token')
    const headers = new Headers(calls[0].init?.headers)
    expect(headers.get('X-Workbench-Token')).toBe('test-session-token')
    // 快照优先：已经拿到的快照序号就是补发起点，不必把快照覆盖过的旧事件重放一遍。
    expect(headers.get('Last-Event-ID')).toBe('12')
    expect(api.connection.value).toBe('open')
    api.stop()
  })

  it('事件只作失效信号：窗口内多帧只触发一次重拉', async () => {
    const stream = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(stream.response)
    const invalidations: InvalidateReason[] = []
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      onInvalidate: (reason) => {
        invalidations.push(reason)
      },
      fetchImpl: impl,
      throttleMs: 10,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    stream.push(
      envelopeFrame(1, 'job.progress', { node: 'storyboard', status: 'started' }, PROGRESS_AT),
    )
    stream.push(
      envelopeFrame(2, 'job.progress', { node: 'storyboard', status: 'done' }, PROGRESS_AT),
    )
    stream.push(frame(3, 'action_item.created', { item_id: 'i1', kind: 'stage1_review' }))
    await settle(30)

    expect(invalidations).toEqual(['event'])
    expect(api.lastEventId.value).toBe(3)
    api.stop()
  })

  it('断线：先重取快照，再带 Last-Event-ID 重订阅', async () => {
    const first = makeStream()
    const second = makeStream()
    const { impl, calls, queue } = makeFetch()
    queue.push(first.response, second.response)
    const invalidations: InvalidateReason[] = []
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      onInvalidate: (reason) => {
        invalidations.push(reason)
      },
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    first.push(frame(7, 'job.started', { kind: 'advance' }))
    await settle(15)
    first.close()
    await settle(30)

    expect(invalidations).toContain('disconnect')
    expect(calls).toHaveLength(2)
    expect(new Headers(calls[1].init?.headers).get('Last-Event-ID')).toBe('7')
    expect(api.connection.value).toBe('open')
    api.stop()
  })

  it('服务端要求重同步：全量重取快照，不尝试补发旧序号，流继续读', async () => {
    let snapshotSeq = 99
    const stream = makeStream({ 'X-Resync-Required': 'true' })
    const { impl, calls, queue } = makeFetch()
    queue.push(stream.response)
    const invalidations: InvalidateReason[] = []
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      snapshotCursor: () => snapshotSeq,
      onInvalidate: (reason) => {
        invalidations.push(reason)
        // 重取快照后序号会回落到事件流真实尾号附近。
        snapshotSeq = 50
      },
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(10)

    expect(invalidations).toEqual(['resync'])
    // 同一条流仍然有效（服务端从当前尾号继续），不需要重连。
    expect(calls).toHaveLength(1)
    expect(api.lastEventId.value).toBe(50)

    stream.push(
      envelopeFrame(
        51,
        'job.progress',
        { node: 'cinematographer', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(10)
    expect(api.lastEventId.value).toBe(51)
    api.stop()
  })

  it('job.progress 的 node/stage 作为当前步骤展示；任务收尾即清空', async () => {
    const stream = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(stream.response)
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    expect(api.currentNode.value).toBeNull()
    expect(api.currentStage.value).toBeNull()

    // 素材循环的进度事件带 stage=asset_loop（D7 §5.2）：界面上显示的是「素材循环」。
    stream.push(
      envelopeFrame(
        1,
        'job.progress',
        { node: 'asset_write', stage: 'asset_loop', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('asset_write')
    expect(api.currentStage.value).toBe('asset_loop')

    stream.push(frame(2, 'job.succeeded', { next: 'awaiting_human' }))
    await settle(5)
    expect(api.currentNode.value).toBeNull()
    expect(api.currentStage.value).toBeNull()
    api.stop()
  })

  it('补发的旧进度恢复节点，但不让 lastEventId 回退到游标之前', async () => {
    // 刷新场景：快照序号 9 就是游标，游标之后没有事件；服务端补发 id=7 的那条旧进度
    // （#271），节点因此仍然可显示。
    const stream = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(stream.response)
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      snapshotCursor: () => 9,
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    stream.push(
      envelopeFrame(
        7,
        'job.progress',
        { node: 'storyboard', stage: 'stage1', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('storyboard')
    expect(api.currentStage.value).toBe('stage1')
    // 旧序号不进入游标：仍是快照的 9，下次重连不会退回去重放已经覆盖的事件。
    expect(api.lastEventId.value).toBeNull()

    stream.push(
      envelopeFrame(
        10,
        'job.progress',
        { node: 'cinematographer', stage: 'stage1', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('cinematographer')
    expect(api.lastEventId.value).toBe(10)
    api.stop()
  })

  it('补发的旧进度不覆盖更新的节点（重连时游标之后已含新进度）', async () => {
    const stream = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(stream.response)
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    stream.push(
      envelopeFrame(
        5,
        'job.progress',
        { node: 'storyboard', stage: 'stage1', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    // 服务端在游标之后的批次之后补发一条更早的进度：序号更小，节点不回退。
    stream.push(
      envelopeFrame(
        3,
        'job.progress',
        { node: 'producer', stage: 'stage1', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('storyboard')
    expect(api.lastEventId.value).toBe(5)
    api.stop()
  })

  it('切会话后进度序号从头算：上一个会话的高序号不吞掉新会话的低序号', async () => {
    // 序号是**会话内**单调的。同一个订阅器实例先后接两个会话（详情页切会话时不重建
    // 组件），拿旧会话的序号当门槛会让新会话的进度全被丢掉。
    const first = makeStream()
    const second = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(first.response, second.response)
    let session = 'sess-1'
    const api = useSessionStream({
      sessionId: () => session,
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    first.push(
      envelopeFrame(
        40,
        'job.progress',
        { node: 'storyboard', stage: 'stage1', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('storyboard')

    api.stop()
    session = 'sess-2'
    api.start()
    await settle(5)
    second.push(
      envelopeFrame(
        2,
        'job.progress',
        { node: 'producer', stage: 'stage1', status: 'started' },
        PROGRESS_AT,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('producer')
    api.stop()
  })

  it('服务端包封里的进度：节点/stage 从 payload 取，started 时刻与上一步单独暴露（#272）', async () => {
    const stream = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(stream.response)
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    expect(api.currentNodeStartedAt.value).toBeNull()
    expect(api.lastDoneNode.value).toBeNull()

    const startedAt = '2026-11-14T07:48:39.557923+00:00'
    stream.push(
      envelopeFrame(
        3,
        'job.progress',
        { stage: 'stage1', node: 'producer', status: 'started' },
        startedAt,
      ),
    )
    await settle(5)
    // 生产形状是包封（payload 嵌套）；节点必须真能显示出来。
    expect(api.currentNode.value).toBe('producer')
    expect(api.currentStage.value).toBe('stage1')
    expect(api.currentNodeStartedAt.value).toBe(Date.parse(startedAt))
    expect(api.lastDoneNode.value).toBeNull()

    const doneAt = '2026-11-14T07:48:47.780613+00:00'
    stream.push(
      envelopeFrame(
        4,
        'job.progress',
        { stage: 'stage1', node: 'producer', status: 'done' },
        doneAt,
      ),
    )
    await settle(5)
    expect(api.lastDoneNode.value).toBe('producer')
    // done 之后节点不再是「当前」：否则刷新补发的最后一条 done 会同时挂成
    // 「当前」和「上一步」（#272）。
    expect(api.currentNode.value).toBeNull()
    expect(api.currentStage.value).toBeNull()
    // 节点跑完就没有「当前节点已耗时」了，下一跳 started 再重新起算。
    expect(api.currentNodeStartedAt.value).toBeNull()

    const nextAt = '2026-11-14T07:48:48.000000+00:00'
    stream.push(
      envelopeFrame(
        5,
        'job.progress',
        { stage: 'stage1', node: 'director', status: 'started' },
        nextAt,
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('director')
    expect(api.currentNodeStartedAt.value).toBe(Date.parse(nextAt))
    expect(api.lastDoneNode.value).toBe('producer')
    api.stop()
  })

  it('任务收尾清空当前节点与计时；切会话不把上一场的上一步带过去', async () => {
    const first = makeStream()
    const second = makeStream()
    const { impl, queue } = makeFetch()
    queue.push(first.response, second.response)
    let session = 'sess-1'
    const api = useSessionStream({
      sessionId: () => session,
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    first.push(
      envelopeFrame(
        1,
        'job.progress',
        { stage: 'stage1', node: 'producer', status: 'started' },
        '2026-11-14T07:48:39.000000+00:00',
      ),
    )
    first.push(
      envelopeFrame(
        2,
        'job.progress',
        { stage: 'stage1', node: 'producer', status: 'done' },
        '2026-11-14T07:48:47.000000+00:00',
      ),
    )
    await settle(5)
    expect(api.lastDoneNode.value).toBe('producer')

    first.push(frame(3, 'job.succeeded', { next: 'awaiting_human' }))
    await settle(5)
    expect(api.currentNode.value).toBeNull()
    expect(api.currentNodeStartedAt.value).toBeNull()
    expect(api.lastDoneNode.value).toBeNull()

    api.stop()
    session = 'sess-2'
    api.start()
    await settle(5)
    second.push(
      envelopeFrame(
        1,
        'job.progress',
        { stage: 'stage1', node: 'director', status: 'started' },
        '2026-11-14T08:00:00.000000+00:00',
      ),
    )
    await settle(5)
    expect(api.currentNode.value).toBe('director')
    expect(api.lastDoneNode.value).toBeNull()
    api.stop()
  })

  it('stop 之后不再重连', async () => {
    const stream = makeStream()
    const { impl, calls, queue } = makeFetch()
    queue.push(stream.response)
    const api = useSessionStream({
      sessionId: () => 'sess-1',
      onInvalidate: () => {},
      fetchImpl: impl,
      throttleMs: 5,
      reconnectDelayMs: 5,
    })

    api.start()
    await settle(5)
    stream.close()
    api.stop()
    await settle(30)

    expect(calls).toHaveLength(1)
    expect(api.connection.value).toBe('stopped')
  })
})
