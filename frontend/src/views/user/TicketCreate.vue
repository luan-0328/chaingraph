<script setup lang="ts">
/**
 * 提交问题（新建工单）。
 *
 * 表单刻意保持简单：描述（必填）+ 可选关联订单 + 可选问题类型。
 * 关联订单很重要 —— 后端会校验订单归属，Agent 也能直接查到该订单/物流/售后，
 * 所以列表只列**已支付**的订单（未支付订单本身没有可讨论的售后问题）。
 * 支持 ?orderId=xxx 预填（订单详情页"联系客服"会带过来）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { createTicket } from '@/api/tickets'
import { listMyOrders } from '@/api/orders'
import type { OrderSummary } from '@/types'
import { TICKET_TYPE_OPTIONS } from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const ordersLoading = ref(false)
const paidOrders = ref<OrderSummary[]>([])

const form = reactive<{ content: string; order_id: number | null; type: string | null }>({
  content: '',
  order_id: null,
  type: null,
})

const rules: FormRules = {
  content: [
    { required: true, message: '请描述你遇到的问题', trigger: 'blur' },
    { min: 5, max: 2000, message: '描述长度为 5~2000 字', trigger: 'blur' },
  ],
}

/** 订单下拉只展示已支付订单（后端也要求关联订单必须存在且属于自己） */
async function loadPaidOrders(): Promise<void> {
  ordersLoading.value = true
  try {
    const result = await listMyOrders({ limit: 50, offset: 0, payment_status: 'PAID' })
    paidOrders.value = result.items
  } catch {
    paidOrders.value = []
  } finally {
    ordersLoading.value = false
  }
}

function orderLabel(order: OrderSummary): string {
  return `${order.order_no} · ${formatAmount(order.total_amount)} · ${formatDateTime(order.created_at)}`
}

const contentLength = computed(() => form.content.trim().length)

async function handleSubmit(): Promise<void> {
  const instance = formRef.value
  if (instance) {
    const valid = await instance.validate().catch(() => false)
    if (!valid) return
  }
  if (submitting.value) return

  submitting.value = true
  try {
    const ticket = await createTicket({
      content: form.content.trim(),
      order_id: form.order_id,
      type: form.type,
    })
    ElMessage.success('工单已提交')
    // 提交完直接进详情：用户接下来要看的就是 Agent 的处理过程
    await router.push({ name: 'ticket-detail', params: { id: ticket.ticket_id } })
  } catch {
    // 校验类错误（订单不属于自己等）拦截器已提示
  } finally {
    submitting.value = false
  }
}

function goList(): void {
  void router.push({ name: 'ticket-list' })
}

onMounted(async () => {
  await loadPaidOrders()
  const orderId = Number(route.query.orderId)
  if (Number.isFinite(orderId) && orderId > 0) {
    form.order_id = orderId
  }
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">提交问题</h2>
      <el-button text @click="goList">返回我的工单</el-button>
    </div>

    <div class="page-card">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px" class="create-form">
        <el-form-item label="问题描述" prop="content">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="6"
            maxlength="2000"
            show-word-limit
            placeholder="请描述遇到的问题，例如：订单 10002 我要退款 / 快递一直没更新（写清订单号能让 Agent 处理得更准）"
          />
          <div class="text-secondary create-form__hint">
            至少 5 个字（当前 {{ contentLength }} 字）。描述越具体，Agent 越容易一次解决。
          </div>
        </el-form-item>

        <el-form-item label="关联订单">
          <el-select
            v-model="form.order_id"
            v-loading="ordersLoading"
            placeholder="可选：选择相关订单（只列已支付订单）"
            clearable
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="order in paidOrders"
              :key="order.order_id"
              :label="orderLabel(order)"
              :value="order.order_id"
            />
          </el-select>
          <div class="text-secondary create-form__hint">
            关联订单后，Agent 可以直接查这笔订单的支付/物流/售后情况。
          </div>
        </el-form-item>

        <el-form-item label="问题类型">
          <el-select v-model="form.type" placeholder="可选：不选则由 Agent 自动判断" clearable style="width: 100%">
            <el-option
              v-for="option in TICKET_TYPE_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="handleSubmit">提交工单</el-button>
          <el-button @click="goList">取消</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.create-form {
  max-width: 720px;
}

.create-form__hint {
  font-size: 12px;
  line-height: 1.4;
}
</style>
