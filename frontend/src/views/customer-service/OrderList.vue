<script setup lang="ts">
/**
 * 客服订单查询（v2.md §23.3、§45）。
 *
 * 这是"查单台"：客服边接电话边按订单号搜，看到金额、支付、履约、物流就能回答用户。
 * 因此发货/运输/签收这些"模拟履约"的动作也放在这里 —— 演示环境要能一条龙走完，
 * 但按钮只在状态允许时出现，避免客服点出一个必然失败的请求。
 *
 * 详情用抽屉而不是跳页：查单大多是"看一眼就回来"，抽屉不会丢掉列表的筛选与页码。
 */
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'

import { deliverOrder, getOrder, listOrders, shipOrder, transitOrder } from '@/api/customerService'
import OrderItemsTable from '@/components/order/OrderItemsTable.vue'
import OrderStatusTags from '@/components/order/OrderStatusTags.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import type { OrderDetail, OrderSummary } from '@/types'
import {
  FULFILLMENT_STATUS_MAP,
  FULFILLMENT_STATUS_OPTIONS,
  ORDER_AFTER_SALE_STATUS_MAP,
  PAYMENT_STATUS_MAP,
} from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const route = useRoute()

const loading = ref(false)
const rows = ref<OrderSummary[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

/** 正在操作的订单 id：按钮只在这一行转圈 */
const actingId = ref<number | null>(null)

const filters = reactive({
  // 工单详情点"查看订单"会带 ?keyword=订单号 过来，这里读一次当初始条件
  keyword: typeof route.query.keyword === 'string' ? route.query.keyword : '',
  payment_status: '',
  fulfillment_status: '',
})

/**
 * dict.ts 只导出了 PAYMENT_STATUS_MAP（没有对应的 *_OPTIONS 数组），
 * 所以从字典本身派生下拉项 —— 中文文案仍然只有字典一个来源，不在这里另写一套映射。
 */
const paymentOptions = Object.entries(PAYMENT_STATUS_MAP).map(([value, item]) => ({
  value,
  label: item.label,
}))

const drawerVisible = ref(false)
const detailLoading = ref(false)
const current = ref<OrderDetail | null>(null)

/** 可发货：后端要求"已支付"且履约状态还没走到发货之后（PENDING / PROCESSING） */
function canShip(row: OrderSummary): boolean {
  return (
    row.payment_status === 'PAID' &&
    (row.fulfillment_status === 'PENDING' || row.fulfillment_status === 'PROCESSING')
  )
}

/** 运输/签收都要求已有物流记录，即订单已发货 */
function canLogistics(row: OrderSummary): boolean {
  return row.fulfillment_status === 'SHIPPED'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listOrders({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      keyword: filters.keyword.trim() || undefined,
      payment_status: filters.payment_status || undefined,
      fulfillment_status: filters.fulfillment_status || undefined,
    })
    rows.value = result.items
    total.value = result.total
  } catch {
    // 错误提示由 request.ts 拦截器统一处理
  } finally {
    loading.value = false
  }
}

function search(): void {
  page.value = 1
  void load()
}

function reset(): void {
  filters.keyword = ''
  filters.payment_status = ''
  filters.fulfillment_status = ''
  search()
}

function onSizeChange(size: number): void {
  pageSize.value = size
  page.value = 1
  void load()
}

async function loadDetail(orderId: number): Promise<void> {
  detailLoading.value = true
  try {
    current.value = await getOrder(orderId)
  } catch {
    current.value = null
  } finally {
    detailLoading.value = false
  }
}

function openDetail(row: OrderSummary): void {
  drawerVisible.value = true
  current.value = null
  void loadDetail(row.order_id)
}

/** 履约动作完成后：列表要刷新（状态变了），抽屉开着的话也要刷新 */
async function refreshAfterAction(orderId: number): Promise<void> {
  await load()
  if (drawerVisible.value && current.value?.order_id === orderId) {
    await loadDetail(orderId)
  }
}

async function handleShip(row: OrderSummary): Promise<void> {
  actingId.value = row.order_id
  try {
    await shipOrder(row.order_id)
    ElMessage.success(`订单 ${row.order_no} 已发货`)
    await refreshAfterAction(row.order_id)
  } catch {
    // 例如未支付 / 已发货（后端 409）
  } finally {
    actingId.value = null
  }
}

async function handleTransit(row: OrderSummary): Promise<void> {
  actingId.value = row.order_id
  try {
    // message 不传就走后端默认文案"快件已到达转运中心"
    await transitOrder(row.order_id)
    ElMessage.success('已模拟一次运输轨迹更新')
    await refreshAfterAction(row.order_id)
  } catch {
    // 例如还没有物流记录
  } finally {
    actingId.value = null
  }
}

