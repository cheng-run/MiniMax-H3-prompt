// SSE 帧解析（D4 spec §6.2）：浏览器侧用 fetch + ReadableStream 读事件流，不引
// EventSource（它不能带请求头，令牌就只能进 URL）。这里只做「字节 → 帧」的最小解析：
// 业务状态不进投影，调用方拿帧上的 id / event / data 就够了。

export interface SseFrame {
  /** ``id:`` 字段；没有时为 null（服务端每帧都会给，只有测试帧可能省略）。 */
  id: string | null
  /** ``event:`` 字段；缺省按 SSE 规范是 ``message``。 */
  event: string
  /** 多行 ``data:`` 用换行拼接。 */
  data: string
}

/**
 * 增量解析器：网络分片不保证落在帧边界上，残留的半帧留在 ``buffer`` 里等下一片。
 *
 * 行分隔符按规范接受 ``\r\n`` / ``\n`` / ``\r``；跨分片的 ``\r\n`` 不能拆成
 * 「换行 + 空行」（那会凭空多切出一帧），所以末尾的 ``\r`` 先扣住不归一化。
 */
export class SseParser {
  private buffer = ''

  push(chunk: string): SseFrame[] {
    this.buffer += chunk
    let held = ''
    if (this.buffer.endsWith('\r')) {
      held = '\r'
      this.buffer = this.buffer.slice(0, -1)
    }
    this.buffer = this.buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n') + held

    const frames: SseFrame[] = []
    let boundary = this.buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const raw = this.buffer.slice(0, boundary)
      this.buffer = this.buffer.slice(boundary + 2)
      const frame = parseFrame(raw)
      if (frame !== null) {
        frames.push(frame)
      }
      boundary = this.buffer.indexOf('\n\n')
    }
    return frames
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event = 'message'
  let id: string | null = null
  const data: string[] = []
  for (const line of raw.split('\n')) {
    // 空行与注释行（心跳、resync 提示）都不携带事件数据。
    if (line === '' || line.startsWith(':')) {
      continue
    }
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) {
      value = value.slice(1)
    }
    if (field === 'event') {
      event = value
    } else if (field === 'id') {
      id = value
    } else if (field === 'data') {
      data.push(value)
    }
  }
  // 没有 data 的帧不是事件：服务端的心跳/重同步提示都是纯注释行。
  if (data.length === 0) {
    return null
  }
  return { id, event, data: data.join('\n') }
}
