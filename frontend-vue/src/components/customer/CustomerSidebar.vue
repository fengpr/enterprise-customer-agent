<script setup lang="ts">
import { ChatLineRound, Headset, HomeFilled, MoreFilled, Notebook, QuestionFilled, Tickets } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { customerApi } from '@/api/customer'
import type { ChatSession, CurrentUser } from '@/types/api'

const props = defineProps<{ prototypeUser?: { name: string; level: string; points: string }; user?: CurrentUser | null; sessions?: ChatSession[]; selectedSessionId?: string | null }>()
const emit = defineEmits<{ contactService: []; unavailable: []; selectSession: [sessionId: string]; sessionsChanged: [payload: { sessionId: string; deleted: boolean }] }>()
const router = useRouter()
const showAllSessions = ref(false)
const user = computed(() => props.prototypeUser || { name: props.user?.display_name || 'Demo Customer', level: '普通客户', points: '2,680' })
type SidebarSession = { title: string; desc: string; time: string; sessionId: string; pinned: boolean }
const sessions = computed<SidebarSession[]>(() => {
  const history = props.sessions?.map((session) => ({ title: session.title || '智能助手', desc: session.ai_summary || session.intent || '暂无最新消息', time: session.updated_at.slice(5, 16), sessionId: session.session_id, pinned: Boolean(session.pinned_at) })) || []
  // 展开与收起始终复用真实会话数据，避免首页和智能客服页出现不同历史记录。
  // 默认展示四条真实会话，兼顾侧栏信息密度与底部联系按钮的稳定位置。
  if (history.length) return showAllSessions.value ? history : history.slice(0, 4)
  return [{ title: '用户咨询订单EC...', desc: '用户反馈未收到...', time: '07-15 13:56', sessionId: '', pinned: false }, { title: '查询我的订单', desc: '用户查询订单EC...', time: '06-24 08:42', sessionId: '', pinned: false }]
})
const contextMenu = ref<{ x: number; y: number; session: SidebarSession } | null>(null)
const menuItems = [
  { label: '首页', icon: HomeFilled, path: '/customer' },
  { label: '我的订单', icon: Notebook, path: '/customer/orders' },
  { label: '我的工单', icon: Tickets, path: '/customer/tickets' },
  { label: '智能客服', icon: ChatLineRound, path: '/customer/service' },
  { label: '帮助中心', icon: QuestionFilled }
]
/** 侧栏同时服务首页与智能客服页，高亮必须由当前路由决定。 */
function isActive(path?: string) { return Boolean(path && router.currentRoute.value.path === path) }
function handleMenu(item: (typeof menuItems)[number]) {
  if (item.path) {
    if (router.currentRoute.value.path !== item.path) void router.push(item.path)
    return
  }
  emit('unavailable')
}
function openSession(sessionId: string) { if (sessionId) emit('selectSession', sessionId); else emit('contactService') }
function toggleAllSessions() { showAllSessions.value = !showAllSessions.value }
function openContextMenu(event: MouseEvent, session: SidebarSession) {
  if (!session.sessionId) return
  contextMenu.value = { x: event.clientX, y: event.clientY, session }
}
function closeContextMenu() { contextMenu.value = null }
async function togglePinned(session: SidebarSession) {
  closeContextMenu()
  try {
    await customerApi.setSessionPinned(session.sessionId, !session.pinned)
    ElMessage.success(session.pinned ? '已取消置顶会话' : '已置顶会话')
    emit('sessionsChanged', { sessionId: session.sessionId, deleted: false })
  } catch {
    // 请求拦截器已显示接口错误，侧栏保留当前可见数据。
  }
}
async function deleteSession(session: SidebarSession) {
  closeContextMenu()
  try {
    await ElMessageBox.confirm(`确认删除“${session.title}”吗？删除后无法在客户侧恢复。`, '删除会话', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
    await customerApi.deleteSession(session.sessionId)
    ElMessage.success('会话已删除')
    emit('sessionsChanged', { sessionId: session.sessionId, deleted: true })
  } catch (error) {
    // 取消确认不提示错误；接口失败由请求拦截器统一反馈。
    if (error !== 'cancel' && error !== 'close') return
  }
}
onMounted(() => window.addEventListener('click', closeContextMenu))
onBeforeUnmount(() => window.removeEventListener('click', closeContextMenu))
</script>

<template>
  <aside class="prototype-sidebar">
    <div class="prototype-brand"><span>🤖</span><strong>智能客服中心</strong></div>
    <section class="prototype-user-card"><div class="prototype-avatar">{{ user.name.slice(0, 1) }}</div><div><h2>{{ user.name }}</h2><p>{{ user.level }}</p></div><div class="prototype-points">积分 <b>{{ user.points }}</b></div></section>
    <nav class="prototype-nav"><button v-for="item in menuItems" :key="item.label" :class="{ active: isActive(item.path) }" @click="handleMenu(item)"><el-icon><component :is="item.icon" /></el-icon>{{ item.label }}</button></nav>
    <section class="prototype-sessions"><div><h2>最近会话</h2><button @click="toggleAllSessions">{{ showAllSessions ? '收起' : '查看全部' }}</button></div><div :class="['prototype-session-list', { expanded: showAllSessions }]"><div v-for="session in sessions" :key="session.sessionId || session.time" class="prototype-session" :class="{ pinned: session.pinned }" @contextmenu.prevent="openContextMenu($event, session)"><button class="prototype-session-main" @click="openSession(session.sessionId)"><i>🤖</i><span><b>{{ session.pinned ? '📌 ' : '' }}{{ session.title }}</b><small>{{ session.desc }}</small></span><time>{{ session.time }}</time></button><button v-if="session.sessionId" class="prototype-session-more" :aria-label="`${session.title}更多操作`" @click.stop="openContextMenu($event, session)"><el-icon><MoreFilled /></el-icon></button></div></div></section>
    <button class="prototype-contact" @click="emit('contactService')"><el-icon><Headset /></el-icon>联系客服</button>
    <div v-if="contextMenu" class="prototype-session-menu" :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }" role="menu" @click.stop><button role="menuitem" @click="togglePinned(contextMenu.session)">{{ contextMenu.session.pinned ? '取消置顶' : '置顶会话' }}</button><button class="danger" role="menuitem" @click="deleteSession(contextMenu.session)">删除会话</button></div>
  </aside>
</template>
