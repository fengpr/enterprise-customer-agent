<script setup lang="ts">
import {
  ChatDotRound,
  CircleCheckFilled,
  DataAnalysis,
  Headset,
  Hide,
  InfoFilled,
  Lock,
  Reading,
  Service,
  Tickets,
  User,
  UserFilled,
  View
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

type LoginRole = 'customer' | 'staff'

interface DemoAccount {
  username: string
  password: string
  label: string
}

const props = defineProps<{
  role: LoginRole
  title: string
  workspaceName: string
  subtitle: string
  capabilityText: string
  accounts: DemoAccount[]
  features: Array<{ title: string; description: string; icon: 'chat' | 'ticket' | 'service' | 'knowledge' }>
}>()

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const remember = ref(true)
const showPassword = ref(false)
const showPolicy = ref(false)
const showTerms = ref(false)
// 用轻量视差增强品牌区的空间感；只影响装饰层，不影响登录表单与键盘操作。
const brandPointer = reactive({ x: 50, y: 50 })
let pointerFrame = 0
let queuedPointer = { x: 50, y: 50 }
const form = reactive({
  username: props.accounts[0]?.username || '',
  password: props.accounts[0]?.password || ''
})

const loginButtonText = computed(() => props.role === 'customer' ? '登录' : '登录工作台')
const contactText = computed(() => props.role === 'customer' ? '联系客服支持' : '联系管理员')
const homePath = computed(() => props.role === 'customer' ? '/customer' : '/staff')

/** 选择真实存在的演示账号，便于面试或本地演示时快速进入对应角色工作台。 */
function useDemoAccount(account: DemoAccount) {
  form.username = account.username
  form.password = account.password
  ElMessage.success(`已填入${account.label}账号`)
}

/** 校验账号角色并完成登录；记住我决定 Token 写入 localStorage 还是 sessionStorage。 */
async function handleLogin() {
  if (!form.username.trim() || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const user = await auth.login(form.username.trim(), form.password, remember.value)
    if (user.role !== props.role) {
      auth.logout()
      ElMessage.error(props.role === 'customer' ? '该账号不是客户账号' : '该账号没有坐席权限')
      return
    }
    await router.push(homePath.value)
  } catch {
    ElMessage.error('登录失败，请检查账号、密码和后端服务状态')
  } finally {
    loading.value = false
  }
}

/** 访客体验仍走真实鉴权，避免未授权访问客户或坐席工作台。 */
async function startPreview() {
  const demo = props.accounts[0]
  if (!demo) return
  useDemoAccount(demo)
  remember.value = false
  await handleLogin()
}

async function showForgotPassword() {
  await ElMessageBox.alert(
    '演示环境暂不提供自助重置密码。请联系管理员重置正式账号，或使用页面展示的演示账号体验系统。',
    '找回密码',
    { confirmButtonText: '我知道了' }
  )
}

async function showSupport() {
  await ElMessageBox.alert(
    props.role === 'customer'
      ? '您可以登录后在智能客服中选择“转人工客服”，或联系平台管理员获取帮助。'
      : '请联系系统管理员核验账号权限、坐席状态或服务可用性。',
    contactText.value,
    { confirmButtonText: '我知道了' }
  )
}

function featureIcon(type: string) {
  return { chat: ChatDotRound, ticket: Tickets, service: Service, knowledge: Reading }[type] || CircleCheckFilled
}

function updateBrandPointer(event: PointerEvent) {
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  queuedPointer = {
    x: Math.max(0, Math.min(100, ((event.clientX - rect.left) / rect.width) * 100)),
    y: Math.max(0, Math.min(100, ((event.clientY - rect.top) / rect.height) * 100))
  }
  // 鼠标事件频率可能高于屏幕刷新率，按帧写入 CSS 变量避免频繁触发 Vue 渲染。
  if (pointerFrame) return
  pointerFrame = window.requestAnimationFrame(() => {
    brandPointer.x = queuedPointer.x
    brandPointer.y = queuedPointer.y
    pointerFrame = 0
  })
}

function resetBrandPointer() {
  if (pointerFrame) window.cancelAnimationFrame(pointerFrame)
  pointerFrame = 0
  brandPointer.x = 50
  brandPointer.y = 50
}

onBeforeUnmount(() => {
  if (pointerFrame) window.cancelAnimationFrame(pointerFrame)
})
</script>

<template>
  <main class="service-login-page" :class="`service-login-page--${role}`">
    <div class="login-bridge" aria-hidden="true" />
    <section
      class="service-login-brand"
      :style="{
        '--showcase-x': `${(brandPointer.x - 50) / 10}px`,
        '--showcase-y': `${(brandPointer.y - 50) / 12}px`,
        '--showcase-rotate-x': `${2 + (brandPointer.y - 50) / 28}deg`,
        '--showcase-rotate-y': `${-4 + (brandPointer.x - 50) / 18}deg`
      }"
      aria-label="智能客服中心介绍"
      @pointerleave="resetBrandPointer"
      @pointermove="updateBrandPointer"
    >
      <div class="brand-particles" aria-hidden="true"><i v-for="index in 10" :key="index" /></div>
      <div class="brand-mark" aria-hidden="true"><span class="brand-robot"><i /><i /></span></div>
      <div class="brand-name">智能客服中心</div>

      <div class="brand-copy">
        <h1>{{ workspaceName }}</h1>
        <p>{{ capabilityText }}</p>
      </div>

      <div class="brand-showcase" aria-hidden="true">
        <div class="showcase-topbar"><span /><span /><span /></div>
        <div class="showcase-layout">
          <div class="showcase-sidebar"><i /><i /><i /><i /></div>
          <div class="showcase-chat">
            <div class="showcase-window-title">智能客服助手</div>
            <div class="showcase-agent-panel">
              <div class="showcase-robot" aria-hidden="true">
                <span class="robot-antenna" />
                <span class="robot-head"><i /><i /></span>
                <span class="robot-body" />
              </div>
              <div class="showcase-bubble">您好，我能为您<br>提供什么帮助？</div>
            </div>
            <div class="showcase-message showcase-message--blue" />
            <div class="showcase-composer" />
          </div>
          <div class="showcase-stat">
            <span class="showcase-donut" />
            <el-icon><DataAnalysis /></el-icon>
            <b>1,286</b><small>今日服务量</small>
          </div>
        </div>
      </div>

      <div class="brand-features">
        <article v-for="feature in features" :key="feature.title" class="brand-feature">
          <el-icon><component :is="featureIcon(feature.icon)" /></el-icon>
          <div><strong>{{ feature.title }}</strong><span>{{ feature.description }}</span></div>
        </article>
      </div>
      <p class="brand-footer"><el-icon><CircleCheckFilled /></el-icon> {{ subtitle }}</p>
    </section>

    <section class="service-login-main">
      <div class="login-orbit login-orbit--top" />
      <div class="login-orbit login-orbit--bottom" />
      <article class="service-login-card">
        <header>
          <h2>{{ title }}</h2>
          <p>欢迎回来，请登录继续使用平台服务</p>
        </header>

        <section class="demo-accounts" aria-label="演示账号">
          <div class="demo-title"><el-icon><InfoFilled /></el-icon> 测试账号</div>
          <button v-for="account in accounts" :key="account.username" type="button" @click="useDemoAccount(account)">
            <el-icon><User /></el-icon><span>{{ account.username }}</span><em>/</em><span>{{ account.password }}</span>
          </button>
        </section>

        <el-form class="service-login-form" @submit.prevent="handleLogin">
          <el-input v-model="form.username" :prefix-icon="UserFilled" autocomplete="username" placeholder="用户名" size="large" />
          <el-input
            v-model="form.password"
            :prefix-icon="Lock"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="current-password"
            placeholder="密码"
            size="large"
            @keyup.enter="handleLogin"
          >
            <template #suffix>
              <el-icon class="password-toggle" role="button" tabindex="0" @click.stop="showPassword = !showPassword" @keydown.enter="showPassword = !showPassword">
                <View v-if="showPassword" />
                <Hide v-else />
              </el-icon>
            </template>
          </el-input>
          <div class="login-options">
            <el-checkbox v-model="remember">记住我</el-checkbox>
            <button type="button" @click="showForgotPassword">忘记密码？</button>
          </div>
          <el-button class="login-submit" :loading="loading" native-type="submit" type="primary">{{ loginButtonText }}</el-button>
        </el-form>

        <div class="login-secondary-actions">
          <el-button :icon="User" plain @click="startPreview">访客体验</el-button>
          <el-button :icon="Headset" plain @click="showSupport">{{ contactText }}</el-button>
        </div>

        <footer class="login-card-footer">
          <span>© 2026 智能客服中心</span>
          <i />
          <button type="button" @click="showPolicy = true">隐私政策</button>
          <i />
          <button type="button" @click="showTerms = true">服务条款</button>
        </footer>
      </article>
    </section>

    <el-dialog v-model="showPolicy" title="隐私政策" width="min(520px, calc(100vw - 32px))">
      <p>系统仅在完成登录、会话服务和工单处理所必需的范围内处理账号与业务数据；客户侧不会展示内部风控、工具原始结果或模型提示词。</p>
    </el-dialog>
    <el-dialog v-model="showTerms" title="服务条款" width="min(520px, calc(100vw - 32px))">
      <p>智能客服回复仅作为服务协助。退款、赔付、订单状态变更等业务结果以平台审核和业务系统记录为准。</p>
    </el-dialog>
  </main>
