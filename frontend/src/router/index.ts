/**
 * 路由 + 守卫（v2.md §49-§52）。
 *
 * 三套界面按角色分区：
 *   /               用户端（商城 + 我的订单 + 售后 + 我的工单）
 *   /customer-service  客服工作台（工单/订单/用户/售后/知识库）
 *   /admin          管理端（商品/订单/工单/用户/知识库）
 *
 * 注意 v2.md §58：前端路由只是"少显示菜单"，
 * 真正的权限由后端 require_roles 再校验一次。
 */

import { createRouter, createWebHistory } from 'vue-router'

import CustomerServiceLayout from '@/layouts/CustomerServiceLayout.vue'
import UserLayout from '@/layouts/UserLayout.vue'
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
      path: '/register',
      name: 'register',
      component: () => import('@/views/Register.vue'),
      meta: { public: true, title: '注册' },
    },

    // ---------------- 用户端 ----------------
    {
      path: '/',
      component: UserLayout,
      children: [
        { path: '', redirect: { name: 'product-list' } },
        {
          path: 'products',
          name: 'product-list',
          component: () => import('@/views/user/ProductList.vue'),
          meta: { title: '商品' },
        },
        {
          path: 'products/:id',
          name: 'product-detail',
          component: () => import('@/views/user/ProductDetail.vue'),
          meta: { title: '商品详情' },
        },
        {
          path: 'cart',
          name: 'cart',
          component: () => import('@/views/user/Cart.vue'),
          meta: { title: '购物车' },
        },
        {
          path: 'orders',
          name: 'order-list',
          component: () => import('@/views/user/OrderList.vue'),
          meta: { title: '我的订单' },
        },
        {
          path: 'orders/:id',
          name: 'order-detail',
          component: () => import('@/views/user/OrderDetail.vue'),
          meta: { title: '订单详情' },
        },
        {
          path: 'after-sales',
          name: 'after-sale-list',
          component: () => import('@/views/user/AfterSaleList.vue'),
          meta: { title: '我的售后' },
        },
        {
          path: 'tickets',
          name: 'ticket-list',
          component: () => import('@/views/user/TicketList.vue'),
          meta: { title: '我的工单' },
        },
        {
          path: 'tickets/create',
          name: 'ticket-create',
          component: () => import('@/views/user/TicketCreate.vue'),
          meta: { title: '提交问题' },
        },
        {
          path: 'tickets/:id',
          name: 'ticket-detail',
          component: () => import('@/views/user/TicketDetail.vue'),
          meta: { title: '工单详情' },
        },
        {
          path: 'profile',
          name: 'profile',
          component: () => import('@/views/Profile.vue'),
          meta: { title: '个人信息' },
        },
      ],
    },

    // ---------------- 客服工作台 ----------------
    {
      path: '/customer-service',
      component: CustomerServiceLayout,
      meta: { roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
      children: [
        { path: '', redirect: { name: 'cs-workbench' } },
        {
          path: 'workbench',
          name: 'cs-workbench',
          component: () => import('@/views/customer-service/Workbench.vue'),
          meta: { title: '工作台', roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
        },
        {
          path: 'tickets',
          name: 'cs-ticket-list',
          component: () => import('@/views/customer-service/TicketList.vue'),
          meta: { title: '工单处理', roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
        },
        {
          path: 'tickets/:id',
          name: 'cs-ticket-detail',
          component: () => import('@/views/customer-service/TicketDetail.vue'),
          meta: { title: '工单详情', roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
        },
        {
          path: 'orders',
          name: 'cs-order-list',
          component: () => import('@/views/customer-service/OrderList.vue'),
          meta: { title: '订单查询', roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
        },
        {
          path: 'after-sales',
          name: 'cs-after-sale-list',
          component: () => import('@/views/customer-service/AfterSaleList.vue'),
          meta: { title: '售后审核', roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
        },
        {
          path: 'users',
          name: 'cs-user-list',
          component: () => import('@/views/customer-service/UserList.vue'),
          meta: { title: '用户查询', roles: ['CUSTOMER_SERVICE', 'ADMIN'] },
        },
      ],
    },

    // ---------------- 管理端 ----------------
    {
      path: '/admin',
      component: CustomerServiceLayout,
      meta: { roles: ['ADMIN'] },
      children: [
        { path: '', redirect: { name: 'admin-products' } },
        {
          path: 'products',
          name: 'admin-products',
          component: () => import('@/views/admin/ProductManage.vue'),
          meta: { title: '商品管理', roles: ['ADMIN'] },
        },
        {
          path: 'orders',
          name: 'admin-orders',
          component: () => import('@/views/admin/OrderManage.vue'),
          meta: { title: '订单总览', roles: ['ADMIN'] },
        },
        {
          path: 'tickets',
          name: 'admin-tickets',
          component: () => import('@/views/admin/TicketManage.vue'),
          meta: { title: '工单总览', roles: ['ADMIN'] },
        },
        {
          path: 'users',
          name: 'admin-users',
          component: () => import('@/views/admin/UserManage.vue'),
          meta: { title: '用户总览', roles: ['ADMIN'] },
        },
        {
          path: 'knowledge',
          name: 'admin-knowledge',
          component: () => import('@/views/admin/KnowledgeManage.vue'),
          meta: { title: '知识库管理', roles: ['ADMIN'] },
        },
      ],
    },

    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()

  // 未登录 → 登录页（记住原本要去哪里）
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 已登录还去登录页 → 回各自首页
  if ((to.name === 'login' || to.name === 'register') && auth.isLoggedIn) {
    return auth.homeRoute
  }

  // 角色不匹配 → 回自己首页（后端还会再校验一次，这里只是体验）
  const required = to.meta.roles as string[] | undefined
  if (required && auth.isLoggedIn && !required.includes(auth.role)) {
    return auth.homeRoute
  }

  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · 智能电商客服与售后工单平台` : '智能电商客服与售后工单平台'
})

export default router
