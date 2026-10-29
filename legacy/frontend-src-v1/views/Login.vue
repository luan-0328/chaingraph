<script setup lang="ts">
/**
 * 登录页（前端文档第七节）。
 *
 * 就是一个普通的企业后台登录页：标题 + 用户名 + 密码 + 登录按钮。
 * 不要 AI 粒子、不要机器人头像、不要"你好我是 AI 助手"。
 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import type { LoginRequest } from '@/types/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive<LoginRequest>({
  username: '',
  password: '',
})

const rules: FormRules<LoginRequest> = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleSubmit(): Promise<void> {
  if (!formRef.value) return

  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.login({ ...form })
    ElMessage.success('登录成功')

    // 登录前想去的页面（被守卫拦下来时会记在 query.redirect）
    const redirect = route.query.redirect
    await router.replace(typeof redirect === 'string' ? redirect : { name: 'ticket-list' })
  } catch {
    // 错误提示已经由 axios 拦截器统一弹过了，这里只需要停掉 loading
  } finally {
    loading.value = false
  }
}

/** 一键填入演示账号（后端 dev 环境会自动给这两个账号设初始密码） */
function fillDemoAccount(): void {
  form.username = 'demo_user'
  form.password = '123456'
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <h1 class="login-card__title">工单智能处理平台</h1>
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

      <div class="login-card__hint">
        <span>演示账号：demo_user / 123456</span>
        <el-button link type="primary" @click="fillDemoAccount">一键填入</el-button>
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
  width: 380px;
  padding: 32px 32px 24px;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 2px 12px rgb(0 0 0 / 6%);
}

.login-card__title {
  margin: 0;
  font-size: 20px;
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

.login-card__hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 12px;
  border-top: 1px dashed var(--border-color);
  color: var(--text-secondary);
  font-size: 12px;
}
</style>
