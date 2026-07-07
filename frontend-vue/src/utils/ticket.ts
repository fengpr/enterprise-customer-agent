import type { Ticket, TicketResult } from '@/types/api'

export function ticketFromResult(ticketResult?: TicketResult): Ticket | null {
  if (!ticketResult || ticketResult.status !== 'success' || !ticketResult.data) {
    return null
  }
  return ticketResult.data
}

export function statusType(status?: string) {
  if (status === 'CLOSED') return 'success'
  if (status === 'PROCESSING') return 'warning'
  if (status === 'PENDING_ASSIGN' || status === 'PENDING_PROCESS') return 'info'
  if (status === 'TRANSFERRED' || status === 'REOPENED') return 'danger'
  return 'info'
}
