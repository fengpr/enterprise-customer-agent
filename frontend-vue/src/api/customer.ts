import { agentHttp } from './http'
import type { AgentReply, AgentStatus, ChatSession, ChatSessionDetail, CustomerOrder, RouteTarget, Ticket } from '@/types/api'

export const customerApi = {
  agentStatus() {
    return agentHttp.get<AgentStatus>('/agent/status')
  },
  sessions(limit = 50) {
    return agentHttp.get<ChatSession[]>('/chat/session/list', { params: { limit } })
  },
  sessionDetail(sessionId: string) {
    return agentHttp.get<ChatSessionDetail>(`/chat/session/${sessionId}`)
  },
  createSession(title = '新会话') {
    return agentHttp.post<ChatSession>('/chat/session', { title })
  },
  deleteSession(sessionId: string) {
    return agentHttp.delete<{ status: string; session_id: string }>(`/chat/session/${sessionId}`)
  },
  orders() {
    return agentHttp.get<CustomerOrder[]>('/customer/orders')
  },
  tickets() {
    return agentHttp.get<Ticket[]>('/customer/tickets')
  },
  reply(
    message: string,
    sessionId?: string | null,
    selectedOrderNo?: string | null,
    selectedTicketNo?: string | null,
    routeTarget: RouteTarget = 'ai'
  ) {
    return agentHttp.post<AgentReply>('/agent/reply', {
      message,
      session_id: sessionId || null,
      selected_order_no: selectedOrderNo || null,
      selected_ticket_no: selectedTicketNo || null,
      route_target: routeTarget
    })
  },
  ticket(ticketNo: string) {
    return agentHttp.get<Ticket>(`/customer/tickets/${ticketNo}`)
  },
  urgeTicket(ticketNo: string, reason: string) {
    return agentHttp.post<Ticket>(`/customer/tickets/${ticketNo}/urge`, { reason })
  }
}
