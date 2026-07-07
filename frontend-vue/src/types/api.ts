export interface CurrentUser {
  user_id: number
  customer_id: number
  display_name: string
  role: 'customer' | 'staff' | 'dispatcher' | string
}

export interface LoginResponse {
  token: string
  token_type: string
  user: CurrentUser
}

export interface AgentStatus {
  status: string
  error?: string
  llm?: {
    enabled?: boolean
    provider?: string | null
    model?: string | null
    base_url?: string | null
    timeout?: number
  }
}

export interface ChatSession {
  id: number
  session_id: string
  customer_id: number
  status: string
  title: string | null
  intent: string | null
  emotion: string | null
  priority: string | null
  ai_summary: string | null
  human_requested_at?: string | null
  human_assigned_staff_id?: string | null
  human_assigned_staff_name?: string | null
  human_accepted_at?: string | null
  human_closed_at?: string | null
  handoff_reason?: string | null
  created_at: string
  updated_at: string
  deleted_at?: string | null
}

export interface Ticket {
  id?: number
  ticketNo: string
  title?: string
  ticketType?: string
  priority?: string
  customerId?: number
  orderNo?: string | null
  sessionId?: number | null
  externalSessionNo?: string | null
  content?: string
  aiSummary?: string
  assignedGroup?: string | null
  handlerId?: number | null
  assignedBy?: 'MANUAL' | 'AUTO_RULE' | 'AI_ASSISTED' | string | null
  assignmentReason?: string | null
  assignmentScore?: number | null
  assignedAt?: string | null
  status?: string
  slaDeadline?: string | null
  source?: string
  urgeCount?: number | null
  lastUrgedAt?: string | null
  lastUrgeReason?: string | null
  createdAt?: string
  updatedAt?: string
}

export interface TicketResult {
  status: 'success' | 'failed' | string
  data?: Ticket
  error?: string
}

export interface ChatMessage {
  id: number
  session_id: string
  sender_type: 'customer' | 'ai' | 'staff' | string
  sender_id?: string | null
  content: string
  message_type: string
  extra_data: {
    ticket_result?: TicketResult
    customer_visible?: boolean
    [key: string]: unknown
  }
  created_at: string
}

export interface ChatSessionDetail {
  session: ChatSession | null
  messages: ChatMessage[]
}

export interface AgentReply {
  session_id: string
  answer: string
  customer_message?: string
  service_status?: string
  auto_send: boolean
  need_human: boolean
  ticket_result?: TicketResult
}

export type RouteTarget = 'ai' | 'human' | 'both'

export interface CustomerOrder {
  id: number
  orderNo: string
  customerId: number
  productId: number
  productName?: string | null
  productCategory?: string | null
  warrantyDays?: number | null
  returnable?: boolean | null
  orderStatus: string
  payTime?: string | null
  shipTime?: string | null
  signTime?: string | null
  amount?: number | string | null
  afterSaleStatus?: string | null
}

export interface StaffReplyDraft {
  ticket_no: string
  session_id: string
  draft_message: string
}

export interface StaffHandoffSession {
  session_id: string
  customer_id: number
  status: 'HUMAN_PENDING' | 'HUMAN_ACTIVE' | 'HUMAN_CLOSED' | 'AI_ONLY' | string
  title?: string | null
  intent?: string | null
  emotion?: string | null
  priority?: string | null
  ai_summary?: string | null
  handoff_reason?: string | null
  human_requested_at?: string | null
  human_assigned_staff_id?: string | null
  human_assigned_staff_name?: string | null
  human_accepted_at?: string | null
  updated_at?: string | null
}

export interface StaffHandoffDetail {
  session: StaffHandoffSession
  messages: ChatMessage[]
}

export interface StaffMemberStatus {
  userId: number
  displayName: string
  role: 'staff' | 'dispatcher' | string
  groupName: string
  skills: string[]
  online: boolean
  maxActiveTickets: number
  acceptingTickets: boolean
  activeTickets: number
  currentWork: string[]
  recentFeedback: string[]
}

export interface StaffMemberAvailabilityRequest {
  online: boolean
  acceptingTickets: boolean
  maxActiveTickets: number
}
