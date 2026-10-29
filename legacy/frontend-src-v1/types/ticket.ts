/**
 * 工单相关的类型定义。
 *
 * ⚠️ 唯一依据是后端 app/schemas/ticket.py。
 *    前端**不允许**自己发明状态/类型枚举 —— 前端文档第十六节明确要求：
 *    "不允许前端自行定义与后端不一致的状态枚举"。
 */

/** 后端定义的状态枚举（app/models/ticket.py::TicketStatus） */
export type TicketStatus = 'OPEN' | 'PROCESSING' | 'WAITING_HUMAN' | 'RESOLVED' | 'CLOSED'

/** 后端定义的类型枚举（app/models/ticket.py::TicketType） */
export type TicketType = 'ORDER_QUERY' | 'LOGISTICS' | 'REFUND' | 'OTHER'

/** 退款状态（app/models/refund.py::RefundStatus） */
export type RefundStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'COMPLETED'

export interface TicketSummary {
  ticket_id: number
  user_id: number
  title: string | null
  content: string
  type: TicketType | null
  status: TicketStatus
  /** ISO 8601 UTC，例如 2026-09-24T14:15:38Z */
  created_at: string
  updated_at: string
}

export interface RefundInfo {
  refund_id: number
  order_id: number
  order_no: string | null
  amount: number
  status: RefundStatus
  reason: string | null
  /** 审批人 user_id；0 = 系统自动通过；null = 尚未审批 */
  approved_by: number | null
  created_at: string
  updated_at: string
}

export interface TicketDetail extends TicketSummary {
  priority: string | null
  agent_result: string | null
  resolution: string | null
  refunds: RefundInfo[]
}

export interface TicketListResponse {
  items: TicketSummary[]
  total: number
  limit: number
  offset: number
}

export interface TicketCreateRequest {
  content: string
  type?: TicketType | null
}

export interface TicketCreateResponse {
  ticket_id: number
  status: TicketStatus
  content: string
}

export interface TicketProcessResponse {
  ticket_id: number
  status: TicketStatus
  analysis: string
  tools_used: string[]
}

export interface TicketListQuery {
  limit?: number
  offset?: number
  status?: TicketStatus | ''
  type?: TicketType | ''
  keyword?: string
  mine?: boolean
}
