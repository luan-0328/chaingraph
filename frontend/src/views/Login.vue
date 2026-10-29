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
  { label: '普通用户', username: 'alice', password: '123456' },
  { label: '客服', username: 'service01', password: '123456' },
  { label: '管理员', username: 'admin', password: 'admin123' },
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
  <div class="login-page">
    <div class="login-card">
      <h1 class="login-card__title">智能电商客服与售后工单平台</h1>
      <p class="login-card__subtitle">请使用您的账号登录</p>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" clearable>
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
            @keyup.enter="handleSubmit"
          >
            <template #prefix>
              <el-icon><Lock /></el-icon>
            </template>
          </el-input>
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            class="login-card__submit"
            :loading="loading"
            @click="handleSubmit"
          >
            {{ loading ? '登录中…' : '登 录' }}
          </el-button>
        </el-form-item>
      </el-form>

      <div class="login-card__footer">
        <span>还没有账号？</span>
        <el-button link type="primary" @click="router.push({ name: 'register' })">
          注册新账号
        </el-button>
      </div>

      <el-divider>演示账号（点击填入）</el-divider>
      <div class="login-card__demo">
        <el-tag
          v-for="account in demoAccounts"
          :key="account.username"
          class="login-card__demo-tag"
          type="info"
          effect="plain"
          @click="fill(account)"
        >
          {{ account.label }}：{{ account.username }}
        </el-tag>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--page-bg);
}

.login-card {
  width: 400px;
  padding: 32px;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 2px 12px rgb(0 0 0 / 6%);
}

.login-card__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  text-align: center;
}

.login-card__subtitle {
  margin: 8px 0 24px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}

.login-card__submit {
  width: 100%;
}

.login-card__footer {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--text-secondary);
  font-size: 13px;
}

.login-card__demo {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.login-card__demo-tag {
  cursor: pointer;
}
</style>
