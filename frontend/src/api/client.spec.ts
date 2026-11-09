import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import { ApiError, getHealth } from './client'

// 测试模式下 baseUrl 固定为 http://localhost（见 client.ts）。
const url = 'http://localhost/api/v1/health'

describe('api client', () => {
  it('成功时解析健康资源', async () => {
    server.use(
      http.get(url, () =>
        HttpResponse.json({ status: 'ok', service: 'minimax-h3-prompt', version: '0.1.0' }),
      ),
    )

    const health = await getHealth()

    expect(health.status).toBe('ok')
    expect(health.service).toBe('minimax-h3-prompt')
    expect(health.version).toBe('0.1.0')
  })

  it('problem+json 错误解析为 ApiError（含 status 与 problem）', async () => {
    server.use(
      http.get(url, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: '资源不存在',
            status: 404,
            detail: 'nope',
            instance: '/api/v1/health',
          },
          { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const err = (await getHealth().catch((e: unknown) => e)) as ApiError

    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(404)
    expect(err.problem?.status).toBe(404)
    expect(err.problem?.title).toBe('资源不存在')
  })

  it('非 JSON 错误兜底为 problem=null', async () => {
    server.use(
      http.get(
        url,
        () =>
          new HttpResponse('bad gateway', {
            status: 502,
            headers: { 'Content-Type': 'text/plain' },
          }),
      ),
    )

    const err = (await getHealth().catch((e: unknown) => e)) as ApiError

    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(502)
    expect(err.problem).toBeNull()
  })

  it('网络错误兜底为 status=0、problem=null', async () => {
    server.use(http.get(url, () => HttpResponse.error()))

    const err = (await getHealth().catch((e: unknown) => e)) as ApiError

    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(0)
    expect(err.problem).toBeNull()
  })
})
