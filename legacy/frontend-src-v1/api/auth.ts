/**
 * 鉴权接口。路径以真实后端为准：app/api/auth.py
 */

import request from '@/utils/request'
import type { LoginRequest, LoginResponse, UserInfo } from '@/types/auth'

/** POST /api/auth/login */
export function login(data: LoginRequest): Promise<LoginResponse> {
  return request.post<unknown, LoginResponse>('/auth/login', data)
}

/** GET /api/auth/me —— 刷新页面后校验令牌是否还有效 */
export function getCurrentUser(): Promise<UserInfo> {
  return request.get<unknown, UserInfo>('/auth/me')
}