async function handleDeliver(row: OrderSummary): Promise<void> {
  actingId.value = row.order_id
  try {
    await deliverOrder(row.order_id)
    ElMessage.success('已模拟签收')
    await refreshAfterAction(row.order_id)
  } catch {
    // 例如还没有物流记录
  } finally {
    actingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">订单查询</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="filters.keyword"
          placeholder="订单号"
          clearable
          style="width: 220px"
          @keyup.enter="search"
        />

        <el-select
          v-model="filters.payment_status"
          placeholder="支付状态"
          clearable
          style="width: 140px"
        >
          <el-option
            v-for="option in paymentOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-select
          v-model="filters.fulfillment_status"
          placeholder="履约状态"
          clearable
          style="width: 140px"
        >
          <el-option
            v-for="option in FULFILLMENT_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="rows" size="default">
        <el-table-column prop="order_no" label="订单号" width="190" />
        <el-table-column label="金额" width="110">
          <template #default="{ row }">
            <span class="text-price">{{ formatAmount(row.total_amount) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="支付状态" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.payment_status" :map="PAYMENT_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="履约状态" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.fulfillment_status" :map="FULFILLMENT_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="售后状态" width="120">
          <template #default="{ row }">
            <StatusTag :value="row.after_sale_status" :map="ORDER_AFTER_SALE_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="下单时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">详情</el-button>
            <el-button
              v-if="canShip(row)"
              link
              type="primary"
              :loading="actingId === row.order_id"
              @click="handleShip(row)"
            >
              确认发货
            </el-button>
            <template v-if="canLogistics(row)">
              <el-button
                link
                type="primary"
                :loading="actingId === row.order_id"
                @click="handleTransit(row)"
              >
                模拟运输
              </el-button>
              <el-button
                link
                type="primary"
                :loading="actingId === row.order_id"
                @click="handleDeliver(row)"
              >
                模拟签收
              </el-button>
            </template>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的订单" />
        </template>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="load"
          @size-change="onSizeChange"
        />
      </div>
    </div>

    <!-- 订单详情抽屉：明细 / 支付 / 物流 -->
    <el-drawer v-model="drawerVisible" title="订单详情" size="640px">
      <div v-loading="detailLoading">
        <template v-if="current">
          <el-descriptions :column="2" size="small" border>
            <el-descriptions-item label="订单号">{{ current.order_no }}</el-descriptions-item>
            <el-descriptions-item label="订单金额">
              <span class="text-price">{{ formatAmount(current.total_amount) }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              <OrderStatusTags :order="current" />
            </el-descriptions-item>
            <el-descriptions-item label="下单时间">
              {{ formatDateTime(current.created_at) }}
            </el-descriptions-item>
          </el-descriptions>

          <h4 class="drawer__title">商品明细</h4>
          <OrderItemsTable :items="current.items" />

          <h4 class="drawer__title">支付记录</h4>
          <el-table v-if="current.payments.length" :data="current.payments" size="small">
            <el-table-column prop="payment_no" label="支付单号" min-width="180" />
            <el-table-column label="金额" width="100">
              <template #default="{ row }">
                <span class="text-price">{{ formatAmount(row.amount) }}</span>
              </template>
            </el-table-column>
            <!-- 支付记录的 status 是 PENDING/SUCCESS/FAILED，dict.ts 没有这个枚举的字典
                 （REFUND_STATUS_MAP 文案是"退款成功"，语义不对），这里如实展示原始值 -->
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" disable-transitions>{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="支付时间" width="170">
              <template #default="{ row }">{{ formatDateTime(row.paid_at) }}</template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无支付记录" :image-size="60" />
            </template>
          </el-table>
          <el-empty v-else description="暂无支付记录" :image-size="60" />

          <h4 class="drawer__title">物流信息</h4>
          <el-descriptions v-if="current.logistics" :column="1" size="small" border>
            <el-descriptions-item label="运单号">
              {{ current.logistics.tracking_no }}
            </el-descriptions-item>
            <el-descriptions-item label="承运商">
              {{ current.logistics.company ?? '—' }}
            </el-descriptions-item>
            <!-- 物流状态是 PENDING/IN_TRANSIT/DELIVERED，同样没有中文字典，展示原始值 -->
            <el-descriptions-item label="物流状态">
              <el-tag size="small" disable-transitions>{{ current.logistics.status }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="最新动态">
              {{ current.logistics.latest_message ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatDateTime(current.logistics.last_update_time) }}
            </el-descriptions-item>
          </el-descriptions>
          <el-empty v-else description="暂无物流信息（可能还未发货）" :image-size="60" />

          <div class="drawer__actions">
            <el-button
              v-if="canShip(current)"
              type="primary"
              :loading="actingId === current.order_id"
              @click="handleShip(current)"
            >
              确认发货
            </el-button>
            <template v-if="canLogistics(current)">
              <el-button :loading="actingId === current.order_id" @click="handleTransit(current)">
                模拟运输
              </el-button>
              <el-button :loading="actingId === current.order_id" @click="handleDeliver(current)">
                模拟签收
              </el-button>
            </template>
          </div>
        </template>

        <el-empty v-else-if="!detailLoading" description="订单不存在或无权限查看" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.drawer__title {
  margin: 20px 0 8px;
  font-size: 14px;
  font-weight: 600;
}

.drawer__actions {
  display: flex;
  gap: 8px;
  margin-top: 20px;
}
</style>
