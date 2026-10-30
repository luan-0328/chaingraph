import { nextTick, onMounted, onUnmounted, watch, type Ref } from 'vue'
import { markTicketRead } from '@/api/tickets'
import type { TicketDetail } from '@/types'

export function useTicketReadReceipt(detail: Ref<TicketDetail | null>): void {
  const acknowledged = new Map<number, number>()
  let busy = false
  let disposed = false
  async function acknowledge(): Promise<void> {
    await nextTick()
    if (disposed || busy || document.visibilityState !== 'visible' || !detail.value) return
    const ticket = detail.value
    const last = Math.max(0, ...ticket.messages.map(message => message.message_id))
    if (last <= (acknowledged.get(ticket.ticket_id) ?? 0)) return
    busy = true
    try {
      await markTicketRead(ticket.ticket_id, last)
      acknowledged.set(ticket.ticket_id, last)
      window.dispatchEvent(new Event('ticket-read'))
    } catch { /* 下次刷新或重新聚焦后重试 */ }
    finally { busy = false }
    if (!disposed && detail.value !== ticket) void acknowledge()
  }
  watch(detail, () => { void acknowledge() })
  onMounted(() => {
    document.addEventListener('visibilitychange', acknowledge)
    window.addEventListener('focus', acknowledge)
  })
  onUnmounted(() => {
    disposed = true
    document.removeEventListener('visibilitychange', acknowledge)
    window.removeEventListener('focus', acknowledge)
  })
}
