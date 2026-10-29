/**
 * 路由 + 路由守卫（前端文档第十节）。
 *
 *   /login            登录页（不需要登录）
 *   /tickets          工单列表
 *   /tickets/create   创建工单
 *   /tickets/:id      工单详情
 *   /                 自动跳 /tickets
 */

import { createRouter, createWebHistory } from 'vue-router'

import MainLayout from '@/layouts/MainLayout.vue'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/Login.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      component: MainLayout,
      redirect: '/tickets',
      children: [
        {
          path: 'tickets',
          name: 'ticket-list',
          component: () => import('@/views/tickets/TicketList.vue'),
          meta: { title: '工单管理' },
        },
        {
          path: 'tickets/create',
          name: 'ticket-create',
          component: () => import('@/views/tickets/TicketCreate.vue'),
          meta: { title: '创建工单' },
        },
        {
          path: 'tickets/:id',
          name: 'ticket-detail',
          component: () => import('@/views/tickets/TicketDetail.vue'),
          meta: { title: '工单详情' },
        },
      ],
    },
    // 兜底：乱七八糟的路径都回工单列表
    { path: '/:pathMatch(.*)*', redirect: '/tickets' },
  ],
})

// ----------------------------------------------------------------------
// 守卫：没登录不让进业务页面；已登录不让回登录页
// ----------------------------------------------------------------------
router.beforeEach((to) => {
  const auth = useAuthStore()

  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (to.name === 'login' && auth.isLoggedIn) {
    return { name: 'ticket-list' }
  }

  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · 工单智能处理平台` : '工单智能处理平台'
})

export default router
