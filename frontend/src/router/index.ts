import { createRouter, createWebHistory } from 'vue-router'

import HomeView from '@/app/HomeView.vue'
import NotFoundView from '@/app/NotFoundView.vue'

// createWebHistory：真实路径（非 hash），与 FastAPI 的 SPA fallback 配套——深链刷新
// 由服务端回退 index.html，前端路由再接管。
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    {
      path: '/diagnostics',
      name: 'diagnostics',
      component: () => import('@/features/diagnostics/DiagnosticsView.vue'),
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView },
  ],
})
