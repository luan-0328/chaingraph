/**
 * 登录态（Pinia）。
 *
 * Auth 放全局 Store（每个页面都要用、刷新后要恢复）；
 * 业务数据（商品/订单/工单）由各页面自己请求维护 —— 它们是服务端数据，
 * 放全局反而同步困难（v2.md 的前端原则）。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { getCurrentUser, login as loginApi, register as registerApi } from '@/api/auth'
import type { LoginRequest, RegisterRequest, UserInfo, UserRole } from '@/types'
import {
  clearAuthStorage,
  getStoredUser,
  getToken,
  setStoredUser,
  setToken,
} from '@/utils/storage'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getToken())
  const user = ref<UserInfo | null>(getStoredUser())

  /**
   * 购物车角标数量：这只是"顶部导航上的一个小红点"，
   * 真正的购物车数据仍在服务端，由页面调 GET /cart 获取。
   * 放在这里是因为布局（UserLayout）需要它，而布局不方便自己持状态。
   */
  const cartCount = ref(0)
  function setCartCount(count: number): void {
    cartCount.value = Math.max(0, count)
  }

  const isLoggedIn = computed(() => Boolean(token.value))
  const username = computed(() => user.value?.nickname || user.value?.username || '')
  const role = computed<UserRole>(() => user.value?.role ?? 'USER')
  const isStaff = computed(() => role.value === 'CUSTOMER_SERVICE' || role.value === 'ADMIN')
  const isAdmin = computed(() => role.value === 'ADMIN')

  /** 登录后落到哪个首页（客服/管理员直接进工作台） */
  const homeRoute = computed(() => {
    if (role.value === 'ADMIN') return { name: 'admin-products' }
    if (role.value === 'CUSTOMER_SERVICE') return { name: 'cs-workbench' }
    return { name: 'product-list' }
  })

  async function login(payload: LoginRequest): Promise<UserInfo> {
    const result = await loginApi(payload)
    token.value = result.access_token
    user.value = result.user
    setToken(result.access_token)
    setStoredUser(result.user)
    return result.user
  }

  async function register(payload: RegisterRequest): Promise<void> {
    await registerApi(payload)
  }

  function logout(): void {
    token.value = null
    user.value = null
    clearAuthStorage()
  }

  async function fetchCurrentUser(): Promise<void> {
    if (!token.value) return
    try {
      const info = await getCurrentUser()
      user.value = info
      setStoredUser(info)
    } catch {
      // 401 已由拦截器统一处理
    }
  }

  return {
    token,
    user,
    cartCount,
    isLoggedIn,
    isStaff,
    isAdmin,
    username,
    role,
    homeRoute,
    login,
    register,
    logout,
    fetchCurrentUser,
    setCartCount,
  }
})
