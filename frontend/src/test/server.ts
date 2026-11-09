import { setupServer } from 'msw/node'

// 空的 MSW 服务器：默认不放行任何请求，测试用 server.use(...) 逐条注入。
export const server = setupServer()
