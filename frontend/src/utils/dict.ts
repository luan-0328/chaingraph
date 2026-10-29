/**
 * 字典：后端英文枚举 → 中文文案 + 标签色（v2.md §10/§15/§19/§42）。
 * 枚举值必须与后端一致：订单三个状态维度分开、售后/工单各自一套。
 */

import type {
  AfterSaleStatus,
  AfterSaleType,
  FulfillmentStatus,
  OrderAfterSaleStatus,
  PaymentStatus,
  SenderType,
  TicketPriority,
  TicketStatus,
  TicketType,
} from '@/types'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

interface Meta {
  label: string
  tag: TagType
}

export const PAYMENT_STATUS_MAP: Record<PaymentStatus, Meta> = {
  UNPAID: { label: '待支付', tag: 'warning' },
  PAID: { label: '已支付', tag: 'success' },
  REFUNDED: { label: '已退款', tag: 'info' },
}

export const FULFILLMENT_STATUS_MAP: Record<FulfillmentStatus, Meta> = {
  PENDING: { label: '待发货', tag: 'warning' },
  PROCESSING: { label: '备货中', tag: 'primary' },
  SHIPPED: { label: '已发货', tag: 'primary' },
  DELIVERED: { label: '已签收', tag: 'success' },
  COMPLETED: { label: '已完成', tag: 'success' },
  CANCELLED: { label: '已取消', tag: 'info' },
}

export const ORDER_AFTER_SALE_STATUS_MAP: Record<OrderAfterSaleStatus, Meta> = {
  NONE: { label: '无售后', tag: 'info' },
  APPLYING: { label: '售后申请中', tag: 'warning' },
  REFUNDING: { label: '退款中', tag: 'warning' },
  REFUNDED: { label: '已退款', tag: 'success' },
}

export const AFTER_SALE_TYPE_MAP: Record<AfterSaleType, Meta> = {
  REFUND: { label: '退款', tag: 'danger' },
  RETURN: { label: '退货退款', tag: 'warning' },
  EXCHANGE: { label: '换货', tag: 'primary' },
  OTHER: { label: '其他', tag: 'info' },
}

export const AFTER_SALE_STATUS_MAP: Record<AfterSaleStatus, Meta> = {
  APPLYING: { label: '申请中', tag: 'warning' },
  PROCESSING: { label: '处理中', tag: 'primary' },
  APPROVED: { label: '已通过', tag: 'primary' },
  REJECTED: { label: '已拒绝', tag: 'danger' },
  COMPLETED: { label: '已完成', tag: 'success' },
  CANCELLED: { label: '已撤销', tag: 'info' },
}

export const REFUND_STATUS_MAP: Record<string, Meta> = {
  PENDING: { label: '处理中', tag: 'warning' },
  SUCCESS: { label: '退款成功', tag: 'success' },
  FAILED: { label: '退款失败', tag: 'danger' },
}

export const TICKET_STATUS_MAP: Record<TicketStatus, Meta> = {
  OPEN: { label: '待处理', tag: 'info' },
  PROCESSING: { label: '处理中', tag: 'primary' },
  WAITING_HUMAN: { label: '待人工确认', tag: 'warning' },
  RESOLVED: { label: '已解决', tag: 'success' },
  CLOSED: { label: '已关闭', tag: 'success' },
  FAILED: { label: '处理失败', tag: 'danger' },
}

export const TICKET_TYPE_MAP: Record<TicketType, Meta> = {
  ORDER: { label: '订单问题', tag: 'info' },
  LOGISTICS: { label: '物流问题', tag: 'warning' },
  REFUND: { label: '退款问题', tag: 'danger' },
  AFTER_SALE: { label: '售后问题', tag: 'primary' },
  OTHER: { label: '其他', tag: 'info' },
}

export const TICKET_PRIORITY_MAP: Record<TicketPriority, Meta> = {
  LOW: { label: '低', tag: 'info' },
  NORMAL: { label: '普通', tag: 'primary' },
  HIGH: { label: '高', tag: 'warning' },
  URGENT: { label: '紧急', tag: 'danger' },
}

