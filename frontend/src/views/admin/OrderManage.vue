<script setup lang="ts">
/**
 * 订单总览（路由 admin-orders）。
 *
 * 接口选择说明：管理端也有 GET /admin/orders（listAllOrders），但它的返回类型是
 * PageResult<Record<string, unknown>> —— 没有 DTO 约束、也不支持筛选；
 * 所以这里用客服端的 listOrders（GET /customer-service/orders），
 * 它返回结构化的 PageResult<OrderSummary>，字段有类型保障，还能按订单号/支付/履约筛选。
 * （ADMIN 也能访问客服端接口，后端用的是 staff_only。）
 */
import { onMounted, reactive, ref } from 'vue'

import { getOrder, listOrders } from '@/api/customerService'
import StatusTag from '@/components/common/StatusTag.vue'
import type { OrderDetail, OrderSummary } from '@/types'
import {
  FULFILLMENT_STATUS_MAP,
  FULFILLMENT_STATUS_OPTIONS,
  ORDER_AFTER_SALE_STATUS_MAP,
  PAYMENT_STATUS_MAP,
} from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/** 支付状态没有 *_OPTIONS 导出，用字典 map 反推选项，避免页面自己写死枚举 */
const PAYMENT_STATUS_OPTIONS = Object.entries(PAYMENT_STATUS_MAP).map(([value, meta]) => ({
  value,
  label: meta.label,
}))

/**
 * 支付流水状态（PENDING/SUCCESS/FAILED）与字典里的 REFUND_STATUS_MAP 取值相同、
 * 语义不同（那是退款流水），直接复用会显示成"退款成功"，所以这里单独定义。
 */
const PAYMENT_RECORD_MAP: Record<string, { label: string; tag: TagType }> = {
  PENDING: { label: '处理中', tag: 'warning' },
  SUCCESS: { label: '支付成功', tag: 'success' },
  FAILED: { label: '支付失败', tag: 'danger' },
}

/** 物流状态 dict.ts 未收录（管理端只读展示），同样在这里补一份 */
const LOGISTICS_STATUS_MAP: Record<string, { label: string; tag: TagType }> = {
  PENDING: { label: '待揽收', tag: 'warning' },
  IN_TRANSIT: { label: '运输中', tag: 'primary' },
  DELIVERED: { label: '已签收', tag: 'success' },
}

const loading = ref(false)
const orders = ref<OrderSummary[]>([])

const query = reactive<{
  keyword: string
  payment_status: string | undefined
  fulfillment_status: string | undefined
}>({ keyword: '', payment_status: undefined, fulfillment_status: undefined })
const pagination = reactive({ current: 1, limit: 10, total: 0 })

async function loadOrders(): Promise<void> {
  loading.value = true
  try {
    const result = await listOrders({
      limit: pagination.limit,
      offset: (pagination.current - 1) * pagination.limit,
      keyword: query.keyword.trim() || undefined,
      payment_status: query.payment_status || undefined,
      fulfillment_status: query.fulfillment_status || undefined,
    })
    orders.value = result.items
    pagination.total = result.total
  } finally {
    // 错误提示由 request 拦截器统一负责
    loading.value = false
  }
}

function search(): void {
  pagination.current = 1
  void loadOrders()
}

function resetQuery(): void {
  query.keyword = ''
  query.payment_status = undefined
  query.fulfillment_status = undefined
  search()
}

function handlePageChange(page: number): void {
  pagination.current = page
  void loadOrders()
}

function handleSizeChange(size: number): void {
  pagination.limit = size
  pagination.current = 1
  void loadOrders()
}

// ======================================================================
// 明细抽屉：列表只给概要，明细/支付/物流要单独调 getOrder
// ======================================================================
const drawerVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<OrderDetail | null>(null)

