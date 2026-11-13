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
    stream.push(frame(1, 'job.progress', { node: 'storyboard', status: 'started' }))
    stream.push(frame(2, 'job.progress', { node: 'storyboard', status: 'done' }))
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

    stream.push(frame(51, 'job.progress', { node: 'cinematographer', status: 'started' }))
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
      frame(1, 'job.progress', { node: 'asset_write', stage: 'asset_loop', status: 'started' }),
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
