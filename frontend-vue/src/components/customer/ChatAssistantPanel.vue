<script setup lang="ts">
import { ArrowDown, ArrowUp, Connection, DocumentCopy, Headset, Link, Message, Position, Refresh, Service, Tickets, VideoPause } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import { computed, nextTick, onActivated, ref, watch } from 'vue'
import type { AgentReply, ChatMessage, ChatSession, CustomerOrder, RouteTarget, Ticket } from '@/types/api'

const props = defineProps<{
  messages: ChatMessage[]
  lastReply: AgentReply | null
  modelValue: string
  routeTarget: RouteTarget
  session: ChatSession | null
  submitting?: boolean
  selectedOrder: CustomerOrder | null
  selectedTicket: Ticket | null
  /** 当前输入框的受控业务语境，仅用于引导客户补充信息。 */
  composerHint?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:routeTarget': [value: RouteTarget]
  'continue-ai': []
  'cancel-handoff': []
  // 提交时携带当前页签快照，避免依赖异步 watch 导致消息被投递到旧通道。
  submit: [target: RouteTarget]
  cancel: []
  quick: [action: string]
  regenerate: [message: string]
}>()

const chatWindowRef = ref<HTMLElement | null>(null)
const isComposing = ref(false)
const activeTab = ref<RouteTarget>('ai')
// 人工接入信息默认展开；客户可收起大面积上下文卡片，避免遮挡会话内容。
const handoffDetailsCollapsed = ref(false)
const inputPlaceholder = computed(() =>
  activeTab.value === 'human'
    ? '请输入要补充给人工客服的信息...'
    : props.composerHint
      ? '请说明退货原因；可补充上门取件或方便取件的时间'
    : '请输入您的问题...'
)
const hasHumanSession = computed(() => ['PENDING', 'ACTIVE', 'CLOSED'].includes(String(props.session?.handoff_status || '')))
const handoffStatusText = computed(() => ({ PENDING: '待接入', ACTIVE: '已接入', CLOSED: '已结束' }[String(props.session?.handoff_status)] || ''))
const handoffStatusType = computed(() => props.session?.handoff_status === 'ACTIVE' ? 'success' : props.session?.handoff_status === 'CLOSED' ? 'info' : 'warning')
const aiMessages = computed(() =>
  props.messages.filter((message) => {
    if (message.sender_type === 'ai') return true
    if (message.sender_type === 'customer') return message.extra_data?.route_target !== 'human'
    return false
  })
)
const humanMessages = computed(() =>
  props.messages.filter((message) => {
    if (message.sender_type === 'staff') return true
    if (message.sender_type === 'system') return String(message.extra_data?.message_source || '').startsWith('handoff_')
    if (message.sender_type === 'customer') return message.extra_data?.route_target === 'human' || message.extra_data?.route_target === 'both'
    return false
  })
)
const visibleMessages = computed(() => (activeTab.value === 'human' ? humanMessages.value : aiMessages.value))
const visibleMessageEntries = computed(() =>
  visibleMessages.value.map((message, index, list) => ({
    message,
    timeDivider: shouldShowTimeDivider(message, list[index - 1]) ? formatTimeDivider(message) : ''
  }))
)

/**
 * 统一切换展示页签和真实投递目标。
 * 人工页签不可用时不允许切换，防止尚未建立人工会话的消息被错误标成人工消息。
 */
function switchConversationChannel(target: RouteTarget | string) {
  if (target !== 'ai' && target !== 'human') return
  if (target === 'human' && !hasHumanSession.value) return
  activeTab.value = target
  // 同步通知父组件，不能仅依赖 watch 在下一个渲染周期更新。
  emit('update:routeTarget', target)
}

/** 短客户消息按内容宽度展示，避免中文按单字换行或产生固定宽度空白。 */
function isShortCustomerMessage(message: ChatMessage) {
  const content = String(message.content || '').trim()
  return message.sender_type === 'customer' && !/[\r\n]/.test(content) && Array.from(content).length <= 10
}

const markdown = new MarkdownIt({
  breaks: true,
  html: false,
  linkify: true,
  typographer: false
})

