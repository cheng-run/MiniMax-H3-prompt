import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './app/App.vue'
import { ensureSessionToken } from './api/client'
import { router } from './router'
import './styles/reset.css'
import './styles/tokens.css'

// 页面一启动就取启动令牌：引导端点同时下发 wb_session Cookie，`<video>` / `<img>`
// 由浏览器自己发请求、挂不上自定义头，只能靠这个 Cookie（D3-1 / ADR 0033）。
// 失败不阻塞渲染：令牌不进模块缓存，第一次 API 调用会自动重试。
void ensureSessionToken().catch(() => {})

const app = createApp(App)

// 状态边界（D1 spec §6）：
// - Pinia 只放客户端 UI 状态（导航、面板展开等）；
// - TanStack Vue Query 管 REST 请求的缓存/加载/错误；
// - 生成会话、执行任务等业务状态归 Python 服务端，绝不复制进 Pinia。
app.use(createPinia())
app.use(router)
app.use(VueQueryPlugin)

app.mount('#app')
