<script setup lang="ts">
/**
 * 注册页（v2.md §6.1）。
 * 注册的永远是普通用户（USER）—— 客服/管理员账号由管理员分配，不能自助注册。
 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import AuthShell from '@/components/auth/AuthShell.vue'
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
    // 只提交注册接口认识的字段（confirm_password 仅用于前端校验）
    const payload: RegisterRequest = {
      username: form.username,
      password: form.password,
      nickname: form.nickname || undefined,
      email: form.email || undefined,
      phone: form.phone || undefined,
    }
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
  <AuthShell>
    <header class="auth-head">
      <h1 class="auth-head__title">注册账号</h1>
      <p class="auth-head__sub">注册后即可购物、申请售后并提交工单。</p>
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
          placeholder="3~64 个字符"
          clearable
          autocomplete="username"
        />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input
          v-model="form.password"
          type="password"
          show-password
          placeholder="至少 6 位"
          autocomplete="new-password"
        />
      </el-form-item>
      <el-form-item label="确认密码" prop="confirm_password">
        <el-input
          v-model="form.confirm_password"
          type="password"
          show-password
          autocomplete="new-password"
        />
      </el-form-item>

      <div class="auth-row">
        <el-form-item label="昵称（可选）">
          <el-input v-model="form.nickname" placeholder="客服称呼你的名字" clearable />
        </el-form-item>
        <el-form-item label="手机号（可选）">
          <el-input v-model="form.phone" placeholder="用于售后联系" clearable />
        </el-form-item>
      </div>

      <el-form-item class="auth-form__action">
        <el-button type="primary" class="auth-submit" :loading="loading" @click="handleSubmit">
          {{ loading ? '注册中…' : '注 册' }}
        </el-button>
      </el-form-item>
    </el-form>

    <div class="auth-switch">
      <span>已有账号？</span>
      <el-button link type="primary" @click="router.push({ name: 'login' })">去登录</el-button>
    </div>
  </AuthShell>
</template>

<style scoped>
.auth-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 12px;
}

@media (max-width: 480px) {
  .auth-row {
    grid-template-columns: 1fr;
  }
}
</style>
