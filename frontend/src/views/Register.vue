<script setup lang="ts">
/**
 * 注册页（v2.md §6.1）。
 * 注册的永远是普通用户（USER）—— 客服/管理员账号由管理员分配，不能自助注册。
 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import type { RegisterRequest } from '@/types'

const auth = useAuthStore()
const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive<RegisterRequest & { confirm_password: string }>({
  username: '',
  password: '',
  confirm_password: '',
  nickname: '',
  email: '',
  phone: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 64, message: '用户名 3~64 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 128, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirm_password: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function handleSubmit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    const { confirm_password: _ignored, ...payload } = form
    await auth.register(payload)
    ElMessage.success('注册成功，请登录')
    await router.push({ name: 'login' })
  } catch {
    // 拦截器已提示（例如用户名已存在 → 409）
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-card__title">注册账号</h1>
      <p class="auth-card__subtitle">注册后即可购物、申请售后并提交工单</p>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" size="large">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="3~64 个字符" clearable />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm_password">
          <el-input v-model="form.confirm_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="昵称（可选）">
          <el-input v-model="form.nickname" placeholder="客服称呼您的名字" clearable />
        </el-form-item>
        <el-form-item label="手机号（可选）">
          <el-input v-model="form.phone" clearable />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            class="auth-card__submit"
            :loading="loading"
            @click="handleSubmit"
          >
            {{ loading ? '注册中…' : '注 册' }}
          </el-button>
        </el-form-item>
      </el-form>

      <div class="auth-card__footer">
        <span>已有账号？</span>
        <el-button link type="primary" @click="router.push({ name: 'login' })">去登录</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--page-bg);
}

.auth-card {
  width: 400px;
  padding: 32px;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 2px 12px rgb(0 0 0 / 6%);
}

.auth-card__title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  text-align: center;
}

.auth-card__subtitle {
  margin: 8px 0 24px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}

.auth-card__submit {
  width: 100%;
}

.auth-card__footer {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding-top: 12px;
  border-top: 1px dashed var(--border-color);
  color: var(--text-secondary);
  font-size: 13px;
}
</style>
