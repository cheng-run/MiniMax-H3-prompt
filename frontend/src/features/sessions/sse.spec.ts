import { describe, expect, it } from 'vitest'

import { SseParser } from './sse'

// 服务端帧形状（workbench_store.Event.sse_frame）：id / event / data 三行 + 空行。
describe('SseParser', () => {
  it('解析一整帧：id/event/data 都取到', () => {
    const parser = new SseParser()
    const frames = parser.push('id: 7\nevent: job.progress\ndata: {"node":"storyboard"}\n\n')
    expect(frames).toEqual([{ id: '7', event: 'job.progress', data: '{"node":"storyboard"}' }])
  })

  it('跨 chunk 的半帧先攒着，补齐后只发一次', () => {
    const parser = new SseParser()
    expect(parser.push('id: 3\nevent: job.st')).toEqual([])
    expect(parser.push('arted\ndata: {}\n\n')).toEqual([
      { id: '3', event: 'job.started', data: '{}' },
    ])
  })

  it('一次 chunk 里多帧按顺序全部返回', () => {
    const parser = new SseParser()
    const frames = parser.push('id: 1\nevent: a\ndata: {}\n\nid: 2\nevent: b\ndata: {}\n\n')
    expect(frames.map((frame) => frame.id)).toEqual(['1', '2'])
  })

  it('心跳注释行不是事件', () => {
    const parser = new SseParser()
    expect(parser.push(': heartbeat\n\n')).toEqual([])
  })

  it('兼容 CRLF 与 CR 换行（含跨 chunk 的 \\r\\n）', () => {
    const parser = new SseParser()
    expect(parser.push('id: 5\r\nevent: a\r\ndata: {}\r\n\r\n')).toHaveLength(1)
    expect(parser.push('id: 6\r')).toEqual([])
    expect(parser.push('\nevent: b\ndata: {}\n\n')).toEqual([{ id: '6', event: 'b', data: '{}' }])
  })

  it('没有 data 的帧（只有注释/字段）不派发', () => {
    const parser = new SseParser()
    expect(parser.push('event: ping\n\n')).toEqual([])
  })

  it('多行 data 用换行拼接', () => {
    const parser = new SseParser()
    expect(parser.push('data: 第一行\ndata: 第二行\n\n')).toEqual([
      { id: null, event: 'message', data: '第一行\n第二行' },
    ])
  })
})
