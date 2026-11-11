import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import {
  ApiError,
  createSession,
  getHealth,
  getSession,
  listSessions,
  resetSessionToken,
  startSessionJob,
} from './client'
import type { SessionCreate, SessionDetail, SessionSummary } from './client'

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

// 会话资源的端到端请求形状（#230）：list / create / get / start-job 都走生成客户端，
// 路径与查询、幂等头必须由这里钉住——组件不自己拼 URL。
const summaryFixture: SessionSummary = {
  session_id: 'sess-1',
  status: 'running',
  mode: 'base',
  variant: 'FL2VA',
  plot: '雨夜旧信',
  revision: 1,
  legacy: false,
  created_at: '2026-11-11T00:00:00Z',
  updated_at: '2026-11-11T00:01:00Z',
}

const detailFixture: SessionDetail = {
  session_id: 'sess-1',
  status: 'running',
  mode: 'base',
  variant: 'FL2VA',
  plot: '雨夜旧信',
  revision: 1,
  legacy: false,
  created_at: '2026-11-11T00:00:00Z',
  updated_at: '2026-11-11T00:01:00Z',
  last_event_seq: 3,
  brief: {
    mode: 'base',
    variant: 'FL2VA',
    duration: 5,
    style: 'Cinematic',
    language: 'Chinese',
    plot: '雨夜旧信',
    clarifications: '',
    draft: '',
    director_mode: 'off',
  },
}

const createBody: SessionCreate = {
  mode: 'base',
  plot: '雨夜旧信',
  duration: 5,
  variant: 'FL2VA',
  style: 'Cinematic',
  language: 'Chinese',
  clarifications: '',
  draft: '',
}

describe('api client 会话资源', () => {
  beforeEach(() => {
    resetSessionToken()
  })

  it('listSessions 传 limit/cursor 并解析分页', async () => {
    let query: URLSearchParams | null = null
    server.use(
      http.get('http://localhost/api/v1/sessions', ({ request }) => {
        query = new URL(request.url).searchParams
        return HttpResponse.json({ items: [summaryFixture], next_cursor: 'next-1' })
      }),
    )

    const page = await listSessions({ limit: 20, cursor: 'cursor-1' })

    expect(query!.get('limit')).toBe('20')
    expect(query!.get('cursor')).toBe('cursor-1')
    expect(page.items).toHaveLength(1)
    expect(page.items[0].session_id).toBe('sess-1')
    expect(page.next_cursor).toBe('next-1')
  })

  it('listSessions 不带参数时不发送查询串', async () => {
    let query: URLSearchParams | null = null
    server.use(
      http.get('http://localhost/api/v1/sessions', ({ request }) => {
        query = new URL(request.url).searchParams
        return HttpResponse.json({ items: [] })
      }),
    )

    const page = await listSessions()

    expect([...query!.keys()]).toEqual([])
    expect(page.items).toEqual([])
  })

  it('createSession 发送完整 brief 与 Idempotency-Key', async () => {
    let idempotencyKey: string | null = null
    let body: unknown = null
    server.use(
      http.post('http://localhost/api/v1/sessions', async ({ request }) => {
        idempotencyKey = request.headers.get('idempotency-key')
        body = await request.json()
        return HttpResponse.json(detailFixture, { status: 201 })
      }),
    )

    const created = await createSession(createBody, 'create-key-1')

    expect(idempotencyKey).toBe('create-key-1')
    expect(body).toMatchObject({
      mode: 'base',
      plot: '雨夜旧信',
      duration: 5,
      variant: 'FL2VA',
    })
    expect(created.session_id).toBe('sess-1')
  })

  it('getSession 走深链路径并解析详情', async () => {
    let pathname = ''
    server.use(
      http.get('http://localhost/api/v1/sessions/sess-1', ({ request }) => {
        pathname = new URL(request.url).pathname
        return HttpResponse.json(detailFixture)
      }),
    )

    const detail = await getSession('sess-1')

    expect(pathname).toBe('/api/v1/sessions/sess-1')
    expect(detail.status).toBe('running')
    expect(detail.brief.duration).toBe(5)
  })

  it('startSessionJob 投递 /jobs 并带幂等键', async () => {
    let idempotencyKey: string | null = null
    let method = ''
    server.use(
      http.post('http://localhost/api/v1/sessions/sess-1/jobs', ({ request }) => {
        idempotencyKey = request.headers.get('idempotency-key')
        method = request.method
        return HttpResponse.json(
          {
            job_id: 'job-1',
            status: 'running',
            kind: 'advance',
            started_at: '2026-11-11T00:00:00Z',
            finished_at: '',
            cancel_requested: false,
          },
          { status: 202 },
        )
      }),
    )

    const job = await startSessionJob('sess-1', 'job-key-1')

    expect(method).toBe('POST')
    expect(idempotencyKey).toBe('job-key-1')
    expect(job.job_id).toBe('job-1')
  })
})
