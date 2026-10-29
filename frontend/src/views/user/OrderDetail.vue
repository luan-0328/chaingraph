<script setup lang="ts">
/**
 * 订单详情：订单信息 + 明细 + 支付记录 + 物流进度 + 操作（支付/取消/确认收货/申请售后）。
 *
 * 一个订单有三个独立状态维度（支付/履约/售后），页面最上方用 OrderStatusTags 一起展示。
 * 申请售后走弹窗表单：类型/原因/金额都要用户确认，金额默认订单全额但可以改。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import type { AxiosError } from 'axios'

import OrderItemsTable from '@/components/order/OrderItemsTable.vue'
import OrderStatusTags from '@/components/order/OrderStatusTags.vue'
import { createAfterSale } from '@/api/afterSales'
import { cancelOrder, confirmOrder, getOrder, payOrder } from '@/api/orders'
import type { AfterSaleType, ApiError, OrderDetail } from '@/types'
import { AFTER_SALE_TYPE_OPTIONS } from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const orderId = computed(() => Number(route.params.id))

const loading = ref(true)
const detail = ref<OrderDetail | null>(null)
const actingAction = ref('')
const acting = computed(() => actingAction.value !== '')

/**
 * 支付流水状态文案。
 * 为什么不用 dict.ts 里的 REFUND_STATUS_MAP？
 *   它的枚举值虽然同样是 PENDING/SUCCESS/FAILED，但文案是"退款成功"，
 *   用在支付记录上会误导用户；dict.ts 没有支付字典，所以在局部定义这一份。
 */
const PAYMENT_STATUS_LABEL: Record<string, string> = {
  PENDING: '处理中',
  SUCCESS: '支付成功',
  FAILED: '支付失败',
}

async function load(): Promise<void> {
  loading.value = true
  try {
    detail.value = await getOrder(orderId.value)
  } catch {
    // 404/403 已由拦截器提示
    detail.value = null
  } finally {
    loading.value = false
  }
}

