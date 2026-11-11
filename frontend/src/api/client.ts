import createClient from 'openapi-fetch'
import type { Middleware } from 'openapi-fetch'

import type { components, paths } from './generated'

// 从 OpenAPI 契约导出的类型，不手写第二份模型（D1 spec §7.2）。
export type Health = components['schemas']['HealthResponse']
export type ProblemDetail = components['schemas']['ProblemDetail']
export type ProblemFieldError = components['schemas']['ProblemFieldError']
export type AuthToken = components['schemas']['AuthTokenResponse']
// 会话资源（D4-3）：列表/详情/创建/起任务全部走生成契约，组件不手写模型。
export type SessionSummary = components['schemas']['SessionSummary']
export type SessionDetail = components['schemas']['SessionDetail']
export type SessionPage = components['schemas']['SessionPage']
export type SessionCreate = components['schemas']['SessionCreate']
export type JobSummary = components['schemas']['JobSummary']
// 审查面（D4-5）与素材（D4-6）：形状都从契约导出，组件不手写模型。
export type ActionItemOut = components['schemas']['ActionItemOut']
export type ArtifactInfo = components['schemas']['ArtifactInfo']
export type ActionItemResolve = components['schemas']['ActionItemResolve']
export type RevisionIn = components['schemas']['RevisionIn']
export type Asset = components['schemas']['Asset']

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

// 启动令牌（D3-1）：每进程一份，只存在服务端内存里；页面打开后取一次，缓存在模块内。
// 它挡的是跨站页面，不是本机进程（ADR 0033）——所以这里不做「保密」，只做「自动带上」。
let sessionToken: string | null = null
let tokenInFlight: Promise<string> | null = null

async function fetchSessionToken(): Promise<string> {
  // 引导端点走裸 fetch：它自己不需要令牌，走 errorHandlingFetch 只会绕回来。
  const response = await fetch(`${baseUrl}/api/v1/auth/token`, { cache: 'no-store' })
  if (!response.ok) {
    throw new ApiError(response.status, await readProblem(response))
  }
  const body = (await response.json()) as Partial<AuthToken>
  if (typeof body.token !== 'string' || body.token.length === 0) {
    throw new ApiError(0, null)
  }
  return body.token
}

/**
 * 取（或复用）本页面的启动令牌。并发调用共享同一次请求，成功后一直复用。
 *
 * 注意：服务重启后旧令牌失效，而缓存里仍是**成功取到的**旧值——页面需要刷新
 * （或由调用方 `resetSessionToken()`）才会重新引导。失败不缓存，下一次调用重试。
 */
export function ensureSessionToken(): Promise<string> {
  if (sessionToken !== null) {
    return Promise.resolve(sessionToken)
  }
  if (tokenInFlight === null) {
    tokenInFlight = fetchSessionToken()
      .then((token) => {
        sessionToken = token
        return token
      })
      .finally(() => {
        tokenInFlight = null
      })
  }
  return tokenInFlight
}

/** 清掉模块内缓存；用例用它模拟「新页面」或「服务重启」，生产代码不调用。 */
export function resetSessionToken(): void {
  sessionToken = null
  tokenInFlight = null
}

// 所有 API 调用自动带凭据（fetch / SSE 走请求头；媒体元素靠引导端点下发的 Cookie）。
const attachToken: Middleware = {
  async onRequest({ request }) {
    request.headers.set('X-Workbench-Token', await ensureSessionToken())
  },
}

export const apiClient = createClient<paths>({ baseUrl, fetch: errorHandlingFetch })
// 本仓锁的 openapi-fetch 0.13 不接受构造参数里的 middleware，只能事后注册。
apiClient.use(attachToken)

export async function getHealth(): Promise<Health> {
  const { data } = await apiClient.GET('/api/v1/health')
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}

/** 会话列表分页参数（服务端默认 limit=50，上限 200）。 */
export interface ListSessionsParams {
  limit?: number
  cursor?: string | null
}

/**
 * 取一页会话。分页游标由服务端下发的不透明串持有，前端只原样回传。
 *
 * 只把有值的键放进查询串：不传参数时不应出现 `?limit=undefined`（服务端会 422）。
 */
export async function listSessions(params: ListSessionsParams = {}): Promise<SessionPage> {
  const query: { limit?: number; cursor?: string } = {}
  if (params.limit !== undefined) {
    query.limit = params.limit
  }
  if (params.cursor !== undefined && params.cursor !== null) {
    query.cursor = params.cursor
  }
  const { data } = await apiClient.GET('/api/v1/sessions', { params: { query } })
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}

/**
 * 建会话。`idempotencyKey` 必传：同一个键配同一个请求体只会建出一条会话（防双击）。
 */
export async function createSession(
  body: SessionCreate,
  idempotencyKey: string,
): Promise<SessionDetail> {
  const { data } = await apiClient.POST('/api/v1/sessions', {
    body,
    params: { header: { 'Idempotency-Key': idempotencyKey } },
  })
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}

/**
 * 会话事件的流地址（D4 spec §6.2）。事件流不走生成客户端（它只管 JSON 请求/响应），
 * 但同源前缀与测试 base 必须与 API 客户端同一份约定，所以 URL 在这里拼。
 *
 * 会话标识含斜杠（`<slug>/GEN001`），逐段编码后仍保留路径分隔——与 `:path` 参数一致。
 */
