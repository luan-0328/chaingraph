<script setup lang="ts">
/**
 * 我的工单：列表 + 状态筛选 + 分页 + 提交问题入口。
 *
 * 状态筛选做成胶囊而不是下拉：工单状态数量少，平铺出来能一眼看到
 * "我还有没有在处理中的单"，少一次点击。
 */
import { onUnmounted, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import StatusTag from '@/components/common/StatusTag.vue'
import { listMyTickets } from '@/api/tickets'
import type { TicketSummary } from '@/types'
import { TICKET_STATUS_MAP, TICKET_STATUS_OPTIONS, TICKET_TYPE_MAP } from '@/utils/dict'
import { formatDateTime, truncate } from '@/utils/format'

const router = useRouter()

const PAGE_SIZE = 10

const loading = ref(false)
const tickets = ref<TicketSummary[]>([])
const total = ref(0)
const page = ref(1)
const statusFilter = ref<string>('')

/** "全部" + 字典里的状态；顺序沿用字典，避免前端另排一套 */
const statusFilters = [{ value: '', label: '全部' }, ...TICKET_STATUS_OPTIONS]

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listMyTickets({
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
      status: statusFilter.value || undefined,
    })
    tickets.value = result.items
    total.value = result.total
  } catch {
    tickets.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function selectStatus(value: string): void {
  if (statusFilter.value === value) return
  statusFilter.value = value
  page.value = 1
  void load()
}

function handlePageChange(next: number): void {
  page.value = next
  void load()
}

function goDetail(row: TicketSummary): void {
  void router.push({ name: 'ticket-detail', params: { id: row.ticket_id } })
}

function goCreate(): void {
  void router.push({ name: 'ticket-create' })
}

/** 标题可能为空（用户只填了描述），此时用描述的前若干字顶上 */
function displayTitle(row: TicketSummary): string {
  return row.title || truncate(row.content, 24)
}

let refreshTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  void load()
  refreshTimer = setInterval(() => {
    if (!loading.value && document.visibilityState === 'visible') void load()
  }, 10000)
})
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-header__title">我的工单</h2>
        <p class="page-header__sub">
          提交问题后，Agent 会查询订单与售后数据，并把处理进度实时回传。
        </p>
      </div>
      <el-button type="primary" @click="goCreate">
        <el-icon><Plus /></el-icon>
        <span>提交问题</span>
      </el-button>
    </div>

    <div class="page-card">
      <div class="filter-bar">
        <button
          v-for="option in statusFilters"
          :key="option.value"
          type="button"
          class="filter-bar__item"
          :class="{ 'is-active': statusFilter === option.value }"
          @click="selectStatus(option.value)"
        >
          {{ option.label }}
        </button>
        <el-button class="filter-bar__refresh" text @click="load">
          <el-icon><Refresh /></el-icon>
          <span>刷新</span>
        </el-button>
      </div>

      <el-table v-loading="loading" :data="tickets" row-key="ticket_id">
        <el-table-column label="未读" width="75">
          <template #default="{ row }">
            <el-tag v-if="row.unread_count" type="danger" size="small">{{
              row.unread_count
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="工单号" width="180">
          <template #default="{ row }">
            <el-link type="primary" :underline="false" @click="goDetail(row)">
              <span class="mono">{{ row.ticket_no }}</span>
            </el-link>
          </template>
        </el-table-column>

        <el-table-column label="标题" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ displayTitle(row) }}</template>
        </el-table-column>

        <el-table-column label="类型" width="120" align="center">
          <template #default="{ row }">
            <!-- type 可能是 null（用户没选类型），StatusTag 会显示 "—" -->
            <StatusTag :value="row.type" :map="TICKET_TYPE_MAP" />
          </template>
        </el-table-column>

        <el-table-column label="状态" width="120" align="center">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="TICKET_STATUS_MAP" />
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="100" fixed="right" align="center">
          <template #default="{ row }">
            <el-button type="primary" link @click="goDetail(row)">查看</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="还没有工单，有问题可以直接提交">
            <el-button type="primary" @click="goCreate">提交问题</el-button>
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
