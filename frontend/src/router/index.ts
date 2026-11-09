import { createRouter, createWebHistory, type RouterHistory, type RouteRecordRaw } from 'vue-router'

import HomeView from '@/app/HomeView.vue'
import NotFoundView from '@/app/NotFoundView.vue'

// createWebHistory：真实路径（非 hash），与 FastAPI 的 SPA fallback 配套——深链刷新
// 由服务端回退 index.html，前端路由再接管。
const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: HomeView },
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
