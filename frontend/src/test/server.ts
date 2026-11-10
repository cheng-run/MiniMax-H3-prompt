import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

// 默认放行引导端点：D3-1 起客户端在第一次 API 调用前会取一次启动令牌并缓存，
// 每个用例都重声明一遍只会把真正想测的请求淹没。
const tokenHandler = http.get('http://localhost/api/v1/auth/token', () =>
  HttpResponse.json({ token: 'test-session-token' }),
)

// MSW 服务器：默认只放行引导端点，其余请求由测试用 server.use(...) 逐条注入。
export const server = setupServer(tokenHandler)
