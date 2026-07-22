export interface CurrentUser {
  user_id: number
  customer_id: number
  display_name: string
  role: 'customer' | 'staff' | string
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
  queue?: {
    enabled?: boolean
    active_worker?: boolean
  }
}

export interface ChatSession {
  id: number
  session_id: string
  customer_id: number
  status: string
  handoff_status: 'NONE' | 'PENDING' | 'ACTIVE' | 'CLOSED' | string
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
  pinned_at?: string | null
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
  returnMethod?: string | null
  pickupTimeWindow?: string | null
  pickupStatus?: string | null
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
  latest_message_id: number
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
  quantity: number
  warrantyDays?: number | null
  returnable?: boolean | null
  orderStatus: string
  payTime?: string | null
  shipTime?: string | null
  signTime?: string | null
  amount?: number | string | null
  afterSaleStatus?: string | null
}

/** 订单详情页使用的客户安全字段，手机号由业务服务脱敏后返回。 */
export interface CustomerOrderDetail extends CustomerOrder {
  receiverName?: string | null
  receiverPhoneMasked?: string | null
  shippingAddress?: string | null
  paymentMethod?: string | null
  deliveryMethod?: string | null
  freightAmount?: number | string | null
}

export interface LogisticsTrace {
  status: string
  description: string
  location?: string | null
  stationName?: string | null
  occurredAt?: string | null
}

export interface CustomerOrderLogistics {
  orderNo: string
  carrierName?: string | null
  trackingNo?: string | null
  logisticsStatus?: string | null
  latestLocation?: string | null
  estimatedDeliveryTime?: string | null
  routeSummary?: string | null
  traces: LogisticsTrace[]
}

export interface StaffReplyDraft {
  ticket_no: string
  session_id: string
  draft_message: string
  /** 草稿生成来源，仅供坐席判断是否需要重点润色。 */
  generation_mode?: 'llm' | 'fallback'
}

export interface RagCitationValidation {
  groundedness: number
  citation_precision: number
  citation_recall: number
  hallucination_detected: boolean
  unsupported_claims: Array<{ claim: string; reason: string }>
}

export interface RagEvaluationRow {
  sample: { query: string; expected_doc: string; business_scope: string }
  failures: string[]
  generated_answer: string
  citation_validation: RagCitationValidation
  missing_required_facts: string[]
}

export interface RagEvaluationReport {
  evaluation_mode?: 'baseline' | 'agent' | string
  metrics: Record<string, number | null>
  failures: RagEvaluationRow[]
}

export interface RagEvaluationJob {
  job_id: string
  status: 'PENDING' | 'PROCESSING' | 'SUCCEEDED' | 'FAILED'
  created_at: string
  started_at: string | null
  finished_at: string | null
  report: RagEvaluationReport | null
  error: string | null
  payload?: { max_samples?: number | null }
}

export interface OnlineEvaluationReport {
  queue: { counts: Record<string, number>; daily_budget: number; budget_used: number }
  metrics: Record<string, number | null>
  items: Array<{ trace_id: string; status: string; sampling_reason: string; created_at: string; result: { failures?: string[]; reasons?: Record<string, string> } }>
  failures: Array<{ trace_id: string; status: string; sampling_reason: string; created_at: string; result: { failures?: string[]; reasons?: Record<string, string> } }>
}

export interface SystemMonitorCacheMetric {
  hit: number
  miss: number
  error: number
  hit_rate: number | null
}

export interface SystemMonitorSnapshot {
  agent_status: {
    status: string
    llm?: AgentStatus['llm']
  }
  queue: {
    available: boolean
    enabled: boolean
    stream_depth: number | null
    pending: number | null
    running: number | null
    retrying: number | null
    error?: string | null
  }
  worker: {
    active: boolean
  }
  dlq: {
    count: number | null
  }
  llm: {
    timeout: number | null
    rate_limit_429: number | null
    circuit_open: number | null
  }
  cache: Record<string, SystemMonitorCacheMetric>
  degraded: {
    total: number | null
    by_reason: Record<string, number>
  }
  updated_at: string
}

export interface StaffHandoffSession {
  session_id: string
  customer_id: number
  status: string
  session_status: string
  handoff_status: 'PENDING' | 'ACTIVE' | 'CLOSED' | string
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
  human_closed_at?: string | null
  waiting_seconds?: number
  linked_ticket_no?: string | null
  latest_message_id?: number
  updated_at?: string | null
}

export interface StaffHandoffDetail {
  session: StaffHandoffSession
  messages: ChatMessage[]
  latest_message_id: number
  handoff_summary?: {
    title: string
    intent?: string | null
    priority?: string | null
    handoff_reason?: string | null
    ai_summary: string
    linked_ticket_no?: string | null
  }
  history_available?: boolean
  history_access_allowed?: boolean
}

export interface StaffHandoffHistoryPage {
  messages: ChatMessage[]
  history_available: boolean
  next_before_message_id?: number | null
}

export interface StaffMemberStatus {
  userId: number
  displayName: string
  role: 'staff' | string
  groupName: string
  skills: string[]
  online: boolean
  maxActiveTickets: number
  acceptingTickets: boolean
  activeTickets: number
  currentWork: string[]
  recentFeedback: string[]
}

export interface CustomerNotification {
  notification_id: string
  session_id: string
  followup_id?: string | null
  type: string
  title: string
  content: string
  is_read: boolean
  created_at: string
  read_at?: string | null
}

export interface CustomerFollowup {
  followup_id: string
  session_id: string
  task_type: string
  order_no: string
  scheduled_at: string
  status: 'PENDING' | 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED' | string
  result?: Record<string, unknown> | null
  created_at: string
  updated_at: string
}
