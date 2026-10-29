<script setup lang="ts">
/**
 * 创建工单（前端文档第十八、十九节）。
 *
 * 字段严格按后端 DTO（app/schemas/ticket.py::TicketCreateRequest）：
 *   content  —— 必填，工单内容
 *   type     —— 可选，用户申报的类型
 *
 * 注意：**请求体里没有 user_id**。用户身份由后端从 JWT 里取，
 *      前端不让用户填、也填不了（前端文档第十一节）。
 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { createTicket } from '@/api/ticket'
import type { TicketType } from '@/types/ticket'
import { TICKET_TYPE_OPTIONS } from '@/utils/dict'

const router = useRouter()

const formRef = ref<FormInstance>()
const submitting = ref(false)

const form = reactive<{ content: string; type: TicketType | '' }>({
  content: '',
  type: '',
})

const rules: FormRules<typeof form> = {
  content: [
    { required: true, message: '请填写工单内容', trigger: 'blur' },
    { min: 5, message: '请至少输入 5 个字，方便 Agent 判断问题', trigger: 'blur' },
  ],
}

async function handleSubmit(): Promise<void> {
  if (!formRef.value) return

  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const created = await createTicket({
      content: form.content.trim(),
      type: form.type || null,
    })
    ElMessage.success('工单创建成功')
    await router.push({ name: 'ticket-detail', params: { id: created.ticket_id } })
  } catch {
    // 拦截器已经提示过错误
  } finally {
    submitting.value = false
  }
}

function handleCancel(): void {
  void router.push({ name: 'ticket-list' })
}
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">创建工单</h2>
    </div>

    <div class="page-card">
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-width="90px"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="工单内容" prop="content">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="6"
            maxlength="5000"
            show-word-limit
            placeholder="请详细描述遇到的问题，例如：我的订单10001还没有发货，我想退款"
          />
        </el-form-item>

        <el-form-item label="工单类型" prop="type">
          <el-select
            v-model="form.type"
            placeholder="请选择（可不填）"
            clearable
            style="width: 220px"
          >
            <el-option
              v-for="option in TICKET_TYPE_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <span class="form-hint">不选也可以，Agent 处理完后会按实际情况自动归类</span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="handleSubmit">
            {{ submitting ? '提交中…' : '提交工单' }}
          </el-button>
          <el-button :disabled="submitting" @click="handleCancel">取消</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.form-hint {
  margin-left: 12px;
  color: var(--text-secondary);
  font-size: 12px;
}
</style>
