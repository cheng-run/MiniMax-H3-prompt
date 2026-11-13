import { getCurrentInstance, onBeforeUnmount, ref, type InjectionKey } from 'vue'

import { ensureSessionToken, resetSessionToken, sessionEventsUrl } from '@/api/client'

import { SseParser, type SseFrame } from './sse'

// 工作台实时接入（D4 spec §6.2）：`fetch` + `ReadableStream` 读 SSE，不用 EventSource
// （它不能自定义请求头，启动令牌就只能进 URL）。
//
// 事件**只作失效信号**：收到就节流一阵去重拉快照，业务状态永远来自 `SessionDetail`，
// 这里不维护事件投影。唯一留下的流上信息是 `job.progress` 的节点名与连接状态——前者
// 是纯展示（快照里没有节点字段），后者是连接健康度，都不是业务状态。

export type InvalidateReason = 'event' | 'resync' | 'disconnect'
export type StreamConnection = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'stopped'

export interface SessionStreamOptions {
  sessionId: () => string
  /**
   * 首次订阅的补发起点：用当前快照的 `last_event_seq`，避免把快照已经覆盖的旧事件
   * 全部重放一遍（它们只会再触发一轮无意义的重拉）。
   */
  snapshotCursor?: () => number | null
  /**
   * 重拉快照的唯一出口。断线时先取快照再重订阅（spec §6.2 的顺序），所以这里返回
   * Promise 时会被等待；重同步请求（`X-Resync-Required`）走同一条路径。
   */
  onInvalidate: (reason: InvalidateReason) => void | Promise<void>
  /** 测试注入用；生产走全局 fetch。 */
  fetchImpl?: typeof fetch
  /** 事件合并窗口（默认 500ms）。 */
  throttleMs?: number
  /** 断线后的重订阅间隔（默认 1s）。 */
  reconnectDelayMs?: number
}

const DEFAULT_THROTTLE_MS = 500
const DEFAULT_RECONNECT_MS = 1000

/**
 * 事件流 fetch 的注入点：生产用全局 fetch；组件测试注入一份可控流，自己决定何时
 * 推帧、何时掐断（D4-AC15 的断线重连用例）。
 */
export const SESSION_STREAM_FETCH: InjectionKey<typeof fetch> = Symbol('session-stream-fetch')
const FINISH_EVENTS = new Set(['job.succeeded', 'job.failed', 'job.cancelled', 'job.interrupted'])

