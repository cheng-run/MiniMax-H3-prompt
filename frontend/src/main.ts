import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './app/App.vue'
import { router } from './router'
import './styles/reset.css'
import './styles/tokens.css'

const app = createApp(App)

// 状态边界（D1 spec §6）：
// - Pinia 只放客户端 UI 状态（导航、面板展开等）；
// - TanStack Vue Query 管 REST 请求的缓存/加载/错误；
// - 生成会话、执行任务等业务状态归 Python 服务端，绝不复制进 Pinia。
app.use(createPinia())
app.use(router)
app.use(VueQueryPlugin)

app.mount('#app')
