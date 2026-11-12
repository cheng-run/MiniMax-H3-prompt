import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

// 默认放行引导端点：D3-1 起客户端在第一次 API 调用前会取一次启动令牌并缓存，
// 每个用例都重声明一遍只会把真正想测的请求淹没。
const tokenHandler = http.get('http://localhost/api/v1/auth/token', () =>
  HttpResponse.json({ token: 'test-session-token' }),
)

// 首页即会话列表（D4-3）：挂载 App 的用例都会打到列表端点。给一个空列表的默认桩，
// 让「只想测壳」的用例不必各自声明；想测具体数据仍用 server.use 覆盖（优先级更高）。
const emptySessionsHandler = http.get('http://localhost/api/v1/sessions', () =>
  HttpResponse.json({ items: [] }),
)

// 健康资源（D5-3 / #238 起概览页也读它：missing_keys 非空就显示配密钥提示）。
// 默认桩是「密钥齐全」形态；缺密钥的用例自己 server.use 覆盖。
const healthHandler = http.get('http://localhost/api/v1/health', () =>
  HttpResponse.json({
    status: 'ok',
    service: 'minimax-h3-prompt',
    version: '0.1.0',
    data_root: 'D:/app/data',
    config_path: 'D:/app/data/config/agent.yaml',
    env_path: 'C:/Users/tester/AppData/Roaming/MiniMax-H3-prompt/.env',
    missing_keys: [],
  }),
)

// MSW 服务器：默认只放行引导端点、空会话列表与健康资源，其余请求由测试用
// server.use(...) 逐条注入。
export const server = setupServer(tokenHandler, emptySessionsHandler, healthHandler)
