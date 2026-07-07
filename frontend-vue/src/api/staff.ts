import { agentHttp, businessHttp } from './http'
import type {
  StaffHandoffDetail,
  StaffHandoffSession,
  StaffMemberAvailabilityRequest,
  StaffMemberStatus,
  StaffReplyDraft,
  Ticket
} from '@/types/api'

export const staffTicketApi = {
  list(status: string) {
    return businessHttp.get<Ticket[]>('/staff/tickets', { params: { status } })
  },
  assign(ticketNo: string, handlerId: number, assignedGroup: string) {
    return businessHttp.post<Ticket>(`/staff/tickets/${ticketNo}/assign`, {
      handlerId,
      assignedGroup
    })
  },
  autoAssign(ticketNo: string) {
    return businessHttp.post<Ticket>(`/staff/tickets/${ticketNo}/auto-assign`)
  },
  start(ticketNo: string, operatorId: number) {
    return businessHttp.post<Ticket>(`/staff/tickets/${ticketNo}/status`, {
      status: 'PROCESSING',
      operatorId,
      reason: '坐席开始处理'
    })
  },
  close(ticketNo: string, operatorId: number, closeReason: string) {
    return businessHttp.post<Ticket>(`/staff/tickets/${ticketNo}/close`, {
      operatorId,
      closeReason
    })
  }
}

export const staffMemberApi = {
  list() {
    return businessHttp.get<StaffMemberStatus[]>('/staff/members')
  },
  updateAvailability(userId: number, payload: StaffMemberAvailabilityRequest) {
    return businessHttp.patch<StaffMemberStatus>(`/staff/members/${userId}/availability`, payload)
  }
}

export const staffReplyApi = {
  draft(ticketNo: string, closeReason: string) {
    return agentHttp.post<StaffReplyDraft>(`/staff/tickets/${ticketNo}/reply/draft`, {
      close_reason: closeReason
    })
  },
  send(ticketNo: string, message: string) {
    return agentHttp.post<{ status: string; session_id: string }>(`/staff/tickets/${ticketNo}/reply/send`, {
      message
    })
  }
}

export const staffHandoffApi = {
  list(limit = 50) {
    return agentHttp.get<StaffHandoffSession[]>('/staff/handoff/sessions', { params: { limit } })
  },
  detail(sessionId: string) {
    return agentHttp.get<StaffHandoffDetail>(`/staff/handoff/sessions/${sessionId}`)
  },
  accept(sessionId: string) {
    return agentHttp.post<{ status: string; session: StaffHandoffSession }>(`/staff/handoff/sessions/${sessionId}/accept`)
  },
  reply(sessionId: string, message: string) {
    return agentHttp.post<{ status: string; session_id: string }>(`/staff/handoff/sessions/${sessionId}/reply`, { message })
  },
  close(sessionId: string, message: string, status: 'HUMAN_CLOSED' | 'AI_ONLY' = 'HUMAN_CLOSED') {
    return agentHttp.post<{ status: string; session: StaffHandoffSession }>(`/staff/handoff/sessions/${sessionId}/close`, {
      message,
      status
    })
  }
}
