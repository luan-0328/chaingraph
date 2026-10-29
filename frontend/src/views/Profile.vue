<script setup lang="ts">
/**
 * 个人信息（v2.md §6.1）：查看/修改昵称与联系方式、改密码。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { changePassword, getCurrentUser, updateProfile } from '@/api/auth'
import type { UserInfo } from '@/types'
import { roleLabel } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'

const loading = ref(true)
const saving = ref(false)
const changing = ref(false)
const profile = ref<UserInfo | null>(null)

const formRef = ref<FormInstance>()
const passwordRef = ref<FormInstance>()

const form = reactive({ nickname: '', email: '', phone: '' })
const passwordForm = reactive({ old_password: '', new_password: '', confirm: '' })

const rules: FormRules = {
  nickname: [{ max: 64, message: '昵称过长', trigger: 'blur' }],
}

const passwordRules: FormRules = {
  old_password: [{ required: true, message: '请输入原密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '新密码至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    {
      validator: (_rule, value: string, callback) => {
        if (value !== passwordForm.new_password) callback(new Error('两次输入不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    profile.value = await getCurrentUser()
    form.nickname = profile.value.nickname ?? ''
    form.email = profile.value.email ?? ''
    form.phone = profile.value.phone ?? ''
  } finally {
    loading.value = false
  }
}

async function handleSave(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  saving.value = true
  try {
    profile.value = await updateProfile({ ...form })
    ElMessage.success('已保存')
  } finally {
    saving.value = false
  }
}

async function handleChangePassword(): Promise<void> {
  const valid = await passwordRef.value?.validate().catch(() => false)
  if (!valid) return

  changing.value = true
  try {
    await changePassword({
      old_password: passwordForm.old_password,
      new_password: passwordForm.new_password,
    })
    ElMessage.success('密码已修改')
    passwordForm.old_password = ''
    passwordForm.new_password = ''
    passwordForm.confirm = ''
  } catch {
    // 原密码错误 → 400（拦截器已提示）
  } finally {
    changing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2 class="page-header__title">个人信息</h2>
    </div>

    <div class="page-card">
      <h3 class="page-card__title">账号</h3>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="用户名">{{ profile?.username ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="角色">{{ roleLabel(profile?.role) }}</el-descriptions-item>
        <el-descriptions-item label="注册时间">
          {{ formatDateTime(profile?.created_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="用户 ID">{{ profile?.user_id ?? '—' }}</el-descriptions-item>
      </el-descriptions>
    </div>

    <div class="page-card">
      <h3 class="page-card__title">联系方式</h3>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px" style="max-width: 460px">
        <el-form-item label="昵称" prop="nickname">
          <el-input v-model="form.nickname" placeholder="客服称呼您的名字" clearable />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" clearable />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="form.phone" clearable />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="page-card">
      <h3 class="page-card__title">修改密码</h3>
      <el-form
        ref="passwordRef"
        :model="passwordForm"
        :rules="passwordRules"
        label-width="90px"
        style="max-width: 460px"
      >
        <el-form-item label="原密码" prop="old_password">
          <el-input v-model="passwordForm.old_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码" prop="new_password">
          <el-input v-model="passwordForm.new_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirm">
          <el-input v-model="passwordForm.confirm" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="changing" @click="handleChangePassword">
            修改密码
          </el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>
