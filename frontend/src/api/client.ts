import createClient from 'openapi-fetch'

import type { components, paths } from './generated'

// 从 OpenAPI 契约导出的类型，不手写第二份模型（D1 spec §7.2）。
export type Health = components['schemas']['HealthResponse']
export type ProblemDetail = components['schemas']['ProblemDetail']
export type ProblemFieldError = components['schemas']['ProblemFieldError']

/**
 * 统一 API 错误。组件只处理这一种错误，不各自识别 FastAPI 默认的 ``detail``。
 * ``status = 0`` 表示网络层失败（连不上服务），此时 ``problem`` 为 ``null``。
 */
export class ApiError extends Error {
  readonly status: number
  readonly problem: ProblemDetail | null

  constructor(status: number, problem: ProblemDetail | null) {
    super(problem?.title ?? (status === 0 ? '网络错误，无法连接服务' : '请求失败'))
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
  }
}

async function readProblem(response: Response): Promise<ProblemDetail | null> {
  const contentType = response.headers.get('content-type') ?? ''
  // 只有 JSON 形态（含 application/problem+json）才解析；其他一律安全兜底为 null。
  if (!contentType.includes('json')) {
    return null
  }
  try {
    return (await response.json()) as ProblemDetail
  } catch {
    return null
  }
}

// 在 fetch 层统一拦截失败：非 2xx 解析 problem+json 抛 ApiError；网络失败抛
// status=0。成功响应原样交给 openapi-fetch 解析（类型来自契约）。这样错误处理只
// 有一处，组件拿到的永远是 ApiError。
const errorHandlingFetch: typeof fetch = async (input, init) => {
  let response: Response
  try {
    response = await fetch(input, init)
  } catch {
    throw new ApiError(0, null)
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readProblem(response))
  }
  return response
}

// 浏览器里同源访问，baseUrl 留空；调用使用生成文件里的完整路径（含 /api/v1）。
// vitest（jsdom）里没有同源 origin，相对 URL 无法解析，因此仅在测试模式给一个固定
// base——生产构建 MODE 不是 'test'，baseUrl 仍为空。
const baseUrl = import.meta.env.MODE === 'test' ? 'http://localhost' : ''

export const apiClient = createClient<paths>({ baseUrl, fetch: errorHandlingFetch })

export async function getHealth(): Promise<Health> {
  const { data } = await apiClient.GET('/api/v1/health')
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}
