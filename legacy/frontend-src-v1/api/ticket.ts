/**
 * 工单接口 + SSE。
 *
 * 路径全部以真实后端为准（app/api/tickets.py）：
 *   POST /api/tickets
 *   GET  /api/tickets
 *   GET  /api/tickets/{id}
 *   POST /api/tickets/{id}/process
 *   POST /api/tickets/{id}/approve
 *   POST /api/tickets/{id}/reject
 *   GET  /api/tickets/{id}/stream     ← SSE
 */

import request from '@/utils/request'
import { getToken } from '@/utils/storage'
import type { AgentProgressEvent } from '@/types/agent'
import type {
  TicketCreateRequest,
  TicketCreateResponse,
  TicketDetail,
  TicketListQuery,
  TicketListResponse,
  TicketProcessResponse,
} from '@/types/ticket'

/** POST /api/tickets —— 注意请求体里没有 user_id，后端从 JWT 取 */
export function createTicket(data: TicketCreateRequest): Promise<TicketCreateResponse> {
  return request.post<unknown, TicketCreateResponse>('/tickets', data)
}

/** GET /api/tickets —— 搜索 / 筛选 / 分页 */
export function listTickets(params: TicketListQuery): Promise<TicketListResponse> {
  return request.get<unknown, TicketListResponse>('/tickets', {
    params: {
      limit: params.limit,
      offset: params.offset,
      // 空字符串不发给后端（后端把空当成"不过滤"更符合直觉，但显式一点更好）
      status: params.status || undefined,
      type: params.type || undefined,
      keyword: params.keyword || undefined,
      mine: params.mine || undefined,
    },
  })
}

/** GET /api/tickets/{id} */
export function getTicket(ticketId: number): Promise<TicketDetail> {
  return request.get<unknown, TicketDetail>(`/tickets/${ticketId}`)
}

/** POST /api/tickets/{id}/process —— 同步等待 Agent 处理完（实测 4~8 秒） */
export function processTicket(ticketId: number, content?: string): Promise<TicketProcessResponse> {
  return request.post<unknown, TicketProcessResponse>(`/tickets/${ticketId}/process`, {
    content: content || null,
  })
}

/** POST /api/tickets/{id}/approve —— 人工确认处理 */
export function approveTicket(ticketId: number, comment?: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/approve`, {
    comment: comment || null,
  })
}

/** POST /api/tickets/{id}/reject —— 人工驳回 */
export function rejectTicket(ticketId: number, comment?: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/reject`, {
    comment: comment || null,
  })
}

// ======================================================================
// SSE：Agent 处理进度
// ======================================================================
export interface TicketStreamHandlers {
  onEvent: (event: AgentProgressEvent) => void
  onError?: (error: Error) => void
  /** 服务端正常结束（收到 done 事件后连接关闭） */
  onClose?: () => void
}

/**
 * 订阅某张工单的 Agent 处理进度。
 *
 * 为什么用 fetch 而不是 EventSource？
 *    EventSource 不能自定义请求头，也就没法带 `Authorization: Bearer ...`，
 *    只能把令牌塞进 URL 查询参数（会进日志、不安全）。
 *    fetch + ReadableStream 可以带头、可以 abort，还能自己控制重连。
 *
 * 返回值是一个"取消订阅"函数，组件卸载时记得调用。
 */
export function subscribeTicketStream(
  ticketId: number,
  handlers: TicketStreamHandlers,
): () => void {
  const controller = new AbortController()
  const token = getToken()
  const base = import.meta.env.VITE_API_BASE_URL || '/api'

  void (async () => {
    try {
      const response = await fetch(`${base}/tickets/${ticketId}/stream`, {
        headers: {
          Accept: 'text/event-stream',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        signal: controller.signal,
      })

      if (!response.ok || !response.body) {
        throw new Error(`SSE 连接失败（HTTP ${response.status}）`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      for (;;) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // SSE 用空行分隔事件块
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? '' // 最后一段可能不完整，留到下一轮

        for (const block of blocks) {
          const event = parseSseBlock(block)
          if (event) handlers.onEvent(event)
          // 收到 done / error 后服务端会主动关连接，这里不用额外处理
        }
      }

      handlers.onClose?.()
    } catch (error) {
      // 主动取消不算错误
      if (error instanceof DOMException && error.name === 'AbortError') return
      handlers.onError?.(error as Error)
    }
  })()

  return () => controller.abort()
}

/** 解析一个 SSE 事件块（心跳注释行返回 null） */
function parseSseBlock(block: string): AgentProgressEvent | null {
  const dataLines = block
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())

  if (dataLines.length === 0) return null // 只有 "event:" 或心跳 ": ping"

  try {
    return JSON.parse(dataLines.join('\n')) as AgentProgressEvent
  } catch {
    return null
  }
}
