<script setup lang="ts">
import ServiceLoginShell from '@/components/auth/ServiceLoginShell.vue'

/** 客户登录页只配置客户角色和客户自助服务文案，通用登录交互由共享外壳统一实现。 */
const accounts = [
  { username: 'demo', password: '123456', label: '演示客户' },
  { username: 'buyer', password: '123456', label: '测试买家' }
]
</script>

<template>
  <ServiceLoginShell
    role="customer"
    title="客户自助服务"
    workspace-name="一站式客户自助服务平台"
    subtitle="7×24 小时智能服务，提升客户自助效率与体验"
    capability-text="支持订单查询、工单跟踪、在线咨询与帮助中心"
    :accounts="accounts"
    :features="[
      { title: '智能咨询', description: 'AI 智能客服 7×24 小时解答问题', icon: 'chat' },
      { title: '工单闭环', description: '工单全流程跟踪，高效解决问题', icon: 'ticket' },
      { title: '自助服务', description: '订单、物流、售后随时可查', icon: 'service' }
    ]"
  />
</template>
