/**
 * 格式化工具。
 *
 * 关于时间：后端返回的是带 Z 的 ISO 8601（UTC），例如 2026-09-24T14:15:38Z。
 * 浏览器 `new Date(...)` 会自动转成本地时区，所以这里直接 toLocaleString 即可。
 */

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

/** 只显示时分秒（时间线里用） */
export function formatTime(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 金额展示：1299 → ¥1299.00 */
export function formatAmount(value: number): string {
  return `¥${value.toFixed(2)}`
}

/** 列表里标题太长时截断 */
export function truncate(text: string, length = 40): string {
  return text.length > length ? `${text.slice(0, length)}…` : text
}
