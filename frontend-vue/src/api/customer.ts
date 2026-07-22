import { agentHttp } from './http'
import type { AgentReply, AgentStatus, ChatSession, ChatSessionDetail, CustomerFollowup, CustomerNotification, CustomerOrder, CustomerOrderDetail, CustomerOrderLogistics, RouteTarget, Ticket } from '@/types/api'

export const customerApi = {
  async streamReply(
    body: Record<string, unknown>,
    onEvent: (event: { request_id?: string; event_type: string; payload: Record<string, unknown>; event_id: string }) => void,
    options: { lastEventId?: string; idempotencyKey: string; signal?: AbortSignal }
  ) {
    /** 使用同一幂等键重连 POST SSE，确保断线续传不会重复执行 Agent 或重复建单。 */
    const token = localStorage.getItem('eca_token') || ''
    const response = await fetch('/api/agent/reply/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: token ? `Bearer ${token}` : '',
        'Idempotency-Key': options.idempotencyKey,
        ...(options.lastEventId ? { 'Last-Event-ID': options.lastEventId } : {})
      },
      body: JSON.stringify(body),
      signal: options.signal
    })
    if (!response.ok || !response.body) throw new Error('流式回复连接失败')
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let requestId = ''
    let terminalType = ''
    let lastEventId = options.lastEventId || ''
    while (true) {
      const part = await reader.read()
      if (part.done) break
      buffer += decoder.decode(part.value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() || ''
      frames.forEach((frame) => {
        const data = frame.split('\n').find((line) => line.startsWith('data: '))?.slice(6)
        if (data) {
          const event = JSON.parse(data) as { request_id?: string; event_type: string; payload: Record<string, unknown>; event_id: string }
          requestId = event.request_id || requestId
          lastEventId = event.event_id || lastEventId
          if (['completed', 'degraded', 'cancelled', 'error'].includes(event.event_type)) terminalType = event.event_type
          onEvent(event)
        }
      })
    }
    return { requestId, terminalType, lastEventId }
  },
  agentStatus() {
    return agentHttp.get<AgentStatus>('/agent/status')
  },
  sessions(limit = 50) {
    return agentHttp.get<ChatSession[]>('/chat/session/list', { params: { limit } })
  },
  sessionDetail(sessionId: string, afterMessageId = 0) {
    return agentHttp.get<ChatSessionDetail>(`/chat/session/${sessionId}`, { params: { after_message_id: afterMessageId } })
  },
  createSession(title = '新会话') {
    return agentHttp.post<ChatSession>('/chat/session', { title })
  },
  deleteSession(sessionId: string) {
    return agentHttp.delete<{ status: string; session_id: string }>(`/chat/session/${sessionId}`)
  },
  setSessionPinned(sessionId: string, pinned: boolean) {
    return agentHttp.patch<ChatSession>(`/chat/session/${sessionId}/pin`, { pinned })
  },
  cancelHandoff(sessionId: string) {
    return agentHttp.post<{ status: string; session: ChatSession }>(`/chat/session/${sessionId}/handoff/cancel`)
  },
  orders() {
    return agentHttp.get<CustomerOrder[]>('/customer/orders')
  },
  orderDetail(orderNo: string) {
    return agentHttp.get<CustomerOrderDetail>(`/customer/orders/${encodeURIComponent(orderNo)}`)
  },
  orderLogistics(orderNo: string) {
    return agentHttp.get<CustomerOrderLogistics>(`/customer/orders/${encodeURIComponent(orderNo)}/logistics`)
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
  /** 人工消息直接落库，不创建需要 SSE 轮询的 Agent 执行任务。 */
  sendHandoffMessage(body: Record<string, unknown>) {
    return agentHttp.post<AgentReply>('/agent/handoff/messages', body)
  },
  replyResult(requestId: string) {
    return agentHttp.get<{ status: string; result?: AgentReply }>(`/agent/replies/${requestId}`)
  },
  cancelReply(requestId: string) {
    return agentHttp.post<{ request_id: string; status: string; partial_answer: string; ticket_no?: string; service_status: string }>(`/agent/replies/${requestId}/cancel`)
  },
  ticket(ticketNo: string) {
    return agentHttp.get<Ticket>(`/customer/tickets/${ticketNo}`)
  },
  urgeTicket(ticketNo: string, reason: string) {
    return agentHttp.post<Ticket>(`/customer/tickets/${ticketNo}/urge`, { reason })
  },
  notifications(limit = 50) {
    return agentHttp.get<CustomerNotification[]>('/customer/notifications', { params: { limit } })
  },
  notificationUnreadCount() {
    return agentHttp.get<{ count: number }>('/customer/notifications/unread-count')
  },
  markNotificationRead(notificationId: string) {
    return agentHttp.post<{ notification_id: string; is_read: boolean }>(`/customer/notifications/${notificationId}/read`)
  },
  followups(limit = 50) {
    return agentHttp.get<CustomerFollowup[]>('/customer/follow-ups', { params: { limit } })
  },
  cancelFollowup(followupId: string) {
    return agentHttp.post<{ followup_id: string; status: string }>(`/customer/follow-ups/${followupId}/cancel`)
  }
}
