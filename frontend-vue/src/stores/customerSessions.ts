import { defineStore } from 'pinia'
import { ref } from 'vue'

import { customerApi } from '@/api/customer'
import type { ChatSession } from '@/types/api'

/**
 * 客户侧共享会话仓库。
 * 所有客户页面必须复用这一份列表，避免 KeepAlive 页面各自缓存导致最近会话不一致。
 */
export const useCustomerSessionStore = defineStore('customerSessions', () => {
  const sessions = ref<ChatSession[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  let activeRequest: Promise<ChatSession[]> | null = null

  /**
   * 会话始终按“置顶优先、同组内最近更新优先”排列。
   * 新建会话只能进入未置顶分组，不能挤掉侧栏中已经置顶的会话。
   */
  function sortSessions(data: ChatSession[]) {
    return [...data].sort((left, right) => {
      const pinnedDiff = Number(Boolean(right.pinned_at)) - Number(Boolean(left.pinned_at))
      if (pinnedDiff) return pinnedDiff
      if (left.pinned_at && right.pinned_at) {
        const pinnedTimeDiff = right.pinned_at.localeCompare(left.pinned_at)
        if (pinnedTimeDiff) return pinnedTimeDiff
      }
      return right.updated_at.localeCompare(left.updated_at)
    })
  }

  /** 合并并发刷新请求，保证各页面最终接收同一次排序结果。 */
  async function refresh(force = false) {
    if (activeRequest) return activeRequest
    if (loaded.value && !force) return sessions.value

    loading.value = true
    activeRequest = customerApi.sessions(50)
      .then(({ data }) => {
        const sortedSessions = sortSessions(data)
        sessions.value = sortedSessions
        loaded.value = true
        return sortedSessions
      })
      .finally(() => {
        loading.value = false
        activeRequest = null
      })
    return activeRequest
  }

  /** 缓存恢复时只通过仓库写入，确保所有页面同步收到变化。 */
  function replace(data: ChatSession[]) {
    sessions.value = sortSessions(data)
    loaded.value = true
  }

  /** 新建会话成功后立即同步各页面，无需等待下一次接口刷新。 */
  function prepend(session: ChatSession) {
    sessions.value = sortSessions([session, ...sessions.value.filter((item) => item.session_id !== session.session_id)])
    loaded.value = true
  }

  /** 登录身份切换时立即清空，防止短暂展示上一客户的会话标题。 */
  function clear() {
    sessions.value = []
    loaded.value = false
  }

  return { sessions, loaded, loading, refresh, replace, prepend, clear }
})