function normalizeAssistantMarkdown(content: string) {
  // 客户侧不展示裸知识库 citation_id；后端仍保留结构化 citations 供评测、Trace 和坐席诊断使用。
  const normalized = String(content || '')
    .replace(/【(?:来源|引用)[:：]\s*kb-[^】]+】/g, '')
    .replace(/\r\n?/g, '\n')
    .trim()
  if (!normalized) return ''

  const lines = normalized.split('\n')
  const meaningfulLines = lines.filter((line) => line.trim())
  const firstLine = meaningfulLines[0]?.trim() || ''
  const hasMarkdownBlocks = lines.some((line) => /^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```)/.test(line))
  const looksLikeTitle =
    meaningfulLines.length > 1 &&
    firstLine.length <= 24 &&
    /(?:说明|规则|指南|步骤|条件|流程|方式|建议|：|:)$/.test(firstLine)

  if (!hasMarkdownBlocks && looksLikeTitle) {
    const titleIndex = lines.findIndex((line) => line.trim() === firstLine)
    lines[titleIndex] = `### ${firstLine.replace(/[：:]$/, '')}`
    return lines.join('\n')
  }

  if (!hasMarkdownBlocks && !normalized.includes('\n\n') && normalized.length >= 180) {
    // 旧回复若仍是超长纯文本，按完整句子分段，避免改变原始语义或业务顺序。
    const sentences = normalized.match(/[^。！？!?；;]+[。！？!?；;]?/g)?.map((item) => item.trim()).filter(Boolean) || []
    if (sentences.length >= 4) {
      const paragraphs: string[] = []
      for (let index = 0; index < sentences.length; index += 2) {
        paragraphs.push(sentences.slice(index, index + 2).join(''))
      }
      return paragraphs.join('\n\n')
    }
  }

  return normalized
}

function renderMessageMarkdown(content: string) {
  // Markdown 禁用原始 HTML，并再次清洗生成结果，避免聊天内容注入可执行标签或属性。
  return DOMPurify.sanitize(markdown.render(normalizeAssistantMarkdown(content)), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'iframe', 'form']
  })
}

type MessageTimeSource = ChatMessage & {
  createTime?: string | number | Date | null
  createdAt?: string | number | Date | null
  sendTime?: string | number | Date | null
  timestamp?: string | number | Date | null
}

function parseMessageDate(value: string | number | Date | null | undefined) {
  if (!value) return new Date()
  if (value instanceof Date) return value
  if (typeof value === 'number') return new Date(value)

  const normalized = value.includes('T') && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? `${value}Z` : value
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed
}

function messageDate(message?: MessageTimeSource | null) {
  const rawTime = message?.created_at ?? message?.createdAt ?? message?.createTime ?? message?.sendTime ?? message?.timestamp
  return parseMessageDate(rawTime)
}

function isSameCalendarDay(left: Date, right: Date) {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate()
}

function shouldShowTimeDivider(message: ChatMessage, previous?: ChatMessage) {
  // 第一条消息必须显示时间；跨天或距离上一条消息达到 10 分钟时显示新的时间分隔条。
  if (!previous) return true
  const currentDate = messageDate(message)
  const previousDate = messageDate(previous)
  if (!isSameCalendarDay(currentDate, previousDate)) return true
  return currentDate.getTime() - previousDate.getTime() >= 10 * 60 * 1000
}

