/**
 * Axios 封装：全项目只有这里创建 axios 实例。
 *
 * 它做四件事（对应前端文档第九节、第四十四节）：
 *   1. 统一 baseURL / 超时；
 *   2. 请求拦截器自动带上 Authorization: Bearer <token>；
 *   3. 响应拦截器直接把 data 返回（业务代码不用每次 .data）；
 *   4. 统一错误处理：401 清登录态并回登录页；其它状态码给人话提示。
 *
 * 注意：**不要把后端异常堆栈展示给用户**（前端文档第四十七节）。
 */

import axios, { AxiosError, type AxiosInstance } from 'axios'
import { ElMessage } from 'element-plus'

import { clearAuthStorage, getToken } from './storage'

/** 后端返回的错误体：FastAPI 用 detail 字段 */
interface ApiErrorBody {
  detail?: string | { msg?: string }[]
}

const request: AxiosInstance = axios.create({
  // 开发环境走 Vite 代理（见 vite.config.ts），所以这里用相对路径 /api
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  // Agent 处理要调 LLM（实测 4~8 秒），超时给足
  timeout: 120_000,
  headers: { 'Content-Type': 'application/json' },
})

// ----------------------------------------------------------------------
// 请求拦截器：带上令牌
// ----------------------------------------------------------------------
request.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ----------------------------------------------------------------------
// 响应拦截器：成功直接返回 data；失败统一提示
// ----------------------------------------------------------------------
request.interceptors.response.use(
  (response) => response.data,
  (error: AxiosError<ApiErrorBody>) => {
    const status = error.response?.status
    const backendMessage = extractBackendMessage(error.response?.data)

    if (status === 401) {
      // 令牌失效：清掉登录态，回登录页（前端文档第四十四节）
      clearAuthStorage()
      ElMessage.error(backendMessage || '登录已失效，请重新登录')
      void redirectToLogin()
      return Promise.reject(error)
    }

    ElMessage.error(backendMessage || messageByStatus(status))
    // 开发环境把原始错误留在 Console，方便排查（生产不展示给用户）
    if (import.meta.env.DEV) {
      console.error('[request error]', error.config?.method, error.config?.url, error)
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

/** 优先用后端给的 detail（后端已经是人话中文），拿不到再用状态码兜底 */
function extractBackendMessage(body?: ApiErrorBody): string | null {
  if (!body?.detail) return null
  if (typeof body.detail === 'string') return body.detail
  const first = body.detail[0]
  return first?.msg ?? null
}

/** 动态导入 router，避免 request ↔ router ↔ views 的循环依赖 */
async function redirectToLogin(): Promise<void> {
  const { default: router } = await import('@/router')
  if (router.currentRoute.value.name !== 'login') {
    await router.replace({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
  }
}

export default request
