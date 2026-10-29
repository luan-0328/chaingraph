<script setup lang="ts">
/**
 * 我的订单：列表 + 履约状态筛选 + 按状态显示可用操作。
 *
 * 操作按钮的显示条件都以**后端状态机**为准（Service 里怎么校验，这里就怎么显示）：
 *   支付/取消只对未支付订单开放（后端 cancel_order 明确拒绝已支付订单），
 *   确认收货只对已发货/已签收开放，申请售后只对已支付且没有进行中售后开放。
 *   按钮少显示一点，比点下去报 409 的体验好得多。
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import OrderStatusTags from '@/components/order/OrderStatusTags.vue'
import { cancelOrder, confirmOrder, listMyOrders, payOrder } from '@/api/orders'
import type { OrderSummary } from '@/types'
import { FULFILLMENT_STATUS_OPTIONS } from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const router = useRouter()

const PAGE_SIZE = 10

const loading = ref(false)
const orders = ref<OrderSummary[]>([])
const total = ref(0)
const page = ref(1)
const fulfillmentFilter = ref<string>('')

/** 正在执行操作的订单 + 操作名：只让被点的那颗按钮转圈并禁止重复点击 */
const actingId = ref<number | null>(null)
const actingAction = ref('')

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listMyOrders({
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
      fulfillment_status: fulfillmentFilter.value || undefined,
    })
    orders.value = result.items
    total.value = result.total
  } catch {
    orders.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleFilterChange(): void {
  page.value = 1
  void load()
}

function handlePageChange(next: number): void {
  page.value = next
  void load()
}

function goDetail(order: OrderSummary): void {
  void router.push({ name: 'order-detail', params: { id: order.order_id } })
}

// ---------------- 按钮可见性 ----------------
function canPay(order: OrderSummary): boolean {
  return order.payment_status === 'UNPAID' && order.fulfillment_status !== 'CANCELLED'
}

function canCancel(order: OrderSummary): boolean {
  // 后端只允许"未支付"订单取消（已支付必须走售后），所以这里额外要求未支付
  return order.payment_status === 'UNPAID' && order.fulfillment_status === 'PENDING'
}

function canConfirm(order: OrderSummary): boolean {
  return order.fulfillment_status === 'SHIPPED' || order.fulfillment_status === 'DELIVERED'
}

function canApplyAfterSale(order: OrderSummary): boolean {
  return order.payment_status === 'PAID' && order.after_sale_status === 'NONE'
}

function hasLogistics(order: OrderSummary): boolean {
  return (
    order.fulfillment_status === 'SHIPPED' ||
    order.fulfillment_status === 'DELIVERED' ||
    order.fulfillment_status === 'COMPLETED'
  )
}

// ---------------- 操作 ----------------
async function run(order: OrderSummary, action: string, task: () => Promise<unknown>): Promise<void> {
  actingId.value = order.order_id
  actingAction.value = action
  try {
    await task()
    await load()
  } catch {
    // 幂等/状态冲突（重复支付、已取消等）由拦截器提示，这里只避免崩溃
  } finally {
    actingId.value = null
    actingAction.value = ''
  }
}

async function handlePay(order: OrderSummary): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认支付订单 ${order.order_no}（${formatAmount(order.total_amount)}）？`, '模拟支付', {
      confirmButtonText: '确认支付',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch {
    return
  }
  await run(order, 'pay', async () => {
    await payOrder(order.order_id)
    ElMessage.success('支付成功')
  })
}

async function handleCancel(order: OrderSummary): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认取消订单 ${order.order_no}？取消后库存会回补。`, '取消订单', {
      confirmButtonText: '确认取消',
      cancelButtonText: '再想想',
      type: 'warning',
    })
  } catch {
    return
  }
  await run(order, 'cancel', async () => {
    await cancelOrder(order.order_id)
    ElMessage.success('订单已取消')
  })
}

async function handleConfirm(order: OrderSummary): Promise<void> {
  try {
    await ElMessageBox.confirm('确认已经收到商品？确认后订单完成。', '确认收货', {
      confirmButtonText: '确认收货',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch {
    return
  }
  await run(order, 'confirm', async () => {
    await confirmOrder(order.order_id)
    ElMessage.success('已确认收货')
  })
}

/**
 * 查看物流：列表 DTO（OrderSummary）里没有物流字段，
 * 所以跳到订单详情看物流区块，而不是为一行数据再发一次请求。
 */
function viewLogistics(order: OrderSummary): void {
  void router.push({ name: 'order-detail', params: { id: order.order_id } })
}

/** 申请售后：入口在详情页的弹窗里（要填类型/原因/金额），这里带 query 让详情页自动弹出 */
function applyAfterSale(order: OrderSummary): void {
  void router.push({
    name: 'order-detail',
    params: { id: order.order_id },
    query: { afterSale: '1' },
  })
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">我的订单</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-select
          v-model="fulfillmentFilter"
          placeholder="全部履约状态"
          clearable
          style="width: 180px"
          @change="handleFilterChange"
        >
          <el-option
            v-for="option in FULFILLMENT_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-button @click="load">刷新</el-button>
      </div>

      <el-table v-loading="loading" :data="orders" row-key="order_id">
        <el-table-column label="订单号" min-width="180">
          <template #default="{ row }">
            <el-link type="primary" :underline="false" @click="goDetail(row)">
              {{ row.order_no }}
            </el-link>
          </template>
        </el-table-column>

        <el-table-column label="订单金额" width="140" align="right">
          <template #default="{ row }">
            <span class="text-price">{{ formatAmount(row.total_amount) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态（支付 / 履约 / 售后）" min-width="260">
          <template #default="{ row }">
            <OrderStatusTags :order="row" />
          </template>
        </el-table-column>

        <el-table-column label="下单时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="canPay(row)"
              type="primary"
              link
              :loading="actingId === row.order_id && actingAction === 'pay'"
              @click="handlePay(row)"
            >
              去支付
            </el-button>

            <el-button
              v-if="canCancel(row)"
              type="danger"
              link
              :loading="actingId === row.order_id && actingAction === 'cancel'"
              @click="handleCancel(row)"
            >
              取消订单
            </el-button>

            <el-button
              v-if="canConfirm(row)"
              type="primary"
              link
              :loading="actingId === row.order_id && actingAction === 'confirm'"
              @click="handleConfirm(row)"
            >
              确认收货
            </el-button>

            <el-button v-if="hasLogistics(row)" link @click="viewLogistics(row)">查看物流</el-button>

            <el-button v-if="canApplyAfterSale(row)" link @click="applyAfterSale(row)">
              申请售后
            </el-button>

            <el-button link @click="goDetail(row)">订单详情</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="还没有订单" :image-size="60">
            <el-button type="primary" @click="router.push({ name: 'product-list' })">
              去逛逛
            </el-button>
          </el-empty>
        </template>
      </el-table>

      <div v-if="total > 0" class="pagination">
        <el-pagination
          layout="total, prev, pager, next"
          :total="total"
          :page-size="PAGE_SIZE"
          :current-page="page"
          background
          @current-change="handlePageChange"
        />
      </div>
    </div>
  </div>
</template>
