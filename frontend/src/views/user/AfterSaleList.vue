<script setup lang="ts">
/**
 * 我的售后：售后单列表 + 申请退款 / 撤销 + 展开查看退款流水。
 *
 * 本页最关键的一点是**区分申请退款的两种结局**（v2.md §16 的核心语义）：
 *   executed = true    → 当场自动退款成功，钱已原路退回；
 *   need_human = true → 规则不允许自动退（已发货 / 超自动上限），转人工审核，
 *                       此时**没有产生退款流水**，不能提示"退款成功"。
 * 两者提示文案完全不同，混在一起就是骗用户。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { applyRefund, cancelAfterSale, listMyAfterSales } from '@/api/afterSales'
import type { AfterSaleInfo } from '@/types'
import {
  AFTER_SALE_STATUS_MAP,
  AFTER_SALE_TYPE_MAP,
  REFUND_STATUS_MAP,
} from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'
import StatusTag from '@/components/common/StatusTag.vue'

const PAGE_SIZE = 10

const loading = ref(false)
const afterSales = ref<AfterSaleInfo[]>([])
const total = ref(0)
const page = ref(1)
/** 正在操作的售后单 id（只让这一行转圈） */
const actingId = ref<number | null>(null)

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listMyAfterSales({ limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE })
    afterSales.value = result.items
    total.value = result.total
  } catch {
    afterSales.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handlePageChange(next: number): void {
  page.value = next
  void load()
}

/** 只有"申请中"能发起退款；处理中/已通过的再点也没意义（后端也会拒绝） */
function canRefund(row: AfterSaleInfo): boolean {
  return row.status === 'APPLYING'
}

/** 后端撤销允许 APPLYING 和 PROCESSING（已完成的不能撤），这里与之一致 */
function canCancel(row: AfterSaleInfo): boolean {
  return row.status === 'APPLYING' || row.status === 'PROCESSING'
}

async function handleRefund(row: AfterSaleInfo): Promise<void> {
  if (actingId.value !== null) return
  try {
    await ElMessageBox.confirm(
      `确认为售后单 ${row.after_sale_no} 申请退款 ${formatAmount(row.amount)}？`,
      '申请退款',
      { confirmButtonText: '申请退款', cancelButtonText: '取消', type: 'info' },
    )
  } catch {
    return
  }

  actingId.value = row.after_sale_id
  try {
    const result = await applyRefund(row.after_sale_id)

    if (result.executed) {
      // 自动退款成功：后端已写入成功的退款流水
      ElMessage.success('退款已成功，款项原路退回')
    } else if (result.need_human) {
      // 转人工审核：reason 是后端规则判定说明（人话中文），直接展示给用户
      await ElMessageBox.alert(
        `${result.reason}；可在「我的工单」里查看人工审核进度。`,
        '已转人工审核',
        { confirmButtonText: '知道了', type: 'warning' },
      )
    } else {
      // 理论上不会发生（executed=false 且 need_human=false），但不能静默
      ElMessage.info(result.reason || '退款申请已受理，请稍后查看')
    }
  } catch {
    // 重复退款（DUPLICATE_REFUND）、已退款等，拦截器已提示
  } finally {
    actingId.value = null
    // 无论哪种结局都以服务端为准重新拉一次（need_human 时售后单状态也会变化）
    await load()
  }
}

async function handleCancel(row: AfterSaleInfo): Promise<void> {
  if (actingId.value !== null) return
  try {
    await ElMessageBox.confirm(`确认撤销售后单 ${row.after_sale_no}？`, '撤销售后', {
      confirmButtonText: '确认撤销',
      cancelButtonText: '再想想',
      type: 'warning',
    })
  } catch {
    return
  }

  actingId.value = row.after_sale_id
  try {
    await cancelAfterSale(row.after_sale_id)
    ElMessage.success('已撤销申请')
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    actingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">我的售后</h2>
      <el-button text @click="load">刷新</el-button>
    </div>

    <div class="page-card">
      <el-table v-loading="loading" :data="afterSales" row-key="after_sale_id">
        <!-- 展开行显示退款流水：退款是"资金流水"，一次售后可能有多条（重试），所以放在明细里 -->
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="refund-detail">
              <div class="refund-detail__title">退款流水</div>
              <el-table :data="row.refunds" size="small" border>
                <el-table-column prop="refund_no" label="退款单号" min-width="200" show-overflow-tooltip />
                <el-table-column label="金额" width="120" align="right">
                  <template #default="{ row: refund }">
                    <span class="text-price">{{ formatAmount(refund.amount) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="120" align="center">
                  <template #default="{ row: refund }">
                    <StatusTag :value="refund.status" :map="REFUND_STATUS_MAP" />
                  </template>
                </el-table-column>
                <el-table-column label="发起时间" width="170">
                  <template #default="{ row: refund }">{{ formatDateTime(refund.created_at) }}</template>
                </el-table-column>
                <el-table-column label="完成时间" width="170">
                  <template #default="{ row: refund }">
                    {{ formatDateTime(refund.completed_at) }}
                  </template>
                </el-table-column>
                <template #empty>
                  <el-empty description="暂无退款流水" :image-size="50" />
                </template>
              </el-table>

              <div class="refund-detail__reason">
                <span class="text-secondary">申请原因：</span>
                <span class="pre-wrap">{{ row.reason || '—' }}</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="after_sale_no" label="售后单号" min-width="180" />

        <el-table-column label="类型" width="120" align="center">
          <template #default="{ row }">
            <StatusTag :value="row.type" :map="AFTER_SALE_TYPE_MAP" />
          </template>
        </el-table-column>

        <el-table-column label="金额" width="130" align="right">
          <template #default="{ row }">
            <span class="text-price">{{ formatAmount(row.amount) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="120" align="center">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="AFTER_SALE_STATUS_MAP" />
          </template>
        </el-table-column>

        <el-table-column label="申请时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="canRefund(row)"
              type="primary"
              link
              :loading="actingId === row.after_sale_id"
              @click="handleRefund(row)"
            >
              申请退款
            </el-button>
            <el-button
              v-if="canCancel(row)"
              type="danger"
              link
              :disabled="actingId === row.after_sale_id"
              @click="handleCancel(row)"
            >
              撤销
            </el-button>
            <span v-if="!canRefund(row) && !canCancel(row)" class="text-secondary">—</span>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="还没有售后记录" :image-size="60">
            <el-button type="primary" @click="$router.push({ name: 'order-list' })">
              去看看我的订单
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

<style scoped>
.refund-detail {
  padding: 8px 16px 12px;
}

.refund-detail__title {
  font-weight: 600;
  margin-bottom: 8px;
}

.refund-detail__reason {
  margin-top: 10px;
  font-size: 13px;
}
</style>
