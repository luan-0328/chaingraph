/**
 * 登录状态（Pinia）。
 *
 * 前端文档第四十/四十一节的原则：
 *   Auth  → 全局 Store（因为每个页面都要用、刷新后要恢复）
 *   Ticket → 页面自己维护（服务端数据，放全局反而同步困难）
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { getCurrentUser, login as loginApi } from '@/api/auth'
import type { LoginRequest, UserInfo } from '@/types/auth'
import { clearAuthStorage, getStoredUser, getToken, setStoredUser, setToken } from '@/utils/storage'

export const useAuthStore = defineStore('auth', () => {
  // 初始值直接从 localStorage 读 —— 这样刷新页面不会掉登录态
  const token = ref<string | null>(getToken())
  const user = ref<UserInfo | null>(getStoredUser())

  const isLoggedIn = computed(() => Boolean(token.value))
  const username = computed(() => user.value?.username ?? '')

  /** 登录：调后端换 JWT，然后同时写 Pinia 和 localStorage */
  async function login(payload: LoginRequest): Promise<void> {
    const result = await loginApi(payload)
    token.value = result.access_token
    user.value = result.user
    setToken(result.access_token)
    setStoredUser(result.user)
  }

  /** 退出登录：本地清干净（后端是无状态 JWT，不需要通知服务端） */
  function logout(): void {
    token.value = null
    user.value = null
    clearAuthStorage()
  }

  /** 刷新页面后校验令牌是否还有效，顺便刷新用户信息 */
  async function fetchCurrentUser(): Promise<void> {
    if (!token.value) return
    try {
      const info = await getCurrentUser()
      user.value = info
      setStoredUser(info)
    } catch {
      // 401 会被拦截器统一处理（清登录态 + 回登录页），这里不用重复处理
    }
  }

  return { token, user, isLoggedIn, username, login, logout, fetchCurrentUser }
})
