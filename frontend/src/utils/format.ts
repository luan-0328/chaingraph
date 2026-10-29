/** 格式化工具：时间 / 金额 / 截断 */

export function formatDateTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value

  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

export function formatTime(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatAmount(value?: number | null): string {
  if (value === null || value === undefined) return '—'
  return `¥${Number(value).toFixed(2)}`
}

export function truncate(text: string, length = 40): string {
  if (!text) return ''
  return text.length > length ? `${text.slice(0, length)}…` : text
}

/** 审批时展示影响退款资格的业务状态，不向客服暴露内部字段名。 */
export function formatRefundReview(snapshot: Record<string, unknown>): string {
  const labels: Record<string, string> = {
    UNPAID: '未支付', PAID: '已支付', REFUNDED: '已退款',
    PENDING: '待发货', PROCESSING: '处理中', SHIPPED: '已发货',
    DELIVERED: '已送达', COMPLETED: '已完成', CANCELLED: '已取消',
    APPLYING: '申请中', APPROVED: '已批准', REJECTED: '已驳回',
    REFUND: '退款', RETURN: '退货退款', EXCHANGE: '换货',
  }
  const label = (value: unknown) => labels[String(value)] || String(value ?? '未知')
  const sale = snapshot.after_sale as Record<string, unknown> | null
  const parts = [
    `支付：${label(snapshot.payment_status)}`,
    `履约：${label(snapshot.fulfillment_status)}`,
    `订单金额：${formatAmount(Number(snapshot.total_amount))}`,
  ]
  if (sale) parts.push(`售后：${label(sale.type)} / ${label(sale.status)}；申请原因：${sale.reason || '未填写'}`)
  return parts.join('；')
}
