/** 鉴权接口（v2.md §45 Auth） */

import request from '@/utils/request'
import type { LoginRequest, LoginResponse, RegisterRequest, UserInfo } from '@/types'

export function login(data: LoginRequest): Promise<LoginResponse> {
  return request.post<unknown, LoginResponse>('/auth/login', data)
}

export function register(data: RegisterRequest): Promise<UserInfo> {
  return request.post<unknown, UserInfo>('/auth/register', data)
}

export function getCurrentUser(): Promise<UserInfo> {
  return request.get<unknown, UserInfo>('/auth/me')
}

export function updateProfile(data: {
  nickname?: string
  email?: string
  phone?: string
}): Promise<UserInfo> {
  return request.patch<unknown, UserInfo>('/auth/me', data)
}

export function changePassword(data: {
  old_password: string
  new_password: string
}): Promise<void> {
  return request.post<unknown, void>('/auth/me/password', data)
}