function parsePayload(data: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(data)
    return parsed !== null && typeof parsed === 'object'
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

export function useSessionStream(options: SessionStreamOptions) {
  const throttleMs = options.throttleMs ?? DEFAULT_THROTTLE_MS
  const reconnectDelayMs = options.reconnectDelayMs ?? DEFAULT_RECONNECT_MS
  const fetchImpl: typeof fetch = options.fetchImpl ?? ((input, init) => fetch(input, init))

  const connection = ref<StreamConnection>('idle')
  const currentNode = ref<string | null>(null)
  // 当前步骤标签（`job.progress` 的 stage）：纯展示，与节点名一样不是业务状态。
  const currentStage = ref<string | null>(null)
  const lastEventId = ref<number | null>(null)

  let running = false
  let stopped = false
  // 代际：切换会话 / stop 会让在途的旧连接作废。只靠 stopped 不够——stop 后紧接着
  // start 会把 stopped 重置回 false，旧请求的拒绝回调就会误以为「还可以重连」，与
  // 新连接并行跑出两条订阅（同一会话被失效两次，且旧会话的节点名会串进新会话）。
  let generation = 0
  let abortController: AbortController | null = null
  let throttleTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  async function report(reason: InvalidateReason): Promise<void> {
    try {
      await options.onInvalidate(reason)
    } catch {
      // 重拉失败由调用方的错误态呈现（快照 query 自己会报错）；订阅循环不因它中断，
      // 否则一次网络抖动就会把实时通道永久停掉。
    }
  }

  function scheduleInvalidate(): void {
    if (stopped || throttleTimer !== null) {
      return
    }
    throttleTimer = setTimeout(() => {
      throttleTimer = null
      void report('event')
    }, throttleMs)
  }

  function applyFrameId(id: string | null): void {
    if (id === null) {
      return
    }
    const seq = Number(id)
    if (!Number.isInteger(seq) || seq < 0) {
      return
    }
    // 重同步之后起点可能回落：只升不降，避免把补发起点退回到快照之前的旧事件。
    if (lastEventId.value === null || seq > lastEventId.value) {
      lastEventId.value = seq
    }
  }

  function handleFrame(frame: SseFrame): void {
    applyFrameId(frame.id)
    if (frame.event === 'job.progress') {
      const payload = parsePayload(frame.data)
      const node = payload?.node
      if (typeof node === 'string' && node.length > 0) {
        currentNode.value = node
      }
      const stage = payload?.stage
      if (typeof stage === 'string' && stage.length > 0) {
        currentStage.value = stage
      }
    } else if (FINISH_EVENTS.has(frame.event)) {
      currentNode.value = null
      currentStage.value = null
    }
    scheduleInvalidate()
  }

  async function readStream(body: ReadableStream<Uint8Array>, gen: number): Promise<void> {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    const parser = new SseParser()
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) {
          return
        }
        // 停止/切会话后旧连接可能还吐得出最后一两帧（abort 对已经拿到的 body 是尽力
        // 而为）：它们不属于当前会话，一律丢弃。
        if (gen !== generation || stopped) {
          return
        }
        for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
          handleFrame(frame)
        }
      }
    } catch {
      // 主动 abort 与网络中断都走断线路径；是否继续由代际与 stop 标记决定。
    } finally {
      try {
        reader.releaseLock()
      } catch {
        // 读还在途时 releaseLock 会抛；连接已经不可用，忽略。
      }
    }
  }

  function effectiveCursor(): number | null {
    return lastEventId.value ?? options.snapshotCursor?.() ?? null
  }

  async function connect(): Promise<void> {
    if (stopped) {
      return
    }
    const gen = generation
    const stale = (): boolean => gen !== generation || stopped
    const abort = new AbortController()
    abortController = abort
    let response: Response
    try {
      const headers: Record<string, string> = {
        Accept: 'text/event-stream',
        // 令牌只在请求头里；URL、查询串、浏览器历史都不带它（ADR 0033）。
        'X-Workbench-Token': await ensureSessionToken(),
      }
      if (stale()) {
        return
      }
      const cursor = effectiveCursor()
      if (cursor !== null) {
        headers['Last-Event-ID'] = String(cursor)
      }
      response = await fetchImpl(sessionEventsUrl(options.sessionId()), {
        headers,
        signal: abort.signal,
        cache: 'no-store',
      })
    } catch {
      if (!stale()) {
        await scheduleReconnect(gen)
      }
      return
    }
    if (stale()) {
      return
    }

    if (!response.ok || response.body === null) {
      // 服务重启会换掉每进程令牌：拿到 401 就丢掉缓存，下次重试重新引导，
      // 否则重连会一直用旧令牌撞死（client.ts 的注释说明了缓存行为）。
      if (response.status === 401) {
        resetSessionToken()
      }
      if (!stale()) {
        await scheduleReconnect(gen)
      }
      return
    }

    if (response.headers.get('X-Resync-Required') === 'true') {
      // 缺口补不上（事件文件被清掉、换了机器）：放弃补发，全量重取快照。
      lastEventId.value = null
      await report('resync')
      if (stale()) {
        return
      }
      // 快照已覆盖到新的尾号；流本身仍然有效（服务端从当前尾号继续），继续读即可。
      lastEventId.value = options.snapshotCursor?.() ?? null
    }

    connection.value = 'open'
    await readStream(response.body, gen)
    if (stale()) {
      return
    }

    // 断线：先重取快照，再带 Last-Event-ID 重订阅（spec §6.2 的顺序）。
    connection.value = 'reconnecting'
    await report('disconnect')
    if (stale()) {
      return
    }
    await scheduleReconnect(gen)
  }

  async function scheduleReconnect(gen: number): Promise<void> {
    connection.value = 'reconnecting'
    await new Promise<void>((resolve) => {
      reconnectTimer = setTimeout(resolve, reconnectDelayMs)
    })
    reconnectTimer = null
    if (gen === generation && !stopped) {
      void connect()
    }
  }

  function start(): void {
    if (running) {
      return
    }
    running = true
    stopped = false
    generation += 1
    connection.value = 'connecting'
    void connect()
  }

  function stop(): void {
    stopped = true
    running = false
    // 让在途连接与它的重连计时器失效（见 generation 的注释）。
    generation += 1
    abortController?.abort()
    abortController = null
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (throttleTimer !== null) {
      clearTimeout(throttleTimer)
      throttleTimer = null
    }
    connection.value = 'stopped'
  }

  // 组件里使用时随卸载断开；组件外（用例）由调用方显式 stop。
  if (getCurrentInstance() !== null) {
    onBeforeUnmount(stop)
  }

  return { connection, currentNode, currentStage, lastEventId, start, stop }
}
