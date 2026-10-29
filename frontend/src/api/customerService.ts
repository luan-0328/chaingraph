/** 客服工作台接口（v2.md §23-§25、§45 customer-service） */

import request from '@/utils/request'
import type {
  AdminUserInfo,
  AfterSaleInfo,
  DashboardStats,
  OrderDetail,
  OrderSummary,
  PageResult,
  RefundInfo,
  TicketDetail,
  TicketMessageInfo,
  TicketSummary,
} from '@/types'

export function getDashboard(): Promise<DashboardStats> {
  return request.get<unknown, DashboardStats>('/customer-service/dashboard')
}

export function listTickets(params: {
  limit?: number
  offset?: number
  status?: string
  only_mine?: boolean
  unassigned?: boolean
  waiting_human?: boolean
  keyword?: string
}): Promise<PageResult<TicketSummary>> {
  return request.get<unknown, PageResult<TicketSummary>>('/customer-service/tickets', { params })
}

export function getTicket(ticketId: number): Promise<TicketDetail> {
  return request.get<unknown, TicketDetail>(`/customer-service/tickets/${ticketId}`)
}

/**
 * 领取工单。
 *
 * ⚠️ 这里必须区分"不传" 与 "传 null"：
 *   后端用 payload.model_dump(exclude_unset=True) 判断，
 *     · 请求体里没有 assignee_id  → fields.get("assignee_id", current_user.id) → 自己领取
 *     · 请求体里 assignee_id=null → 命中"取消领取"分支
 *   所以不传时**不要**把 null 塞进 body，否则点"领取"会变成"取消领取"。
 */
export function assignTicket(ticketId: number, assigneeId?: number): Promise<TicketDetail> {
  const body = assigneeId === undefined ? {} : { assignee_id: assigneeId }
  return request.post<unknown, TicketDetail>(
    `/customer-service/tickets/${ticketId}/assign`,
    body,
  )
}

/** 取消领取（显式传 null 才是取消） */
export function releaseTicket(ticketId: number): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/customer-service/tickets/${ticketId}/assign`, {
    assignee_id: null,
  })
}

export function replyTicket(ticketId: number, content: string): Promise<TicketMessageInfo> {
  return request.post<unknown, TicketMessageInfo>(
    `/customer-service/tickets/${ticketId}/messages`,
    { content },
  )
}

export function resolveTicket(ticketId: number, solution: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/customer-service/tickets/${ticketId}/resolve`, {
    solution,
  })
}

export function closeTicket(ticketId: number, comment?: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/customer-service/tickets/${ticketId}/close`, {
    comment,
  })
}

// ---------------- 订单（客服视角） ----------------
export function listOrders(params: {
  limit?: number
  offset?: number
  keyword?: string
  payment_status?: string
  fulfillment_status?: string
}): Promise<PageResult<OrderSummary>> {
  return request.get<unknown, PageResult<OrderSummary>>('/customer-service/orders', { params })
}

export function getOrder(orderId: number): Promise<OrderDetail> {
  return request.get<unknown, OrderDetail>(`/customer-service/orders/${orderId}`)
}

export function shipOrder(orderId: number): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/customer-service/orders/${orderId}/ship`)
}

export function transitOrder(orderId: number, message?: string): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/customer-service/orders/${orderId}/transit`, null, {
    params: { message },
  })
}

export function deliverOrder(orderId: number): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/customer-service/orders/${orderId}/deliver`)
}

export interface RefundApprovalBody {
  after_sale_id: number
  expected_run_id: number | null
  review_round: number | null
  order_snapshot: Record<string, unknown>
  comment?: string
}

export function approveRefund(orderId: number, payload: RefundApprovalBody): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/customer-service/orders/${orderId}/refund/approve`, payload)
}

export function rejectRefund(orderId: number, payload: RefundApprovalBody): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/customer-service/orders/${orderId}/refund/reject`, payload)
}

// ---------------- 售后 / 退款 / 用户 ----------------
export function listAfterSales(params: {
  limit?: number
  offset?: number
  status?: string
  order_id?: number
}): Promise<PageResult<AfterSaleInfo>> {
  return request.get<unknown, PageResult<AfterSaleInfo>>('/customer-service/after-sales', { params })
}

export function getAfterSale(afterSaleId: number): Promise<AfterSaleInfo> {
  return request.get<unknown, AfterSaleInfo>(`/customer-service/after-sales/${afterSaleId}`)
}

export function listRefunds(params: {
  limit?: number
  offset?: number
  status?: string
}): Promise<PageResult<RefundInfo>> {
  return request.get<unknown, PageResult<RefundInfo>>('/customer-service/refunds', { params })
}

export function listUsers(params: {
  limit?: number
  offset?: number
  keyword?: string
  role?: string
}): Promise<PageResult<AdminUserInfo>> {
  return request.get<unknown, PageResult<AdminUserInfo>>('/customer-service/users', { params })
}

// ---------------- HITL 审批（工单维度） ----------------
export function approveTicket(ticketId: number, expectedRunId: number, reviewRound: number, comment?: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/approve`, { comment, expected_run_id: expectedRunId, review_round: reviewRound })
}

export function rejectTicket(ticketId: number, expectedRunId: number, reviewRound: number, reason?: string): Promise<TicketDetail> {
  return request.post<unknown, TicketDetail>(`/tickets/${ticketId}/reject`, { reason, expected_run_id: expectedRunId, review_round: reviewRound })
}
