<script setup lang="ts">
import { Lock, User } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const form = reactive({
  username: 'dispatcher',
  password: '123456'
})

async function handleLogin() {
  loading.value = true
  try {
    const user = await auth.login(form.username, form.password)
    if (user.role !== 'dispatcher') {
      auth.logout()
      ElMessage.error('该账号没有调度权限')
      return
    }
    await router.push('/dispatcher')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-panel">
      <h1>工单调度台</h1>
      <p>Demo 调度账号：dispatcher / 123456</p>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="form.username" :prefix-icon="User" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            :prefix-icon="Lock"
            autocomplete="current-password"
            show-password
            type="password"
          />
        </el-form-item>
        <el-button :loading="loading" type="primary" @click="handleLogin">登录</el-button>
      </el-form>
    </section>
  </main>
</template>