// ---------------- 操作 ----------------
async function handlePay(): Promise<void> {
  if (!detail.value) return
  if (acting.value) return // 防止重复点击（后端也有幂等保护，前端别制造并发）
  try {
    await ElMessageBox.confirm(`确认支付 ${formatAmount(detail.value.total_amount)}？`, '模拟支付', {
      confirmButtonText: '确认支付',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch {
    return
  }
  actingAction.value = 'pay'
  try {
    await payOrder(orderId.value)
    ElMessage.success('支付成功')
    await load()
  } catch {
    /* 已支付/已取消等，拦截器已提示 */
  } finally {
    actingAction.value = ''
  }
}

async function handleCancel(): Promise<void> {
  if (!detail.value || acting.value) return
  try {
    await ElMessageBox.confirm('确认取消该订单？取消后库存会回补。', '取消订单', {
      confirmButtonText: '确认取消',
      cancelButtonText: '再想想',
      type: 'warning',
    })
  } catch {
    return
  }
  actingAction.value = 'cancel'
  try {
    await cancelOrder(orderId.value)
    ElMessage.success('订单已取消')
    await load()
  } catch {
    /* 同上 */
  } finally {
    actingAction.value = ''
  }
}

async function handleConfirm(): Promise<void> {
  if (acting.value) return
  try {
    await ElMessageBox.confirm('确认已经收到商品？确认后订单完成。', '确认收货', {
      confirmButtonText: '确认收货',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch {
    return
  }
  actingAction.value = 'confirm'
  try {
    await confirmOrder(orderId.value)
    ElMessage.success('已确认收货')
    await load()
  } catch {
    /* 同上 */
  } finally {
    actingAction.value = ''
  }
}

// ---------------- 申请售后 ----------------
const saleDialogVisible = ref(false)
const saleFormRef = ref<FormInstance | null>(null)
const submittingSale = ref(false)
/** 弹窗内的友好提示（例如"该订单已有进行中的售后"），不用再弹一个重复的 toast */
const saleDialogError = ref('')

const saleForm = reactive<{ type: AfterSaleType; reason: string; amount: number }>({
  type: 'REFUND',
  reason: '',
  amount: 0,
})

const saleRules: FormRules = {
  type: [{ required: true, message: '请选择售后类型', trigger: 'change' }],
  reason: [
    { required: true, message: '请填写申请原因', trigger: 'blur' },
    { min: 2, max: 500, message: '原因长度为 2~500 字', trigger: 'blur' },
  ],
}

const canPay = computed(
  () => detail.value?.payment_status === 'UNPAID' && detail.value?.fulfillment_status !== 'CANCELLED',
)
const canCancel = computed(
  () => detail.value?.payment_status === 'UNPAID' && detail.value?.fulfillment_status === 'PENDING',
)
const canConfirm = computed(
  () =>
    detail.value?.fulfillment_status === 'SHIPPED' || detail.value?.fulfillment_status === 'DELIVERED',
)
const canApplyAfterSale = computed(
  () => detail.value?.payment_status === 'PAID' && detail.value?.after_sale_status === 'NONE',
)
const cancelled = computed(() => detail.value?.fulfillment_status === 'CANCELLED')

/** 物流步骤：待发货 → 已发货 → 已签收 */
const logisticStep = computed(() => {
  const status = detail.value?.fulfillment_status
  if (status === 'SHIPPED') return 1
  if (status === 'DELIVERED' || status === 'COMPLETED') return 2
  return 0
})

async function openSaleDialog(): Promise<void> {
  saleDialogError.value = ''
  saleForm.type = 'REFUND'
  saleForm.reason = ''
  saleForm.amount = detail.value?.total_amount ?? 0
  saleDialogVisible.value = true
  // 等弹窗渲染出来再清校验红字（el-dialog 的内容是懒挂载的，ref 此刻才存在）
  await nextTick()
  saleFormRef.value?.clearValidate()
}

async function submitSale(): Promise<void> {
  if (!detail.value) return
  const form = saleFormRef.value
  if (form) {
    const valid = await form.validate().catch(() => false)
    if (!valid) return
  }
  submittingSale.value = true
  try {
    await createAfterSale({
      order_id: detail.value.order_id,
      type: saleForm.type,
      reason: saleForm.reason.trim(),
      amount: saleForm.amount,
    })
    ElMessage.success('售后申请已提交，可在「我的售后」跟进')
    saleDialogVisible.value = false
    await router.push({ name: 'after-sale-list' })
  } catch (error) {
    const code = (error as AxiosError<ApiError>).response?.data?.code
    if (code === 'DUPLICATE_AFTER_SALE') {
      // 后端 409：该订单已有进行中的售后。
      // 拦截器已经弹过一次 error toast，这里只在弹窗里给出"下一步去哪"的指引，不重复弹窗
      saleDialogError.value = '该订单已有进行中的售后，请到「我的售后」页查看进度。'
    }
    // 其余错误（未支付、金额超限、原因长度）保持弹窗打开让用户修改
  } finally {
    submittingSale.value = false
  }
}

function goAfterSaleList(): void {
  saleDialogVisible.value = false
  void router.push({ name: 'after-sale-list' })
}

/** 售后处理不了时最常见的诉求是"找客服"，带上订单号直接进提交工单页 */
function goCreateTicket(): void {
  void router.push({ name: 'ticket-create', query: { orderId: String(orderId.value) } })
}

onMounted(async () => {
  await load()
  // 从订单列表点"申请售后"会带 ?afterSale=1，条件满足时直接把弹窗打开
  if (route.query.afterSale === '1' && canApplyAfterSale.value) {
    openSaleDialog()
  }
})

// 同一组件内切换订单 id 时重新加载
watch(orderId, () => {
  void load()
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2 class="page-header__title">订单详情</h2>
      <div class="detail__actions">
        <el-button text @click="router.push({ name: 'order-list' })">返回订单列表</el-button>
        <el-button text @click="goCreateTicket">联系客服</el-button>
      </div>
    </div>

    <div v-if="!loading && !detail" class="page-card">
      <el-result icon="warning" title="订单不存在或无权查看">
        <template #extra>
          <el-button type="primary" @click="router.push({ name: 'order-list' })">
            返回订单列表
          </el-button>
        </template>
      </el-result>
    </div>

    <template v-if="detail">
      <div class="page-card">
        <div class="detail__head">
          <div>
            <span class="text-secondary">订单号</span>
            <span class="detail__no">{{ detail.order_no }}</span>
          </div>
          <OrderStatusTags :order="detail" />
        </div>

        <el-descriptions :column="3" border size="small" class="detail__meta">
          <el-descriptions-item label="订单金额">
            <span class="text-price">{{ formatAmount(detail.total_amount) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="下单时间">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatDateTime(detail.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="detail__buttons">
          <el-button v-if="canPay" type="primary" :loading="actingAction === 'pay'" @click="handlePay">
            去支付
          </el-button>
          <el-button
            v-if="canCancel"
            type="danger"
            :loading="actingAction === 'cancel'"
            @click="handleCancel"
          >
            取消订单
          </el-button>
          <el-button
            v-if="canConfirm"
            type="primary"
            :loading="actingAction === 'confirm'"
            @click="handleConfirm"
          >
            确认收货
          </el-button>
          <el-button v-if="canApplyAfterSale" type="warning" @click="openSaleDialog">
            申请售后
          </el-button>
          <el-button type="primary" plain @click="goCreateTicket">联系客服</el-button>

          <span v-if="detail.after_sale_status === 'APPLYING'" class="text-secondary">
            该订单有进行中的售后，可在「我的售后」查看进度
          </span>
        </div>
      </div>

      <div class="page-card">
        <h3 class="page-card__title">商品明细</h3>
        <OrderItemsTable :items="detail.items" />
      </div>

      <div class="page-card">
        <h3 class="page-card__title">支付记录</h3>
        <el-table :data="detail.payments" border size="small">
          <el-table-column prop="payment_no" label="支付流水号" min-width="200" show-overflow-tooltip />
          <el-table-column label="金额" width="140" align="right">
            <template #default="{ row }">
              <span class="text-price">{{ formatAmount(row.amount) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120" align="center">
            <template #default="{ row }">
              <el-tag :type="row.status === 'SUCCESS' ? 'success' : 'info'" size="small" disable-transitions>
                {{ PAYMENT_STATUS_LABEL[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="支付时间" width="180">
            <template #default="{ row }">{{ formatDateTime(row.paid_at) }}</template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无支付记录" :image-size="60" />
          </template>
        </el-table>
      </div>

      <div class="page-card">
        <h3 class="page-card__title">物流信息</h3>

        <el-alert
          v-if="cancelled"
          type="info"
          show-icon
          :closable="false"
          title="订单已取消，没有物流信息"
        />

        <template v-else>
          <el-steps :active="logisticStep" align-center finish-status="success" class="detail__steps">
            <el-step title="待发货" description="商家备货中" />
            <el-step title="已发货" description="包裹在途中" />
            <el-step title="已签收" description="确认收货后完成" />
          </el-steps>

          <el-descriptions v-if="detail.logistics" :column="2" border size="small">
            <el-descriptions-item label="运单号">
              {{ detail.logistics.tracking_no }}
            </el-descriptions-item>
            <el-descriptions-item label="承运商">
              {{ detail.logistics.company || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="最新动态">
              {{ detail.logistics.latest_message || '暂无更新' }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatDateTime(detail.logistics.last_update_time) }}
            </el-descriptions-item>
          </el-descriptions>

          <el-empty v-else description="还没有物流信息（商家发货后可查看）" :image-size="60" />
        </template>
      </div>

      <!-- 用户端的订单详情不展示 Agent 结果卡：Agent 结果属于工单语境，这里保持页面聚焦在"订单本身"。 -->

      <el-dialog v-model="saleDialogVisible" title="申请售后" width="520px">
        <el-alert
          v-if="saleDialogError"
          class="sale-dialog__alert"
          type="warning"
          show-icon
          :closable="false"
          :title="saleDialogError"
        >
          <el-button type="primary" link @click="goAfterSaleList">去「我的售后」查看</el-button>
        </el-alert>

        <el-form ref="saleFormRef" :model="saleForm" :rules="saleRules" label-width="90px">
          <el-form-item label="售后类型" prop="type">
            <el-select v-model="saleForm.type" style="width: 100%">
              <el-option
                v-for="option in AFTER_SALE_TYPE_OPTIONS.filter(item => item.value === 'REFUND')"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="申请原因" prop="reason">
            <el-input
              v-model="saleForm.reason"
              type="textarea"
              :rows="3"
              maxlength="500"
              show-word-limit
              placeholder="请说明遇到的问题，例如：不想要了 / 商品有破损"
            />
          </el-form-item>

          <el-form-item label="退款金额" prop="amount">
            <el-input-number
              v-model="saleForm.amount"
              :min="0.01"
              :max="detail.total_amount"
              :precision="2"
              :step="1"
            />
            <span class="text-secondary sale-dialog__hint">
              默认为订单全额 {{ formatAmount(detail.total_amount) }}，可按需修改
            </span>
          </el-form-item>
        </el-form>

        <template #footer>
          <el-button @click="saleDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="submittingSale" @click="submitSale">提交申请</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
.detail__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.detail__no {
  margin-left: 8px;
  font-weight: 600;
}

.detail__meta {
  margin-bottom: 16px;
}

.detail__buttons {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.detail__actions {
  display: flex;
  gap: 8px;
}

.detail__steps {
  margin-bottom: 20px;
}

.sale-dialog__alert {
  margin-bottom: 12px;
}

.sale-dialog__hint {
  margin-left: 12px;
  font-size: 12px;
}
</style>
