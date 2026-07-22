<script setup lang="ts">
import ServiceLoginShell from '@/components/auth/ServiceLoginShell.vue'

/** 坐席登录页使用实际初始化的坐席账号，避免原型账号与 Java 演示数据不一致。 */
const accounts = [
  { username: 'staff', password: '123456', label: '演示坐席' },
  { username: 'staff2', password: '123456', label: '售后坐席' }
]
</script>

<template>
  <ServiceLoginShell
    role="staff"
    title="座席端登录"
    workspace-name="智能座席工作台"
    subtitle="7×24 小时智能协同，提升客服效率与服务质量"
    capability-text="支持工单处理、会话接待、人工接管、知识辅助与服务监控"
    :accounts="accounts"
    :features="[
      { title: '会话接待', description: '接收人工会话，实时响应客户咨询', icon: 'chat' },
      { title: '工单处理', description: '处理与跟踪工单状态', icon: 'ticket' },
      { title: '知识辅助', description: '检索业务知识并生成建议', icon: 'knowledge' }
    ]"
  />
</template>