export function sessionEventsUrl(sessionId: string): string {
  const encoded = sessionId.split('/').map(encodeURIComponent).join('/')
  return `${baseUrl}/api/v1/sessions/${encoded}/events`
}

/** 取会话快照：工作台唯一的数据源（spec §6.1），刷新即重拉。 */
export async function getSession(sessionId: string): Promise<SessionDetail> {
  const { data } = await apiClient.GET('/api/v1/sessions/{session_id}', {
    params: { path: { session_id: sessionId } },
  })
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}

/**
 * 起一次 `advance` 任务。幂等键挡住重复点击产生的第二条任务；重复提交同键只会重放。
 */
export async function startSessionJob(
  sessionId: string,
  idempotencyKey: string,
): Promise<JobSummary> {
  const { data } = await apiClient.POST('/api/v1/sessions/{session_id}/jobs', {
    params: {
      path: { session_id: sessionId },
      header: { 'Idempotency-Key': idempotencyKey },
    },
  })
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}

/**
 * 请求取消（D4 spec §6.3）：**协作式**——接口 202 只表示「已登记」，任务要在当前
 * 节点跑完后才停。界面文案不许承诺立即停止（ADR 0032）。
 */
export async function cancelSessionJob(sessionId: string, jobId: string): Promise<JobSummary> {
  const { data } = await apiClient.POST('/api/v1/sessions/{session_id}/jobs/{job_id}/cancel', {
    params: { path: { session_id: sessionId, job_id: jobId } },
  })
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}

/** 会话标识本身含斜杠（`<slug>/GEN001`）：逐段编码后仍保留路径分隔。 */
function encodeSessionPath(identifier: string): string {
  return identifier.split('/').map(encodeURIComponent).join('/')
}

/**
 * 素材的媒体地址（D3 §4.5）：`<img>` / `<video>` / `<audio>` 由浏览器自己发请求，
 * 挂不上自定义头，只认 `/auth/token` 顺带下发的 `wb_session` Cookie。
 * 凭据不进 URL；URL 与测试 base 跟其余端点共用同一份约定。
 */
export function assetMediaUrl(assetId: string): string {
  return `${baseUrl}/api/v1/media/${encodeSessionPath(assetId)}`
}

/**
 * 产物的下载地址（D4 spec §10）：下载 = content 端点原样落盘。
 *
 * `<a download>` 由浏览器自己发请求，挂不上自定义头，只能靠 `/auth/token` 顺带下发的
 * `wb_session` Cookie（与素材媒体同一份约定，ADR 0033）；URL 里也不放凭据。
 * 产物标识同样带会话斜杠，逐段编码后保留路径分隔（端点参数是 `{artifact_id:path}`）。
 */
export function artifactContentUrl(artifactId: string): string {
  return `${baseUrl}/api/v1/artifacts/${encodeSessionPath(artifactId)}/content`
}

/**
 * 上传一份素材（复用 D3 端点，D4-AC13）。
 *
 * 走裸 fetch 而不是 `apiClient.POST`：生成的 multipart 类型把 `file` 声明成字符串
 * （format: binary），真正要发的是 `FormData`；用 openapi-fetch 得靠一个占位 body
 * 加 bodySerializer 绕过类型。这里复用同一份错误拦截与令牌注入，返回形状仍由契约
 * 类型约束。
 */
export async function uploadSessionAsset(sessionId: string, file: File): Promise<Asset> {
  const form = new FormData()
  form.append('file', file)
  const response = await errorHandlingFetch(
    `${baseUrl}/api/v1/sessions/${encodeSessionPath(sessionId)}/assets`,
    {
      method: 'POST',
      headers: { 'X-Workbench-Token': await ensureSessionToken() },
      body: form,
    },
  )
  return (await response.json()) as Asset
}

/**
 * 读文本产物全文（D4 spec §7）。端点按产物形态分流：文本走 ``text/plain``（1 MiB 上限），
 * 媒体走字节 + Range；审查面只读文本，所以这里显式要 ``text``——生成客户端默认按
 * JSON 解析，纯文本会被当成坏 JSON 直接抛错。
 */
export async function getArtifactContent(artifactId: string): Promise<string> {
  const { data } = await apiClient.GET('/api/v1/artifacts/{artifact_id}/content', {
    params: { path: { artifact_id: artifactId } },
    parseAs: 'text',
  })
  if (data === undefined) {
    throw new ApiError(0, null)
  }
  return data
}
/**
 * 提交人工决定（D4 spec §8 / §9）。审查待办收 `decision`，帧待办收 `frames`；
 * 帧链通过审查时服务端不建任务，202 的响应体因此可能是 `null`（不是错误）。
 * 幂等键可选：审查卡带键挡双击，帧提交卡自身已禁用重复提交。
 */
export async function resolveActionItem(
  sessionId: string,
  itemId: string,
  body: ActionItemResolve,
  idempotencyKey?: string,
): Promise<JobSummary | null> {
  const { data } = await apiClient.POST(
    '/api/v1/sessions/{session_id}/action-items/{item_id}/resolve',
    {
      params: {
        path: { session_id: sessionId, item_id: itemId },
        ...(idempotencyKey ? { header: { 'Idempotency-Key': idempotencyKey } } : {}),
      },
      body,
    },
  )
  return data ?? null
}
