/**
 * 鉴权相关的类型定义。
 *
 * ⚠️ 这些类型必须与后端 app/schemas/auth.py **逐字段对应**：
 *    后端改字段名 → 这里同步改，否则前端拿到的就是 undefined。
 */

export interface LoginRequest {
  username: string
  password: string
}

export interface UserInfo {
  user_id: number
  username: string
  email?: string | null
  phone?: string | null
}

export interface LoginResponse {
  access_token: string
  token_type: string
  /** 有效期（秒） */
  expires_in: number
  user: UserInfo
}