function formatTimeDivider(message: ChatMessage) {
  const date = messageDate(message)
  const now = new Date()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  const time = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  }).format(date)
  if (isSameCalendarDay(date, now)) return `今天 ${time}`
  if (isSameCalendarDay(date, yesterday)) return `昨天 ${time}`
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day} ${time}`
}

function senderLabel(message: ChatMessage) {
  if (message.sender_type === 'customer') {
    return '我'
  }
  if (message.sender_type === 'staff') return '人工客服'
  if (message.sender_type === 'system') return '系统'
  return '智能助手'
}

function senderAvatar(message: ChatMessage) {
  if (message.sender_type === 'customer') return '我'
  if (message.sender_type === 'staff') return '人'
  if (message.sender_type === 'system') return '系'
  return 'AI'
}

function messageClass(message: ChatMessage) {
  if (message.sender_type === 'customer') return 'from-user'
  if (message.sender_type === 'staff') return 'from-staff'
  if (message.sender_type === 'system') return 'from-system'
  return 'from-agent'
}

/** 找到该 AI 回复之前的客户问题，用于通过现有受鉴权的回复链路重新生成。 */
function regeneratePrompt(message: ChatMessage) {
  const index = props.messages.findIndex((item) => item.id === message.id)
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const candidate = props.messages[cursor]
    if (candidate.sender_type === 'customer' && candidate.extra_data?.route_target !== 'human') return candidate.content
  }
  return ''
}

/** 复制客户可见的原始消息文本；Markdown 渲染结果不参与复制，避免混入页面结构。 */
async function copyMessage(content: string) {
  const text = String(content || '').trim()
  if (!text) {
    ElMessage.warning('暂无可复制内容')
    return
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      // HTTP 或受限浏览器无法使用 Clipboard API 时，使用临时文本框完成兼容复制。
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.cssText = 'position:fixed;opacity:0;pointer-events:none;'
      document.body.appendChild(textarea)
      textarea.select()
      const copied = document.execCommand('copy')
      document.body.removeChild(textarea)
      if (!copied) throw new Error('copy_failed')
    }
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

function handleInputKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || isComposing.value) return
  event.preventDefault()
  if (props.submitting || !props.modelValue.trim()) return
  emit('submit', activeTab.value)
}

async function scrollToBottom() {
  await nextTick()
  const target = chatWindowRef.value
  if (target) {
    // 首次进入、切换会话和历史消息回填必须直接定位，避免整段会话从顶部缓慢滑到底部。
    target.scrollTo({ top: target.scrollHeight, behavior: 'auto' })
  }
}

// KeepAlive 页面恢复时默认回到智能助手，并在浏览器绘制前校准历史消息位置。
onActivated(() => {
  switchConversationChannel('ai')
  void scrollToBottom()
})

watch(
  () => [visibleMessages.value.length, props.lastReply?.answer, activeTab.value],
  () => {
    void scrollToBottom()
  },
  { immediate: true }
)

watch(
  () => props.routeTarget,
  (target) => {
    // 父组件从快捷操作、取消人工排队等入口改写目标时，同步修正当前展示页签。
    if (target === 'ai' || hasHumanSession.value) activeTab.value = target
  },
  { immediate: true }
)

watch(
  () => ({
    sessionId: props.session?.session_id || '',
    handoffStatus: props.session?.handoff_status || ''
  }),
  (current, previous) => {
    // 首次加载或切换历史会话时始终进入智能助手，人工记录只决定页签是否可选。
    if (!previous || current.sessionId !== previous.sessionId) {
      switchConversationChannel('ai')
      // 切换会话后恢复展开，避免把上一会话的界面偏好误带入当前人工记录。
      handoffDetailsCollapsed.value = false
      return
    }
    if (current.handoffStatus === 'CLOSED' && current.handoffStatus !== previous.handoffStatus) {
      switchConversationChannel('ai')
      return
    }
    // 只有同一会话在本轮中新进入人工接管状态时才自动展示人工页签。
    if (current.handoffStatus && current.handoffStatus !== previous.handoffStatus) {
      // 接管状态变化只更新状态卡，不强制切换页签。
      // 否则 AI 触发转人工后，客户下一条普通提问会被错误投递到人工通道。
      handoffDetailsCollapsed.value = false
    }
  },
  { immediate: true }
)
</script>

<template>
  <section class="chat-panel dashboard-card">
    <div class="chat-header">
      <div>
        <h2>在线客服 / 智能助手</h2>
        <p><span class="online-dot"></span>智能助手 · 7x24小时为您服务</p>
      </div>
      <div class="chat-header-actions">
        <el-segmented
          :model-value="activeTab"
          :options="[
            { label: `智能助手 ${aiMessages.length}`, value: 'ai' },
            { label: `人工客服 ${humanMessages.length}`, value: 'human', disabled: !hasHumanSession }
          ]"
          class="chat-channel-tabs"
          @update:model-value="switchConversationChannel($event)"
        />
        <el-button :icon="Headset" plain @click="emit('quick', '转人工客服')">转人工客服</el-button>
      </div>
    </div>

    <div class="chat-context-strip">
      <span v-if="selectedOrder">
        <el-icon><Message /></el-icon>
        当前订单：{{ selectedOrder.orderNo }}
      </span>
      <span v-if="selectedTicket">
        <el-icon><Tickets /></el-icon>
        当前工单：{{ selectedTicket.ticketNo }}
      </span>
      <span v-if="!selectedOrder && !selectedTicket">当前为无订单咨询，可直接提问规则、发票、会员或人工服务问题</span>
    </div>

    <div
      v-if="activeTab === 'human' && handoffStatusText"
      :class="['human-status-card', { 'is-collapsed': handoffDetailsCollapsed }]"
    >
      <div class="human-status-main">
        <div class="human-status-title">
          <span>当前状态</span>
          <strong>{{ handoffStatusText }}</strong>
        </div>
        <div class="human-status-actions">
          <el-button v-if="session?.handoff_status === 'PENDING'" size="small" @click="emit('cancel-handoff')">取消排队</el-button>
          <el-tag :type="handoffStatusType">{{ session?.handoff_status }}</el-tag>
          <el-button
            class="handoff-collapse-button"
            text
            size="small"
            :icon="handoffDetailsCollapsed ? ArrowDown : ArrowUp"
            :aria-expanded="String(!handoffDetailsCollapsed)"
            @click="handoffDetailsCollapsed = !handoffDetailsCollapsed"
          >
            {{ handoffDetailsCollapsed ? '展开详情' : '收起详情' }}
          </el-button>
        </div>
      </div>
      <p v-if="handoffDetailsCollapsed" class="handoff-collapsed-summary">
        {{ selectedTicket ? `已关联工单 ${selectedTicket.ticketNo}` : '人工客服正在查看当前会话' }}，您可在下方继续补充信息。
      </p>
      <el-collapse-transition>
        <div v-show="!handoffDetailsCollapsed" class="handoff-detail-content">
          <dl>
            <div>
              <dt>请求时间</dt>
              <dd>{{ session?.human_requested_at || '-' }}</dd>
            </div>
            <div>
              <dt>最近更新</dt>
              <dd>{{ session?.updated_at || '-' }}</dd>
            </div>
            <div>
              <dt>关联工单</dt>
              <dd>{{ selectedTicket?.ticketNo || '-' }}</dd>
            </div>
            <div>
              <dt>预计等待</dt>
              <dd>{{ session?.handoff_status === 'PENDING' ? '排队中' : '-' }}</dd>
            </div>
          </dl>
          <div class="handoff-summary-card">
            <strong>上下文摘要</strong>
            <p>当前订单：{{ selectedOrder?.orderNo || '未选择订单' }}</p>
            <p>当前工单：{{ selectedTicket?.ticketNo || '暂无关联工单' }}</p>
            <p>{{ session?.ai_summary || '人工客服可查看当前会话上下文，您也可以在下方继续补充关键信息。' }}</p>
          </div>
        </div>
      </el-collapse-transition>
    </div>

    <div ref="chatWindowRef" class="chat-window">
      <template v-if="visibleMessages.length">
        <template
          v-for="entry in visibleMessageEntries"
          :key="entry.message.id"
        >
          <time v-if="entry.timeDivider" class="chat-time-divider">{{ entry.timeDivider }}</time>
          <div v-if="entry.message.sender_type === 'system'" class="handoff-system-notice" role="status">
            <el-icon><Service /></el-icon>
            <span>{{ entry.message.content }}</span>
          </div>
          <div
            v-else
            :class="['chat-message', messageClass(entry.message)]"
          >
            <div class="bubble-avatar">{{ senderAvatar(entry.message) }}</div>
            <div class="message-stack">
              <span v-if="entry.message.sender_type !== 'customer'" class="message-sender">{{ senderLabel(entry.message) }}</span>
              <div class="message-bubble-shell">
                <div :class="['chat-bubble', { 'is-short-customer': isShortCustomerMessage(entry.message) }]">
                  <p v-if="entry.message.sender_type === 'customer'">{{ entry.message.content }}</p>
                  <div v-else class="message-markdown" v-html="renderMessageMarkdown(entry.message.content)" />
                </div>
                <div class="message-bubble-actions">
                  <el-tooltip content="复制" placement="bottom">
                    <el-button class="message-copy-action" text circle size="small" :icon="DocumentCopy" aria-label="复制消息" @click="copyMessage(entry.message.content)" />
                  </el-tooltip>
                  <el-tooltip v-if="entry.message.sender_type === 'ai'" content="重新生成" placement="bottom">
                    <el-button class="message-regenerate-action" text circle size="small" :icon="Refresh" aria-label="重新生成回复" :disabled="submitting || !regeneratePrompt(entry.message)" @click="emit('regenerate', regeneratePrompt(entry.message))" />
                  </el-tooltip>
                </div>
              </div>
            </div>
          </div>
        </template>
      </template>
      <div v-else-if="activeTab === 'ai'" class="chat-message from-agent">
        <div class="bubble-avatar">AI</div>
        <div class="message-stack">
          <div class="message-bubble-shell">
            <div class="chat-bubble">
              <p>
                您好，我可以帮您咨询售后规则、发票、会员权益或转人工服务；如需查询物流或申请退货退款，请先选择对应订单。
              </p>
            </div>
            <div class="message-bubble-actions"><el-tooltip content="复制" placement="bottom"><el-button class="message-copy-action" text circle size="small" :icon="DocumentCopy" aria-label="复制消息" @click="copyMessage('您好，我可以帮您咨询售后规则、发票、会员权益或转人工服务；如需查询物流或申请退货退款，请先选择对应订单。')" /></el-tooltip></div>
          </div>
        </div>
      </div>
      <div v-else class="chat-message from-staff">
        <div class="bubble-avatar">人</div>
        <div class="message-stack">
          <div class="message-bubble-shell">
            <div class="chat-bubble">
              <p>人工客服会话已单独展示。您可以在下方补充信息，客服接入后会在这里回复。</p>
            </div>
            <div class="message-bubble-actions"><el-tooltip content="复制" placement="bottom"><el-button class="message-copy-action" text circle size="small" :icon="DocumentCopy" aria-label="复制消息" @click="copyMessage('人工客服会话已单独展示。您可以在下方补充信息，客服接入后会在这里回复。')" /></el-tooltip></div>
          </div>
        </div>
      </div>
      <div v-if="lastReply && activeTab === 'ai'" class="chat-message from-agent">
        <div class="bubble-avatar">AI</div>
        <div class="message-stack">
          <div class="message-bubble-shell">
            <div class="chat-bubble">
              <div class="message-markdown" v-html="renderMessageMarkdown(lastReply.customer_message || lastReply.answer)" />
            </div>
            <!-- 流式回复仅用于展示生成进度；在请求完成并落库前不提供复制，避免复制临时状态文案。 -->
            <div v-if="!submitting" class="message-bubble-actions"><el-tooltip content="复制" placement="bottom"><el-button class="message-copy-action" text circle size="small" :icon="DocumentCopy" aria-label="复制消息" @click="copyMessage(lastReply.customer_message || lastReply.answer)" /></el-tooltip></div>
          </div>
        </div>
      </div>
    </div>

    <div class="quick-actions">
      <el-button :disabled="!selectedOrder" :icon="Connection" @click="emit('quick', '查询物流')">查看物流</el-button>
      <el-button :disabled="!selectedOrder" :icon="Refresh" @click="emit('quick', '申请退货')">申请退货退款</el-button>
      <el-button :icon="Tickets" @click="emit('quick', '催办工单')">催办工单</el-button>
      <el-button :icon="Message" @click="emit('quick', '发票问题')">发票问题</el-button>
      <el-button :icon="Service" @click="emit('quick', '转人工客服')">转人工客服</el-button>
    </div>

    <div class="chat-input-box">
      <div class="route-target-row">
        <span>当前发送给 <strong>{{ activeTab === 'human' ? '人工客服' : '智能助手' }}</strong></span>
        <small>内容由 AI 生成，仅供参考</small>
      </div>
      <div v-if="composerHint && activeTab === 'ai'" class="composer-context-hint">
        <el-icon><Refresh /></el-icon>
        <span>{{ composerHint }}，请补充原因；不填写也可直接发送。</span>
      </div>
      <el-input
        :model-value="modelValue"
        :rows="1"
        :placeholder="inputPlaceholder"
        type="textarea"
        @compositionend="isComposing = false"
        @compositionstart="isComposing = true"
        @keydown="handleInputKeydown"
        @update:model-value="emit('update:modelValue', String($event))"
      />
      <div class="chat-input-footer">
        <div class="input-tools">
          <el-icon><Message /></el-icon>
          <el-icon><Link /></el-icon>
        </div>
        <el-tooltip v-if="submitting" content="停止本次生成，已显示的内容会保留" placement="top">
          <el-button class="generation-stop-button" :icon="VideoPause" aria-label="停止生成" @click="emit('cancel')">
            停止生成
          </el-button>
        </el-tooltip>
        <el-button v-else :icon="Position" circle type="primary" @click="emit('submit', activeTab)" />
      </div>
    </div>
  </section>
</template>
