<script setup lang="ts">
/**
 * 登录页（v2.md §51）。
 * 普通企业后台登录页：用户名 + 密码 + 登录。不要 AI 粒子/机器人/巨型聊天框。
 *
 * 页面上列出演示账号（后端 dev 环境自动创建），方便直接体验三种角色。
 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import AuthShell from '@/components/auth/AuthShell.vue'
import { useAuthStore } from '@/stores/auth'
import type { LoginRequest } from '@/types'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive<LoginRequest>({ username: '', password: '' })

const rules: FormRules<LoginRequest> = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/** 演示账号（与 backend/app/repositories/seed.py 一致） */
const demoAccounts = [
  { label: '普通用户', username: 'alice', password: '123456', icon: 'User' },
  { label: '客服', username: 'service01', password: '123456', icon: 'Service' },
  { label: '管理员', username: 'admin', password: 'admin123', icon: 'Setting' },
]

function fill(account: { username: string; password: string }): void {
  form.username = account.username
  form.password = account.password
}

async function handleSubmit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    const user = await auth.login({ ...form })
    ElMessage.success(`欢迎回来，${user.nickname || user.username}`)

    // 登录前想去的页面（被守卫拦下时会记在 query.redirect）
    const redirect = route.query.redirect
    if (typeof redirect === 'string' && redirect) {
      await router.replace(redirect)
      return
    }
    // 否则按角色回各自首页：客服进工作台、管理员进管理端、用户进商城
    const home = useAuthStore().homeRoute
    await router.replace(home)
  } catch {
    // 401 等错误已由拦截器提示
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthShell>
    <header class="auth-head">
      <h1 class="auth-head__title">欢迎回来</h1>
      <p class="auth-head__sub">登录后继续处理你的订单、售后与工单。</p>
    </header>

    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-position="top"
      size="large"
      @submit.prevent="handleSubmit"
    >
      <el-form-item label="用户名" prop="username">
        <el-input
          v-model="form.username"
          placeholder="请输入用户名"
          clearable
          autocomplete="username"
        >
          <template #prefix>
            <el-icon><User /></el-icon>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="密码" prop="password">
        <el-input
          v-model="form.password"
          type="password"
          placeholder="请输入密码"
          show-password
          autocomplete="current-password"
          @keyup.enter="handleSubmit"
        >
          <template #prefix>
            <el-icon><Lock /></el-icon>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item class="auth-form__action">
        <el-button type="primary" class="auth-submit" :loading="loading" @click="handleSubmit">
          {{ loading ? '登录中…' : '登 录' }}
        </el-button>
      </el-form-item>
    </el-form>

    <div class="auth-switch">
      <span>还没有账号？</span>
      <el-button link type="primary" @click="router.push({ name: 'register' })">
        注册新账号
      </el-button>
    </div>

    <div class="auth-demo">
      <div class="auth-demo__head">
        <span class="auth-demo__title">演示账号</span>
        <span class="auth-demo__hint">点击卡片填入</span>
      </div>
      <div class="auth-demo__grid">
        <button
          v-for="account in demoAccounts"
          :key="account.username"
          type="button"
          class="demo-card"
          @click="fill(account)"
        >
          <span class="demo-card__icon">
            <el-icon><component :is="account.icon" /></el-icon>
          </span>
          <span class="demo-card__label">{{ account.label }}</span>
          <span class="demo-card__user">{{ account.username }}</span>
        </button>
      </div>
      <p class="auth-demo__passwords">用户 / 客服密码 123456 · 管理员 admin123</p>
    </div>
  </AuthShell>
</template>

<style scoped>
.auth-demo {
  margin-top: 22px;
  padding-top: 18px;
  border-top: 1px dashed var(--border-color);
}

.auth-demo__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.auth-demo__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-regular);
  letter-spacing: 0.4px;
}

.auth-demo__hint {
  color: var(--text-secondary);
  font-size: 11px;
}

.auth-demo__grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.demo-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 10px 10px 9px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: #fff;
  font-family: inherit;
  cursor: pointer;
  text-align: left;
  transition:
    border-color var(--dur) var(--ease),
    background-color var(--dur) var(--ease),
    transform var(--dur) var(--ease);
}

.demo-card:hover {
  border-color: #c4d3fb;
  background: var(--brand-softer);
  transform: translateY(-1px);
}

.demo-card__icon {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 13px;
  margin-bottom: 4px;
}

.demo-card__label {
  color: var(--text-primary);
  font-size: 12px;
  font-weight: 600;
}

.demo-card__user {
  color: var(--text-secondary);
  font-size: 11px;
  font-family: var(--font-mono);
}

.auth-demo__passwords {
  margin: 10px 0 0;
  color: #a9b0bf;
  font-size: 11px;
}
</style>
