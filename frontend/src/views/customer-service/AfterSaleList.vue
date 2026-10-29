<script setup lang="ts">
/**
 * 售后审核（v2.md §23.4、§15-§17）。
 *
 * 这是 HITL 的"业务侧"入口：Agent 判不了/不敢自己退的售后单会停在这里等客服拍板。
 *
 * 注意接口口径：approveRefund / rejectRefund 都是**按订单**操作的
 * （POST /customer-service/orders/{order_id}/refund/approve），
 * 因为它要在一个事务里同时改退款流水、售后单和订单状态。
 * 售后单上的 order_id 就是这里的入参，所以列表行直接带上 order_id 就够了。
 */
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { approveRefund, getAfterSale, listAfterSales, rejectRefund } from '@/api/customerService'
import StatusTag from '@/components/common/StatusTag.vue'
import type { AfterSaleInfo } from '@/types'
import {
  AFTER_SALE_STATUS_MAP,
  AFTER_SALE_STATUS_OPTIONS,
  AFTER_SALE_TYPE_MAP,
  REFUND_STATUS_MAP,
} from '@/utils/dict'
import { formatAmount, formatDateTime, formatRefundReview } from '@/utils/format'

const route = useRoute()

const loading = ref(false)
const rows = ref<AfterSaleInfo[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

/** 正在审批的售后单 id */
const actingId = ref<number | null>(null)

const filters = reactive({
  status: '',
  // 工单详情点"查看售后"会带 ?order_id=<订单id> 过来，先按该订单过滤
  order_id: typeof route.query.order_id === 'string' ? route.query.order_id : '',
})

/** 只有"申请中/处理中"的售后单才需要客服审批，其它状态点批准后端也会拒 */
function canApprove(row: AfterSaleInfo): boolean {
  return row.type === 'REFUND' && (row.status === 'APPLYING' || row.status === 'PROCESSING')
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const orderId = Number(filters.order_id)
    const result = await listAfterSales({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      status: filters.status || undefined,
      // 非数字（用户乱改 URL）就当没填，避免给后端发 order_id=NaN
      order_id: Number.isFinite(orderId) && orderId > 0 ? orderId : undefined,
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
  filters.status = ''
  filters.order_id = ''
  search()
}

function onSizeChange(size: number): void {
  pageSize.value = size
  page.value = 1
  void load()
}

/** 展开行只看列表里带的退款流水；点"刷新流水"再单查一次售后详情 */
async function loadRefunds(row: AfterSaleInfo): Promise<void> {
  actingId.value = row.after_sale_id
  try {
    const fresh = await getAfterSale(row.after_sale_id)
    // 就地替换这一行：避免整页刷新把用户展开的行收起来
    const index = rows.value.findIndex((item) => item.after_sale_id === row.after_sale_id)
    if (index >= 0) rows.value[index] = fresh
  } catch {
    // 失败提示由拦截器处理
  } finally {
    actingId.value = null
  }
}

async function handleApprove(row: AfterSaleInfo): Promise<void> {
  try {
    const fresh = await getAfterSale(row.after_sale_id)
    const context = fresh.approval_context
    if (!context) throw new Error('缺少审批上下文，请刷新')
    if (context.expected_run_id && context.run_status !== 'WAITING_HUMAN') {
      ElMessage.warning('关联 Agent 正在处理，请稍后查看工单')
      return
    }
    const { value } = await ElMessageBox.prompt(
      `退款金额 ${formatAmount(fresh.amount)}。${context.expected_run_id ? '批准将提交关联 Agent 方案，等待后台执行。' : '批准后将执行全额退款。'}方案：${context.review_payload?.solution || fresh.reason || '全额退款'}；当前订单与售后：${formatRefundReview(context.order_snapshot)}。`,
      '批准退款',
      {
        confirmButtonText: '批准并退款',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '选填，例如：符合七天无理由规则',
        type: 'warning',
      },
    )
    actingId.value = row.after_sale_id
    // 空字符串会被后端当成有效备注存下来，所以空就传 undefined
    await approveRefund(row.order_id, {
      after_sale_id: fresh.after_sale_id, expected_run_id: context.expected_run_id,
      review_round: context.review_round, order_snapshot: context.order_snapshot,
      comment: value.trim() || undefined,
    })
    ElMessage.success(context.expected_run_id ? '审批已提交，请到关联工单查看执行进度' : '已批准并执行退款')
    await load()
  } catch {
    // 用户取消，或后端拒绝（例如退款已被执行过）
  } finally {
    actingId.value = null
  }
}

async function handleReject(row: AfterSaleInfo): Promise<void> {
  try {
    const fresh = await getAfterSale(row.after_sale_id)
    const context = fresh.approval_context
    if (!context) throw new Error('缺少审批上下文，请刷新')
    const { value } = await ElMessageBox.prompt(
      '请填写驳回原因（会通知用户，务必写清楚）',
      '驳回退款',
      {
        confirmButtonText: '确认驳回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：商品已超出七天无理由期限',
        inputValidator: (text: string) => (text.trim() ? true : '驳回原因不能为空'),
        type: 'warning',
      },
    )
    actingId.value = row.after_sale_id
    await rejectRefund(row.order_id, {
      after_sale_id: fresh.after_sale_id, expected_run_id: context.expected_run_id,
      review_round: context.review_round, order_snapshot: context.order_snapshot,
      comment: value.trim(),
    })
    ElMessage.success(context.expected_run_id ? '驳回已提交，等待后台交接' : '已驳回该退款申请')
    await load()
  } catch {
    // 用户取消或接口失败
  } finally {
    actingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">售后审核</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-select v-model="filters.status" placeholder="全部状态" clearable style="width: 160px">
          <el-option
            v-for="option in AFTER_SALE_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-input
          v-model="filters.order_id"
          placeholder="订单 ID"
          clearable
          style="width: 160px"
          @keyup.enter="search"
        />

        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="rows" size="default">
        <!-- 展开行直接看退款流水：客服审批前最想知道"这笔退到哪一步了" -->
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand">
              <el-table :data="row.refunds" size="small">
                <el-table-column prop="refund_no" label="退款单号" min-width="180" />
                <el-table-column label="金额" width="110">
                  <template #default="scope">
                    <span class="text-price">{{ formatAmount(scope.row.amount) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="110">
                  <template #default="scope">
                    <StatusTag :value="scope.row.status" :map="REFUND_STATUS_MAP" />
                  </template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="160" show-overflow-tooltip />
                <el-table-column label="审批人" width="90">
                  <template #default="scope">
                    <span v-if="scope.row.approved_by" class="text-secondary">
                      #{{ scope.row.approved_by }}
                    </span>
                    <span v-else class="text-secondary">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="发起时间" width="170">
                  <template #default="scope">{{ formatDateTime(scope.row.created_at) }}</template>
                </el-table-column>
                <el-table-column label="完成时间" width="170">
                  <template #default="scope">{{ formatDateTime(scope.row.completed_at) }}</template>
                </el-table-column>
                <template #empty>
                  <el-empty description="该售后单还没有退款流水" :image-size="60" />
                </template>
              </el-table>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="after_sale_no" label="售后单号" width="190" />
        <el-table-column label="订单 ID" width="100">
          <template #default="{ row }">
            <span class="text-secondary">#{{ row.order_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.type" :map="AFTER_SALE_TYPE_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="金额" width="110">
          <template #default="{ row }">
            <span class="text-price">{{ formatAmount(row.amount) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="AFTER_SALE_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="申请原因" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.reason || '—' }}</template>
        </el-table-column>
        <el-table-column label="申请时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <template v-if="canApprove(row)">
              <el-button
                link
                type="primary"
                :loading="actingId === row.after_sale_id"
                @click="handleApprove(row)"
              >
                批准退款
              </el-button>
              <el-button
                link
                type="danger"
                :loading="actingId === row.after_sale_id"
                @click="handleReject(row)"
              >
                驳回
              </el-button>
            </template>
            <el-button
              link
              type="primary"
              :loading="actingId === row.after_sale_id"
              @click="loadRefunds(row)"
            >
              刷新流水
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的售后单" />
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
  </div>
</template>

<style scoped>
.expand {
  padding: 8px 24px 12px;
  background: var(--page-bg);
}
</style>