</template>

<style scoped>
.service-login-page { min-height: 100vh; display: grid; grid-template-columns: minmax(520px, 48%) 1fr; overflow: hidden; background: #f6f9ff; color: #132448; }
.service-login-brand { position: relative; display: flex; flex-direction: column; min-height: 100vh; padding: clamp(42px, 6vw, 82px); overflow: hidden; color: #fff; background: radial-gradient(circle at 75% 45%, #087ee9 0, #0756ce 38%, #061e77 100%); }
.service-login-brand::before, .service-login-brand::after { position: absolute; border: 1px solid rgb(151 210 255 / 28%); border-radius: 50%; content: ''; }
.service-login-brand::before { width: 580px; height: 580px; top: -160px; right: -220px; }.service-login-brand::after { width: 360px; height: 360px; right: -170px; bottom: 40px; }
.brand-mark { display: inline-grid; place-items: center; width: 52px; height: 52px; margin-bottom: 12px; border: 1px solid rgb(255 255 255 / 45%); border-radius: 17px; background: rgb(255 255 255 / 13%); font-size: 31px; }.brand-name { font-size: clamp(30px, 3vw, 48px); font-weight: 800; letter-spacing: .04em; }
.brand-copy { margin-top: clamp(55px, 7vh, 96px); }.brand-copy h1 { margin: 0; font-size: clamp(31px, 3.2vw, 49px); line-height: 1.22; }.brand-copy p { max-width: 650px; margin: 18px 0 0; color: #d6e8ff; font-size: clamp(16px, 1.4vw, 23px); line-height: 1.65; }
.brand-showcase { position: relative; z-index: 1; width: min(680px, 100%); min-height: 286px; margin: 42px auto 34px; padding: 14px; border: 1px solid rgb(255 255 255 / 36%); border-radius: 22px; background: linear-gradient(145deg, rgb(240 248 255 / 96%), rgb(171 213 255 / 76%)); box-shadow: 0 28px 48px rgb(0 30 105 / 28%); transform: perspective(900px) rotateX(3deg) rotateY(-4deg); }
.showcase-topbar { display: flex; gap: 6px; height: 20px; }.showcase-topbar span { width: 8px; height: 8px; border-radius: 50%; background: #85b9ec; }.showcase-layout { display: grid; grid-template-columns: 44px 1fr 130px; gap: 12px; min-height: 226px; }.showcase-sidebar { display: grid; gap: 12px; padding: 13px 11px; border-radius: 10px; background: #0d70dd; }.showcase-sidebar i { height: 8px; border-radius: 8px; background: rgb(255 255 255 / 72%); }.showcase-chat { display: flex; flex-direction: column; gap: 14px; padding: 24px 10px; border-radius: 14px; background: rgb(255 255 255 / 76%); }.showcase-message { width: 76%; height: 30px; border-radius: 12px; }.showcase-message--light { background: #e3edf8; }.showcase-message--blue { align-self: flex-end; background: #338bfa; }.showcase-message.short { width: 53%; }.showcase-composer { height: 38px; margin-top: auto; border: 1px solid #d5e0f1; border-radius: 10px; background: #fff; }.showcase-stat { display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 6px; border-radius: 16px; color: #15356b; background: rgb(255 255 255 / 91%); }.showcase-stat .el-icon { color: #1976ef; font-size: 38px; }.showcase-stat b { font-size: 27px; }.showcase-stat small { color: #7186a8; }
.brand-features { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: auto; }.brand-feature { display: flex; gap: 11px; min-height: 104px; padding: 18px 14px; border: 1px solid rgb(255 255 255 / 15%); border-radius: 14px; background: rgb(27 125 235 / 17%); }.brand-feature > .el-icon { flex: 0 0 auto; font-size: 27px; }.brand-feature div { display: grid; gap: 6px; }.brand-feature strong { font-size: 17px; }.brand-feature span { color: #c8e3ff; font-size: 13px; line-height: 1.5; }.brand-footer { display: flex; gap: 8px; align-items: center; margin: 28px 0 0; color: #d9ebff; font-size: 15px; }
.service-login-main { position: relative; display: grid; place-items: center; min-height: 100vh; padding: 42px 24px; overflow: hidden; background: radial-gradient(circle at 100% 35%, #e7f1ff 0 8%, transparent 8.2%), linear-gradient(145deg, #f7faff, #edf4ff); }.login-orbit { position: absolute; border: 1px solid #d8e8ff; border-radius: 50%; }.login-orbit--top { width: 480px; height: 480px; top: -310px; right: -220px; }.login-orbit--bottom { width: 550px; height: 550px; right: -280px; bottom: -365px; }
.service-login-card { position: relative; z-index: 1; width: min(600px, 100%); overflow: hidden; border: 1px solid rgb(255 255 255 / 85%); border-radius: 24px; background: rgb(255 255 255 / 94%); box-shadow: 0 24px 70px rgb(34 89 167 / 16%); }.service-login-card > header { padding: 44px 48px 26px; text-align: center; }.service-login-card h2 { margin: 0; color: #101828; font-size: clamp(31px, 3vw, 46px); line-height: 1.2; }.service-login-card header p { margin: 13px 0 0; color: #71809a; font-size: 17px; }
.demo-accounts { margin: 0 48px; padding: 17px 20px; border: 1px solid #bbd7ff; border-radius: 12px; background: linear-gradient(135deg, #f5f9ff, #eef6ff); }.demo-title { display: flex; gap: 9px; align-items: center; margin-bottom: 10px; color: #1768eb; font-weight: 700; }.demo-accounts button { display: flex; gap: 10px; align-items: center; width: 100%; padding: 5px 0; border: 0; color: #43526c; background: transparent; font-size: 16px; text-align: left; cursor: pointer; }.demo-accounts button:hover { color: #1467e9; }.demo-accounts em { color: #a4b2c7; font-style: normal; }
.service-login-form { display: grid; gap: 16px; padding: 26px 48px 0; }.service-login-form :deep(.el-input__wrapper) { min-height: 52px; padding: 0 15px; border: 1px solid #c9d9ee; border-radius: 10px; box-shadow: none; }.service-login-form :deep(.el-input__wrapper.is-focus) { border-color: #2878f0; box-shadow: 0 0 0 3px rgb(40 120 240 / 10%); }.login-options { display: flex; justify-content: space-between; align-items: center; margin-top: -2px; }.login-options button, .login-card-footer button { padding: 0; border: 0; color: #1768eb; background: transparent; cursor: pointer; }.login-submit { height: 58px; border-radius: 10px; font-size: 20px; font-weight: 700; }.login-secondary-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; padding: 21px 48px 25px; }.login-secondary-actions .el-button { height: 46px; border-color: #b8d0f6; color: #1768eb; font-size: 16px; }.login-card-footer { display: flex; justify-content: center; align-items: center; gap: 16px; padding: 25px 18px; border-top: 1px solid #e6edf6; color: #8a98ae; font-size: 13px; }.login-card-footer i { width: 1px; height: 14px; background: #d8e0eb; }.login-card-footer button { color: #71809a; }
.password-toggle { color: #71809a; cursor: pointer; outline: none; }

/* 分阶段入场让静态信息具备自然阅读顺序，不使用大幅位移以保证企业端页面的稳定感。 */
.brand-mark { animation: brand-reveal .5s ease-out both; }
.brand-name { animation: brand-reveal .5s .08s ease-out both; }
.brand-copy { animation: brand-reveal .58s .16s ease-out both; }
.brand-feature { animation: brand-reveal .48s ease-out both; }
.brand-feature:nth-child(1) { animation-delay: .28s; }
.brand-feature:nth-child(2) { animation-delay: .36s; }
.brand-feature:nth-child(3) { animation-delay: .44s; }
.brand-footer { animation: brand-reveal .5s .48s ease-out both; }

/* 输入区与按钮统一使用短促、可逆的反馈，降低操作犹豫感。 */
.service-login-form :deep(.el-input__wrapper) { transition: border-color .22s ease, box-shadow .22s ease, transform .22s ease; }
.service-login-form :deep(.el-input__wrapper:hover) { border-color: #8db9f6; }
.service-login-form :deep(.el-input__wrapper.is-focus) { transform: translateY(-1px); }
.login-submit:active { transform: translateY(1px) scale(.988); box-shadow: none; }
.login-secondary-actions .el-button:active { transform: translateY(1px) scale(.985); }

/* 登录页不再使用硬切两栏：中心光桥、玻璃层和同一底色把品牌介绍与登录操作纳入同一空间。 */
.service-login-page { position: relative; isolation: isolate; background: linear-gradient(120deg, #063b9f 0%, #0a6fdf 42%, #edf5ff 55%, #f7faff 100%); }
.service-login-brand { z-index: 1; border-radius: 0 54px 54px 0; box-shadow: 28px 0 70px rgb(4 39 122 / 20%); }
.service-login-main { z-index: 1; margin-left: -44px; padding-left: 68px; background: radial-gradient(circle at 4% 50%, rgb(89 168 255 / 30%), transparent 19%), radial-gradient(circle at 100% 35%, #e7f1ff 0 8%, transparent 8.2%), linear-gradient(145deg, rgb(247 250 255 / 92%), rgb(237 244 255 / 96%)); }
.login-bridge { position: absolute; z-index: 2; top: 8%; bottom: 8%; left: calc(48% - 74px); width: 148px; border-radius: 999px; opacity: .7; pointer-events: none; background: radial-gradient(ellipse at center, rgb(176 223 255 / 72%) 0%, rgb(88 167 255 / 24%) 38%, transparent 71%); filter: blur(8px); animation: bridge-breathe 7s ease-in-out infinite; }
.brand-particles i { position: absolute; z-index: 0; width: 6px; height: 6px; border-radius: 50%; background: #d7f0ff; box-shadow: 0 0 14px #fff; animation: particle-float 5s ease-in-out infinite; }.brand-particles i:nth-child(1) { top: 16%; right: 14%; }.brand-particles i:nth-child(2) { top: 27%; right: 31%; width: 10px; height: 10px; animation-delay: -1.5s; }.brand-particles i:nth-child(3) { top: 49%; right: 7%; animation-delay: -3s; }.brand-particles i:nth-child(4) { top: 68%; left: 11%; width: 8px; height: 8px; animation-delay: -2s; }.brand-particles i:nth-child(5) { top: 77%; right: 22%; animation-delay: -4s; }.brand-particles i:nth-child(6) { top: 35%; left: 20%; animation-delay: -1s; }.brand-particles i:nth-child(7) { top: 56%; left: 6%; animation-delay: -3.5s; }.brand-particles i:nth-child(8) { top: 14%; left: 62%; animation-delay: -2.3s; }.brand-particles i:nth-child(9) { bottom: 9%; left: 48%; animation-delay: -4.4s; }.brand-particles i:nth-child(10) { bottom: 24%; right: 11%; animation-delay: -2.7s; }
.brand-mark, .brand-name, .brand-copy, .brand-features, .brand-footer { position: relative; z-index: 1; }.brand-mark { transition: transform .28s ease, background-color .28s ease, box-shadow .28s ease; }.service-login-brand:hover .brand-mark { transform: rotate(-7deg) scale(1.08); background: rgb(255 255 255 / 22%); box-shadow: 0 12px 25px rgb(0 30 100 / 22%); }
.brand-robot { position: relative; display: flex; justify-content: space-around; align-items: center; width: 29px; height: 22px; border: 3px solid #fff; border-radius: 10px; }.brand-robot::before { position: absolute; top: -11px; left: 11px; width: 3px; height: 8px; border-radius: 4px; background: #fff; content: ''; }.brand-robot::after { position: absolute; top: -16px; left: 8px; width: 9px; height: 9px; border-radius: 50%; background: #fff; content: ''; }.brand-robot i { position: relative; z-index: 1; width: 4px; height: 4px; border-radius: 50%; background: #fff; }
.brand-showcase { transform: perspective(900px) rotateX(var(--showcase-rotate-x)) rotateY(var(--showcase-rotate-y)) translate3d(var(--showcase-x), var(--showcase-y), 0); transition: transform .18s ease-out, box-shadow .3s ease, filter .3s ease; animation: showcase-float 6s ease-in-out infinite; }.service-login-brand:hover .brand-showcase { box-shadow: 0 36px 64px rgb(0 24 100 / 38%); filter: saturate(1.08); }.showcase-stat { animation: stat-pulse 3.5s ease-in-out infinite; }.showcase-message--blue { animation: message-pulse 2.8s ease-in-out infinite; }.showcase-sidebar i:nth-child(2) { animation: menu-shine 2.4s ease-in-out infinite; }
.brand-feature { position: relative; overflow: hidden; transition: transform .25s ease, border-color .25s ease, background-color .25s ease, box-shadow .25s ease; }.brand-feature::after { position: absolute; top: -110%; left: -35%; width: 34%; height: 320%; content: ''; background: linear-gradient(90deg, transparent, rgb(255 255 255 / 24%), transparent); transform: rotate(18deg); transition: left .55s ease; }.brand-feature:hover { transform: translateY(-7px); border-color: rgb(255 255 255 / 44%); background: rgb(99 182 255 / 25%); box-shadow: 0 16px 28px rgb(1 34 104 / 20%); }.brand-feature:hover::after { left: 120%; }.brand-feature > .el-icon { transition: transform .28s ease; }.brand-feature:hover > .el-icon { transform: scale(1.18) rotate(-8deg); }
.service-login-card { backdrop-filter: blur(12px); transition: transform .32s ease, box-shadow .32s ease; animation: card-enter .7s cubic-bezier(.2,.8,.2,1) both; }.service-login-card:hover { transform: translateY(-5px); box-shadow: 0 32px 82px rgb(34 89 167 / 23%); }.demo-accounts { transition: transform .22s ease, box-shadow .22s ease; }.demo-accounts:hover { transform: translateY(-2px); box-shadow: 0 10px 24px rgb(46 120 235 / 10%); }.demo-accounts button { border-radius: 7px; transition: transform .18s ease, background-color .18s ease, padding-left .18s ease; }.demo-accounts button:hover { padding-left: 7px; background: rgb(37 112 238 / 6%); transform: translateX(2px); }.login-submit { position: relative; overflow: hidden; transition: transform .2s ease, box-shadow .2s ease; }.login-submit::after { position: absolute; top: 0; left: -130%; width: 70%; height: 100%; content: ''; background: linear-gradient(90deg, transparent, rgb(255 255 255 / 40%), transparent); transform: skewX(-20deg); transition: left .55s ease; }.login-submit:hover { transform: translateY(-2px); box-shadow: 0 12px 22px rgb(22 104 235 / 28%); }.login-submit:hover::after { left: 150%; }.login-secondary-actions .el-button { transition: transform .2s ease, background-color .2s ease, box-shadow .2s ease; }.login-secondary-actions .el-button:hover { transform: translateY(-3px); background: #f1f7ff; box-shadow: 0 8px 18px rgb(28 102 221 / 12%); }.login-card-footer button:hover, .login-options button:hover { text-decoration: underline; text-underline-offset: 3px; }
/* 动效收敛：保留一次性入场和交互反馈，移除循环跳动与布局属性动画。 */
.login-bridge { animation: none; opacity: .58; filter: blur(10px); }
.brand-particles i { width: 4px; height: 4px; opacity: .38; box-shadow: 0 0 8px rgb(215 240 255 / 62%); animation-duration: 13s; animation-timing-function: cubic-bezier(.45, 0, .55, 1); }
.brand-particles i:nth-child(n + 7) { display: none; }
.brand-showcase { margin-top: 42px; will-change: transform; backface-visibility: hidden; animation: none; transition: transform .34s cubic-bezier(.22, .61, .36, 1), box-shadow .34s ease, filter .34s ease; }
.showcase-stat, .showcase-message--blue, .showcase-sidebar i:nth-child(2) { animation: none; }
.showcase-stat { transition: transform .28s ease, box-shadow .28s ease; }.service-login-brand:hover .showcase-stat { transform: translateY(-3px); box-shadow: 0 10px 18px rgb(28 92 184 / 14%); }
.brand-feature, .service-login-card, .demo-accounts, .login-submit, .login-secondary-actions .el-button { will-change: transform; }

/* 以原型的“客服工作台 + 机器人”作为视觉骨架，避免仅用抽象色块替代核心插画信息。 */
.brand-showcase { width: min(650px, 94%); min-height: 332px; padding: 12px 14px 16px; border-radius: 20px; background: linear-gradient(145deg, #d7eaff, #8dc6fb); }
.showcase-topbar { align-items: center; height: 31px; }.showcase-topbar::after { margin-left: 8px; color: #1260c9; font-size: 14px; font-weight: 700; content: '智能客服助手'; }.showcase-layout { grid-template-columns: 38px 1fr 152px; min-height: 270px; gap: 10px; }.showcase-sidebar { gap: 18px; padding: 17px 9px; border-radius: 11px; background: linear-gradient(#2792ff, #0876e8); }.showcase-sidebar i { height: 7px; background: rgb(255 255 255 / 76%); }
.showcase-chat { position: relative; gap: 10px; padding: 10px; overflow: hidden; border-radius: 14px; background: #f5faff; }.showcase-window-title { color: #2468bc; font-size: 12px; font-weight: 700; }.showcase-agent-panel { display: flex; align-items: center; gap: 13px; min-height: 122px; padding: 12px; border-radius: 13px; background: linear-gradient(135deg, #dcebfb, #edf6ff); }.showcase-robot { position: relative; flex: 0 0 auto; width: 78px; height: 84px; }.robot-antenna { position: absolute; top: 0; left: 37px; width: 5px; height: 18px; border-radius: 6px; background: #328cff; }.robot-antenna::before { position: absolute; top: -7px; left: -3px; width: 11px; height: 11px; border-radius: 50%; background: #2b8bff; content: ''; }.robot-head { position: absolute; top: 17px; left: 5px; display: flex; justify-content: space-around; align-items: center; width: 67px; height: 45px; border: 5px solid #fff; border-radius: 19px; background: linear-gradient(160deg, #135ebd, #072b75); box-shadow: 0 6px 12px rgb(29 102 206 / 22%); }.robot-head i { width: 8px; height: 8px; border-radius: 50%; background: #70f3ff; box-shadow: 0 0 8px #70f3ff; }.robot-body { position: absolute; right: 10px; bottom: 0; left: 10px; height: 27px; border-radius: 18px 18px 10px 10px; background: linear-gradient(90deg, #b7e3ff, #fff, #b7e3ff); }.showcase-bubble { position: relative; padding: 12px 11px; border-radius: 8px; color: #2a5f98; background: #fff; font-size: 12px; line-height: 1.55; box-shadow: 0 5px 12px rgb(73 133 196 / 12%); }.showcase-bubble::before { position: absolute; top: 27px; left: -7px; width: 14px; height: 14px; background: #fff; content: ''; transform: rotate(45deg); }.showcase-bubble { z-index: 1; }
.showcase-message--blue { width: 66%; height: 18px; margin: 0 auto; border-radius: 8px; background: #2e88f4; }.showcase-composer { height: 31px; }.showcase-stat { position: relative; align-items: flex-start; justify-content: flex-start; padding: 19px 14px; overflow: hidden; background: linear-gradient(145deg, #fff, #edf6ff); }.showcase-stat .el-icon { position: absolute; top: 15px; right: 14px; font-size: 20px; }.showcase-stat b { z-index: 1; margin-top: 49px; font-size: 25px; }.showcase-stat small { z-index: 1; }.showcase-donut { position: absolute; top: 19px; left: 18px; width: 58px; height: 58px; border-radius: 50%; background: conic-gradient(#1b89f4 0 52%, #29c8c6 52% 74%, #f1c43d 74% 100%); }.showcase-donut::after { position: absolute; inset: 12px; border-radius: 50%; background: #f8fbff; content: ''; }
@keyframes bridge-breathe { 0%, 100% { opacity: .46; transform: scaleY(.94); } 50% { opacity: .86; transform: scaleY(1.05); } } @keyframes particle-float { 0%, 100% { transform: translate3d(0, 0, 0); opacity: .45; } 50% { transform: translate3d(8px, -15px, 0); opacity: 1; } } @keyframes showcase-float { 0%, 100% { margin-top: 42px; } 50% { margin-top: 33px; } } @keyframes stat-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.035); } } @keyframes message-pulse { 0%, 100% { opacity: .86; } 50% { opacity: 1; } } @keyframes menu-shine { 0%, 100% { opacity: .55; } 50% { opacity: 1; } } @keyframes card-enter { from { transform: translateY(22px) scale(.98); opacity: 0; } to { transform: translateY(0) scale(1); opacity: 1; } } @keyframes brand-reveal { from { transform: translateY(12px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; } }
@media (max-width: 960px) { .service-login-page { grid-template-columns: 1fr; background: #f4f8ff; }.service-login-brand, .login-bridge { display: none; }.service-login-main { min-height: 100vh; margin-left: 0; padding-left: 24px; }.service-login-card { max-width: 600px; } }
@media (max-width: 520px) { .service-login-main { padding: 16px; }.service-login-card { border-radius: 18px; }.service-login-card > header { padding: 32px 24px 20px; }.demo-accounts, .service-login-form, .login-secondary-actions { margin-left: 24px; margin-right: 24px; padding-left: 0; padding-right: 0; }.demo-accounts { margin: 0 24px; padding: 15px; }.login-secondary-actions { padding-bottom: 20px; }.login-card-footer { gap: 10px; font-size: 12px; } }
</style>
