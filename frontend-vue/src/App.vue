<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()

// 以客户身份隔离缓存实例，切换首页与智能客服时保留已加载的业务列表。
const customerViewKey = computed(() => `${auth.user?.customer_id ?? 'guest'}:${route.path}`)
</script>

<template>
  <router-view v-slot="{ Component }">
    <KeepAlive :include="['CustomerHome', 'CustomerDashboard', 'CustomerOrders', 'CustomerTickets', 'CustomerHelp']">
      <component :is="Component" :key="customerViewKey" />
    </KeepAlive>
  </router-view>
</template>
