import { createRouter, createWebHistory, type RouterHistory, type RouteRecordRaw } from 'vue-router'

import NotFoundView from '@/app/NotFoundView.vue'
import SessionDetailView from '@/features/sessions/SessionDetailView.vue'
import SessionsListView from '@/features/sessions/SessionsListView.vue'

// createWebHistory：真实路径（非 hash），与 FastAPI 的 SPA fallback 配套——深链刷新
// 由服务端回退 index.html，前端路由再接管。
//
// 会话标识本身含斜杠（`<slug>/GEN001`），所以详情路由用自定义正则 `:id(.*)` 承接
// 整段路径，否则 `/sessions/a/B` 会匹配不上（被 catch-all 当成 404）。
const routes: RouteRecordRaw[] = [
  { path: '/', name: 'sessions', component: SessionsListView },
  { path: '/sessions/:id(.*)', name: 'session-detail', component: SessionDetailView },
  {
    path: '/diagnostics',
    name: 'diagnostics',
    component: () => import('@/features/diagnostics/DiagnosticsView.vue'),
  },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView },
]

// 工厂让测试用 createMemoryHistory 造隔离的路由，不污染浏览器历史与全局单例。
export function createAppRouter(history: RouterHistory = createWebHistory()) {
  return createRouter({ history, routes })
}

export const router = createAppRouter()