async function openDetail(row: OrderSummary): Promise<void> {
  drawerVisible.value = true
  detail.value = null // 先清空，避免看到上一个订单的残留数据
  detailLoading.value = true
  try {
    detail.value = await getOrder(row.order_id)
  } catch {
    // 拦截器已提示；抽屉保持打开，用户可以关掉或重试
    drawerVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

onMounted(loadOrders)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">订单总览</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="按订单号搜索"
          clearable
          style="width: 240px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select
          v-model="query.payment_status"
          placeholder="全部支付状态"
          clearable
          style="width: 160px"
          @change="search"
        >
          <el-option
            v-for="option in PAYMENT_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-select
          v-model="query.fulfillment_status"
          placeholder="全部履约状态"
          clearable
          style="width: 160px"
          @change="search"
        >
          <el-option
            v-for="option in FULFILLMENT_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetQuery">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="orders" border stripe>
        <el-table-column prop="order_no" label="订单号" min-width="180" show-overflow-tooltip />
        <el-table-column prop="order_id" label="订单 ID" width="90" />
        <el-table-column label="订单金额" width="120" align="right">
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
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">查看明细</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的订单" />
        </template>
      </el-table>

      <div class="pagination">
        <el-pagination
          :current-page="pagination.current"
          :page-size="pagination.limit"
          :page-sizes="[10, 20, 50]"
          :total="pagination.total"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </div>

    <el-drawer v-model="drawerVisible" title="订单明细" size="620px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="订单号">{{ detail.order_no }}</el-descriptions-item>
            <el-descriptions-item label="订单 ID">{{ detail.order_id }}</el-descriptions-item>
            <el-descriptions-item label="订单金额">
              <span class="text-price">{{ formatAmount(detail.total_amount) }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="支付状态">
              <StatusTag :value="detail.payment_status" :map="PAYMENT_STATUS_MAP" />
            </el-descriptions-item>
            <el-descriptions-item label="履约状态">
              <StatusTag :value="detail.fulfillment_status" :map="FULFILLMENT_STATUS_MAP" />
            </el-descriptions-item>
            <el-descriptions-item label="售后状态">
              <StatusTag :value="detail.after_sale_status" :map="ORDER_AFTER_SALE_STATUS_MAP" />
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatDateTime(detail.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatDateTime(detail.updated_at) }}
            </el-descriptions-item>
          </el-descriptions>

          <h3 class="page-card__title detail__title">商品明细</h3>
          <el-table :data="detail.items" border size="small">
            <el-table-column prop="product_id" label="商品 ID" width="90" />
            <el-table-column
              prop="product_name"
              label="商品名称"
              min-width="160"
              show-overflow-tooltip
            />
            <el-table-column label="单价" width="100" align="right">
              <template #default="{ row }">{{ formatAmount(row.product_price) }}</template>
            </el-table-column>
            <el-table-column prop="quantity" label="数量" width="70" align="center" />
            <el-table-column label="小计" width="100" align="right">
              <template #default="{ row }">{{ formatAmount(row.subtotal) }}</template>
            </el-table-column>
            <template #empty>
              <el-empty description="无商品明细" :image-size="60" />
            </template>
          </el-table>

          <h3 class="page-card__title detail__title">支付记录</h3>
          <el-table :data="detail.payments" border size="small">
            <el-table-column
              prop="payment_no"
              label="支付流水号"
              min-width="160"
              show-overflow-tooltip
            />
            <el-table-column label="金额" width="100" align="right">
              <template #default="{ row }">{{ formatAmount(row.amount) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <StatusTag :value="row.status" :map="PAYMENT_RECORD_MAP" />
              </template>
            </el-table-column>
            <el-table-column label="支付时间" width="180">
              <template #default="{ row }">{{ formatDateTime(row.paid_at) }}</template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无支付记录" :image-size="60" />
            </template>
          </el-table>

          <h3 class="page-card__title detail__title">物流信息</h3>
          <el-descriptions v-if="detail.logistics" :column="1" border size="small">
            <el-descriptions-item label="运单号">
              {{ detail.logistics.tracking_no }}
            </el-descriptions-item>
            <el-descriptions-item label="快递公司">
              {{ detail.logistics.company ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="物流状态">
              <StatusTag :value="detail.logistics.status" :map="LOGISTICS_STATUS_MAP" />
            </el-descriptions-item>
            <el-descriptions-item label="最新动态">
              {{ detail.logistics.latest_message ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatDateTime(detail.logistics.last_update_time) }}
            </el-descriptions-item>
          </el-descriptions>
          <el-empty v-else description="该订单还没有物流记录" :image-size="60" />
        </template>

        <el-empty v-else-if="!detailLoading" description="没有取到订单明细" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.detail__title {
  margin-top: 20px;
}
</style>
