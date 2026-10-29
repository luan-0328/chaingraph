/**
 * Axios 封装：全项目唯一创建 axios 实例的地方。
 *
 * 1. 统一 baseURL / 超时；
 * 2. 请求拦截器自动带 Authorization: Bearer <token>（SSE 见 utils/sse.ts）；
 * 3. 响应拦截器直接返回 data；
 * 4. 统一错误处理：401 清登录态回登录页；优先展示后端 message（后端已经是人话中文）。
 */

import axios, { AxiosError, type AxiosInstance } from 'axios'
import { ElMessage } from 'element-plus'

import type { ApiError } from '@/types'
import { clearAuthStorage, getToken } from './storage'

const request: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  // Agent 处理要调 LLM（实测 5~30 秒），超时给足
  timeout: 180_000,
  headers: { 'Content-Type': 'application/json' },
})

request.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

request.interceptors.response.use(
  (response) => response.data,
  (error: AxiosError<ApiError>) => {
    const status = error.response?.status
    const backendMessage = error.response?.data?.message

    if (status === 401) {
      clearAuthStorage()
      ElMessage.error(backendMessage || '登录已失效，请重新登录')
      void redirectToLogin()
      return Promise.reject(error)
    }

    ElMessage.error(backendMessage || messageByStatus(status))
    if (import.meta.env.DEV) {
      console.error('[request error]', error.config?.method, error.config?.url, error.response?.data)
    }
    return Promise.reject(error)
  },
)

function messageByStatus(status?: number): string {
  switch (status) {
    case 400:
      return '请求参数有误'
    case 403:
      return '暂无权限执行该操作'
    case 404:
      return '请求的资源不存在'
    case 409:
      return '当前状态不允许该操作'
    case 422:
      return '提交的数据不合法'
    case 500:
      return '服务器处理失败，请稍后重试'
    default:
      return '请求失败，请检查网络连接'
  }
}

/** 动态导入 router，避免 request ↔ router ↔ views 循环依赖 */
async function redirectToLogin(): Promise<void> {
  const { default: router } = await import('@/router')
  if (router.currentRoute.value.name !== 'login') {
    await router.replace({
      name: 'login',
      query: { redirect: router.currentRoute.value.fullPath },
    })
  }
}

export default request
