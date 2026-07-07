<script setup lang="ts">
import { ChatLineRound, Headset, HomeFilled, Notebook, QuestionFilled, Tickets } from '@element-plus/icons-vue'
import { computed } from 'vue'
import type { ChatSession, CurrentUser } from '@/types/api'

const props = defineProps<{
  user: CurrentUser | null
  sessions: ChatSession[]
  selectedSessionId?: string | null
}>()

const emit = defineEmits<{
  selectSession: [sessionId: string]
}>()

const menuItems = [
  { label: '首页', icon: HomeFilled },
  { label: '我的订单', icon: Notebook },
  { label: '我的工单', icon: Tickets },
  { label: '在线客服', icon: ChatLineRound, active: true },
  { label: '帮助中心', icon: QuestionFilled }
]

const fallbackSessions = [
  { title: '智能助手', desc: '关于退货申请进度的查询', time: '10:30' },
  { title: '人工客服-小美', desc: '好的，我帮您催办一下', time: '昨天' },
  { title: '智能助手', desc: '发票开具流程说明', time: '06-23' }
]

const recentSessions = computed(() => {
  if (!props.sessions.length) {
    return fallbackSessions.map((session) => ({ ...session, sessionId: '' }))
  }

  // 侧边栏优先展示真实会话，避免客户侧首页只停留在静态样例。
  return props.sessions.slice(0, 3).map((session, index) => ({
    sessionId: session.session_id,
    title: session.title || '智能助手',
    desc: session.ai_summary || session.intent || '暂无最新摘要',
    time: session.updated_at ? session.updated_at.slice(5, 16) : index === 0 ? '刚刚' : ''
  }))
})
</script>

<template>
  <aside class="customer-sidebar">
    <div class="sidebar-brand">
      <div class="brand-bot">🤖</div>
      <strong>智能客服中心</strong>
    </div>

    <section class="customer-card">
      <div class="avatar">{{ user?.display_name?.slice(0, 1) || 'D' }}</div>
      <div>
        <h3>{{ user?.display_name || 'Demo Customer' }}</h3>
        <p>普通客户</p>
      </div>
      <div class="points">积分 <strong>2,680</strong></div>
    </section>

    <nav class="sidebar-menu">
      <button v-for="item in menuItems" :key="item.label" :class="{ active: item.active }">
        <el-icon><component :is="item.icon" /></el-icon>
        {{ item.label }}
      </button>
    </nav>

    <section class="recent-sessions">
      <div class="section-head">
        <strong>最近会话</strong>
        <span>查看全部</span>
      </div>
      <button
        v-for="session in recentSessions"
        :key="session.title + session.time"
        :class="['recent-row', { active: session.sessionId && session.sessionId === selectedSessionId }]"
        :disabled="!session.sessionId"
        @click="session.sessionId && emit('selectSession', session.sessionId)"
      >
        <div class="mini-avatar">{{ session.title.includes('人工') ? '👩' : '🤖' }}</div>
        <div>
          <strong>{{ session.title }}</strong>
          <p>{{ session.desc }}</p>
        </div>
        <time>{{ session.time }}</time>
      </button>
    </section>

    <button class="contact-button">
      <el-icon><Headset /></el-icon>
      联系客服
    </button>
  </aside>
</template>
