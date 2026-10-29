/**
 * 字典：把后端的英文枚举翻译成中文界面文案 + Element Plus 的标签颜色。
 *
 * 集中放一处，避免每个页面各写一份（前端文档第十七节的要求）。
 */

import type { RefundStatus, TicketStatus, TicketType } from '@/types/ticket'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/** 工单状态 → 中文 + 标签色（枚举值必须与后端一致，见 app/models/ticket.py） */
export const TICKET_STATUS_MAP: Record<TicketStatus, { label: string; tag: TagType }> = {
  OPEN: { label: '待处理', tag: 'info' },
  PROCESSING: { label: '处理中', tag: 'primary' },
  WAITING_HUMAN: { label: '待人工确认', tag: 'warning' },
  RESOLVED: { label: '已解决', tag: 'success' },
  CLOSED: { label: '已关闭', tag: 'success' },
}

/** 工单状态下拉选项 */
export const TICKET_STATUS_OPTIONS = (Object.keys(TICKET_STATUS_MAP) as TicketStatus[]).map(
  (value) => ({ value, label: TICKET_STATUS_MAP[value].label }),
)

/** 工单类型 → 中文 */
export const TICKET_TYPE_MAP: Record<TicketType, { label: string; tag: TagType }> = {
  ORDER_QUERY: { label: '订单查询', tag: 'info' },
  LOGISTICS: { label: '物流问题', tag: 'warning' },
  REFUND: { label: '退款问题', tag: 'danger' },
  OTHER: { label: '其他', tag: 'info' },
}

/** 工单类型下拉选项（创建工单时用户申报、列表筛选时使用） */
export const TICKET_TYPE_OPTIONS = (Object.keys(TICKET_TYPE_MAP) as TicketType[]).map((value) => ({
  value,
  label: TICKET_TYPE_MAP[value].label,
}))

/** 退款状态 → 中文 */
export const REFUND_STATUS_MAP: Record<RefundStatus, { label: string; tag: TagType }> = {
  PENDING: { label: '待审核', tag: 'warning' },
  APPROVED: { label: '已通过', tag: 'primary' },
  REJECTED: { label: '已驳回', tag: 'danger' },
  COMPLETED: { label: '已完成', tag: 'success' },
}

/** Agent 调用的工具 → 业务动作文案（给用户看，不是给模型看的） */
export const TOOL_LABELS: Record<string, string> = {
  get_order: '查询订单信息',
  get_user_orders: '查询用户订单',
  get_logistics: '查询物流信息',
  create_refund: '创建退款申请',
  close_ticket: '关闭工单',
}

export function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool
}

export function ticketStatusLabel(status?: TicketStatus | null): string {
  return status ? (TICKET_STATUS_MAP[status]?.label ?? status) : '—'
}

export function ticketTypeLabel(type?: TicketType | null): string {
  return type ? (TICKET_TYPE_MAP[type]?.label ?? type) : '—'
}

export function refundStatusLabel(status: RefundStatus): string {
  return REFUND_STATUS_MAP[status]?.label ?? status
}
