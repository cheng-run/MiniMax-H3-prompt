import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import { ApiError, getHealth, resetSessionToken } from './client'

// 测试模式下 baseUrl 固定为 http://localhost（见 client.ts）。
const url = 'http://localhost/api/v1/health'
const tokenUrl = 'http://localhost/api/v1/auth/token'

const okHealth = { status: 'ok', service: 'minimax-h3-prompt', version: '0.1.0' }

beforeEach(() => {
  // 令牌是模块级缓存：每个用例从「没取过」开始，才能数清引导端点被调了几次。
  resetSessionToken()
})

describe('api client', () => {
  it('每次 API 调用都自动带上启动令牌头', async () => {
    let seen: string | null = null
    server.use(
      http.get(url, ({ request }) => {
        seen = request.headers.get('x-workbench-token')
        return HttpResponse.json(okHealth)
      }),
    )

    await getHealth()

    expect(seen).toBe('test-session-token')
  })

  it('启动令牌只取一次，后续调用复用模块内缓存', async () => {
    let tokenCalls = 0
    server.use(
      http.get(tokenUrl, () => {
        tokenCalls += 1
        return HttpResponse.json({ token: 'cached-token' })
      }),
      http.get(url, () => HttpResponse.json(okHealth)),
    )

    await getHealth()
    await getHealth()

    expect(tokenCalls).toBe(1)
  })

  it('取令牌失败不缓存：下一次调用会重新取', async () => {
    let tokenCalls = 0
    server.use(
      http.get(tokenUrl, () => {
        tokenCalls += 1
        return HttpResponse.json(
          { title: '禁止访问', status: 403, detail: 'nope', instance: tokenUrl },
          { status: 403, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
      http.get(url, () => HttpResponse.json(okHealth)),
    )

    await expect(getHealth()).rejects.toBeInstanceOf(ApiError)
    await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

    expect(tokenCalls).toBe(2)
  })

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
