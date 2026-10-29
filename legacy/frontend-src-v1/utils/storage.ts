/**
 * 本地存储：只放"刷新页面后还要在"的东西 —— 令牌和当前用户。
 *
 * 前端文档第八节：token + user 用 Pinia + localStorage。
 * Pinia 负责内存里的响应式状态，localStorage 负责刷新后恢复。
 */

import type { UserInfo } from '@/types/auth'

const TOKEN_KEY = 'ticket_agent_token'
const USER_KEY = 'ticket_agent_user'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function removeToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function getStoredUser(): UserInfo | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as UserInfo
  } catch {
    // 存的内容坏了就当作没有，别让整个应用起不来
    return null
  }
}

export function setStoredUser(user: UserInfo): void {
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

/** 退出登录 / 令牌失效时统一调用它 */
export function clearAuthStorage(): void {
  removeToken()
  localStorage.removeItem(USER_KEY)
}
