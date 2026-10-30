/** 工单接口 + Agent 触发 + SSE 订阅（v2.md §45 Ticket / HITL） */

import request from '@/utils/request'
import { getToken } from '@/utils/storage'
import type {
  AgentProcessResponse,
  AgentStreamEvent,
  PageResult,
  TicketDetail,
  TicketMessageInfo,
  TicketSummary,
} from '@/types'

export function createTicket(data: {
  content: string
  order_id?: number | null
  after_sale_id?: number | null
  type?: string | null
  title?: string | null
}): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>('/tickets', data)
}

export function listMyTickets(params: {
  limit?: number
  offset?: number
  status?: string
}): Promise<PageResult<TicketSummary>> {
  return request.get<unknown, PageResult<TicketSummary>>('/tickets', { params })
}

export function getTicket(ticketId: number): Promise<TicketDetail> {
  return request.get<unknown, TicketDetail>(`/tickets/${ticketId}`)
}

export function sendMessage(ticketId: number, content: string): Promise<TicketMessageInfo> {
  return request.post<unknown, TicketMessageInfo>(`/tickets/${ticketId}/messages`, { content })
}

export function reopenTicket(ticketId: number): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/reopen`)
}

export function closeTicket(ticketId: number, comment?: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/close`, { comment })
}

/** 提交 Agent 任务（立即返回；进度用 subscribeTicketStream 查看） */
export function processTicket(ticketId: number): Promise<AgentProcessResponse> {
  return request.post<unknown, AgentProcessResponse>(`/tickets/${ticketId}/process`)
}

export function requestHuman(ticketId: number): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/request-human`, {})
}

export function getUnreadSummary(): Promise<{ unread_messages: number; unread_tickets: number }> {
  return request.get('/tickets/unread-summary')
}

export function markTicketRead(ticketId: number, lastMessageId: number): Promise<{ last_message_id: number; unread_count: number }> {
  return request.post(`/tickets/${ticketId}/read`, { last_message_id: lastMessageId })
}

export function uploadTicketImage(ticketId: number, file: File): Promise<TicketMessageInfo> {
  const data = new FormData()
  data.append('file', file)
  return request.post(`/tickets/${ticketId}/attachments`, data, { headers: { 'Content-Type': 'multipart/form-data' } })
}

export function getTicketImageUrl(ticketId: number, attachmentId: number): Promise<{ url: string; expires_in: number }> {
  return request.get(`/tickets/${ticketId}/attachments/${attachmentId}/url`)
}

// ======================================================================
// SSE：Agent 业务进度
// ======================================================================
export interface TicketStreamHandlers {
  onEvent: (event: AgentStreamEvent) => void
  onError?: (error: Error) => void
  onClose?: () => void
}

export interface TicketStreamOptions {
  /**
   * 断线重连用：上次收到的最后一个事件序号（SSE 的 `id:`）。
   *
   * 带上它，后端只回放"你还没收到过"的那一段 ——
   * Agent 在断线期间走的步骤不会丢，也不会和已渲染的重复。
   * 首次连接不传（后端会回放全部历史）。
   */
  lastEventId?: number | null
}

/**
 * 订阅工单的 Agent 处理进度。
 *
 * 为什么用 fetch 而不是 EventSource？
 *   EventSource 不能自定义请求头，没法带 Authorization（只能把 token 塞 URL，会进日志）。
 *   fetch + ReadableStream 能带鉴权头、能 abort、能自己控制重连。
 *
 * 后端建流时会**回放历史事件**，所以晚连上/断线重连都不会漏进度；
 * 每条事件都带 `id:`，重连时把最后一次的 id 通过 `lastEventId` 传回来即可续上。
 * 返回"取消订阅"函数，组件卸载时调用。
 */
export function subscribeTicketStream(
  ticketId: number,
  handlers: TicketStreamHandlers,
  options: TicketStreamOptions = {},
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
          // 断线重连：告诉后端"我已经收到 seq=N 之前的所有事件"
          ...(options.lastEventId ? { 'Last-Event-ID': String(options.lastEventId) } : {}),
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
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''

        for (const block of blocks) {
          const event = parseSseBlock(block)
          if (event) handlers.onEvent(event)
        }
      }

      handlers.onClose?.()
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      handlers.onError?.(error as Error)
    }
  })()

  return () => controller.abort()
}

/**
 * 记下事件的序号，供重连时续传。
 *
 * 优先用 `id:` 行（那是 SSE 协议里的重连游标），没有才退回 data 里的 `seq`。
 * 组件只需要在 `onEvent` 里调一次，然后把结果存下来传给下一次订阅。
 */
export function trackEventId(event: AgentStreamEvent, previous?: number | null): number | null {
  const fromData = typeof event.seq === 'number' ? event.seq : null
  if (fromData === null) return previous ?? null
  if (previous && fromData <= previous) return previous
  return fromData
}

function parseSseBlock(block: string): AgentStreamEvent | null {
  const lines = block.split('\n')
  const dataLines = lines
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())

  if (dataLines.length === 0) return null // 心跳注释行

  try {
    const event = JSON.parse(dataLines.join('\n')) as AgentStreamEvent
    // `id:` 行是权威的重连游标；data 里的 seq 是同一份数据的冗余副本
    const idLine = lines.find((line) => line.startsWith('id:'))
    const id = idLine ? Number.parseInt(idLine.slice(3).trim(), 10) : Number.NaN
    if (Number.isFinite(id) && typeof event.seq !== 'number') event.seq = id
    return event
  } catch {
    return null
  }
}