export const SENDER_LABEL: Record<SenderType, string> = {
  USER: '用户',
  AGENT: 'Agent',
  CUSTOMER_SERVICE: '客服',
  SYSTEM: '系统',
}

/** Agent 业务事件 → 中文文案（v2.md §43） */
export const AGENT_EVENT_MAP: Record<string, Meta> = {
  STARTED: { label: '开始处理', tag: 'info' },
  ANALYZING: { label: '分析问题', tag: 'primary' },
  DECIDED: { label: '完成决策', tag: 'primary' },
  QUERY_ORDER: { label: '查询订单', tag: 'primary' },
  QUERY_LOGISTICS: { label: '查询物流', tag: 'primary' },
  QUERY_AFTER_SALE: { label: '查询售后', tag: 'primary' },
  RETRIEVING_KNOWLEDGE: { label: '检索规则', tag: 'primary' },
  TOOL_CALLING: { label: '调用工具', tag: 'primary' },
  EXECUTING_REFUND: { label: '执行退款', tag: 'warning' },
  RESULT_GENERATED: { label: '生成方案', tag: 'primary' },
  WAITING_HUMAN: { label: '等待人工', tag: 'warning' },
  HUMAN_APPROVED: { label: '人工批准', tag: 'success' },
  HUMAN_REJECTED: { label: '人工驳回', tag: 'danger' },
  COMPLETED: { label: '处理完成', tag: 'success' },
  FAILED: { label: '处理失败', tag: 'danger' },
}

export const TICKET_STATUS_OPTIONS = Object.entries(TICKET_STATUS_MAP).map(([value, m]) => ({
  value,
  label: m.label,
}))
export const TICKET_TYPE_OPTIONS = Object.entries(TICKET_TYPE_MAP).map(([value, m]) => ({
  value,
  label: m.label,
}))
export const FULFILLMENT_STATUS_OPTIONS = Object.entries(FULFILLMENT_STATUS_MAP).map(
  ([value, m]) => ({ value, label: m.label }),
)
export const AFTER_SALE_STATUS_OPTIONS = Object.entries(AFTER_SALE_STATUS_MAP).map(
  ([value, m]) => ({ value, label: m.label }),
)
export const AFTER_SALE_TYPE_OPTIONS = Object.entries(AFTER_SALE_TYPE_MAP).map(([value, m]) => ({
  value,
  label: m.label,
}))
export const PRODUCT_STATUS_OPTIONS = [
  { value: 'ON_SALE', label: '上架' },
  { value: 'OFF_SALE', label: '下架' },
]
export const USER_ROLE_OPTIONS = [
  { value: 'USER', label: '普通用户' },
  { value: 'CUSTOMER_SERVICE', label: '客服' },
  { value: 'ADMIN', label: '管理员' },
]

export function meta(map: Record<string, Meta>, key?: string | null): Meta {
  if (!key) return { label: '—', tag: 'info' }
  return map[key] ?? { label: key, tag: 'info' }
}

export const paymentMeta = (k?: string | null) => meta(PAYMENT_STATUS_MAP, k)
export const fulfillmentMeta = (k?: string | null) => meta(FULFILLMENT_STATUS_MAP, k)
export const orderAfterSaleMeta = (k?: string | null) => meta(ORDER_AFTER_SALE_STATUS_MAP, k)
export const afterSaleTypeMeta = (k?: string | null) => meta(AFTER_SALE_TYPE_MAP, k)
export const afterSaleStatusMeta = (k?: string | null) => meta(AFTER_SALE_STATUS_MAP, k)
export const refundStatusMeta = (k?: string | null) => meta(REFUND_STATUS_MAP, k)
export const ticketStatusMeta = (k?: string | null) => meta(TICKET_STATUS_MAP, k)
export const ticketTypeMeta = (k?: string | null) => meta(TICKET_TYPE_MAP, k)
export const ticketPriorityMeta = (k?: string | null) => meta(TICKET_PRIORITY_MAP, k)
export const agentEventMeta = (k?: string | null) => meta(AGENT_EVENT_MAP, k)
export const roleLabel = (k?: string | null) => meta(
  Object.fromEntries(USER_ROLE_OPTIONS.map((o) => [o.value, { label: o.label, tag: 'info' as const }])),
  k,
).label
