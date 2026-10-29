<script setup lang="ts">
/**
 * 工单总览（路由 admin-tickets）。
 *
 * 用的是客服端的 listTickets（GET /customer-service/tickets）：它返回结构化的
 * PageResult<TicketSummary> 且支持 status/keyword/only_mine/unassigned/waiting_human 筛选；
 * 管理端的 listAllTickets 只返回 Record<string, unknown>，管理总览没必要牺牲类型。
 *
 * 管理端只做"看"：点行跳到客服工单详情去处理（那边有领取/回复/审批能力）。
 */
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { listTickets } from '@/api/customerService'
import StatusTag from '@/components/common/StatusTag.vue'
import type { TicketSummary } from '@/types'
import {
  TICKET_PRIORITY_MAP,
  TICKET_STATUS_MAP,
  TICKET_STATUS_OPTIONS,
  TICKET_TYPE_MAP,
} from '@/utils/dict'
import { formatDateTime, truncate } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const tickets = ref<TicketSummary[]>([])

const query = reactive<{
  keyword: string
  status: string | undefined
  only_mine: boolean
  unassigned: boolean
  waiting_human: boolean
}>({
  keyword: '',
  status: undefined,
  only_mine: false,
  unassigned: false,
  waiting_human: false,
})
const pagination = reactive({ current: 1, limit: 10, total: 0 })

async function loadTickets(): Promise<void> {
  loading.value = true
  try {
    const result = await listTickets({
      limit: pagination.limit,
      offset: (pagination.current - 1) * pagination.limit,
      keyword: query.keyword.trim() || undefined,
      status: query.status || undefined,
      only_mine: query.only_mine || undefined,
      unassigned: query.unassigned || undefined,
      waiting_human: query.waiting_human || undefined,
    })
    tickets.value = result.items
    pagination.total = result.total
  } finally {
    // 错误提示由 request 拦截器统一负责
    loading.value = false
  }
}

function search(): void {
  pagination.current = 1
  void loadTickets()
}

function resetQuery(): void {
  query.keyword = ''
  query.status = undefined
  query.only_mine = false
  query.unassigned = false
  query.waiting_human = false
  search()
}

/**
 * "只看我领取的"和"只看未领取的"语义互斥（一个要求有领取人、一个要求没有），
 * 同时传后端会返回空列表，所以勾选一个时自动取消另一个。
 * v-model 已经写回了 query，这里只做互斥和重新查询。
 */
function onOnlyMineChange(): void {
  if (query.only_mine) query.unassigned = false
  search()
}

function onUnassignedChange(): void {
  if (query.unassigned) query.only_mine = false
  search()
}

function handlePageChange(page: number): void {
  pagination.current = page
  void loadTickets()
}

function handleSizeChange(size: number): void {
  pagination.limit = size
  pagination.current = 1
  void loadTickets()
}

/** 管理端不提供处理动作，跳到客服端详情页（复用同一套详情与操作） */
function openTicket(row: TicketSummary): void {
  void router.push({ name: 'cs-ticket-detail', params: { id: row.ticket_id } })
}

onMounted(loadTickets)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">工单总览</h2>
      <span class="text-secondary">点击任意一行可进入客服工单详情</span>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="按工单号 / 标题搜索"
          clearable
          style="width: 240px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select
          v-model="query.status"
          placeholder="全部状态"
          clearable
          style="width: 160px"
          @change="search"
        >
          <el-option
            v-for="option in TICKET_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-checkbox v-model="query.only_mine" @change="onOnlyMineChange">只看我领取的</el-checkbox>
        <el-checkbox v-model="query.unassigned" @change="onUnassignedChange"
          >只看未领取的</el-checkbox
        >
        <el-checkbox v-model="query.waiting_human" @change="search">只看待人工确认</el-checkbox>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetQuery">重置</el-button>
      </div>

      <el-table
        v-loading="loading"
        :data="tickets"
        border
        stripe
        row-class-name="row-clickable"
        @row-click="openTicket"
      >
        <el-table-column prop="ticket_no" label="工单号" width="170" show-overflow-tooltip />
        <el-table-column label="标题" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.title || truncate(row.content, 30) }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.type" :map="TICKET_TYPE_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="TICKET_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <StatusTag :value="row.priority" :map="TICKET_PRIORITY_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="提交人" width="100">
          <template #default="{ row }">用户 #{{ row.user_id }}</template>
        </el-table-column>
        <el-table-column label="领取人" width="100">
          <template #default="{ row }">
            {{ row.assignee_id === null ? '未领取' : `#${row.assignee_id}` }}
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <!-- .stop：否则会同时触发 row-click，导致同一个路由被 push 两次 -->
            <el-button link type="primary" @click.stop="openTicket(row)">查看详情</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的工单" />
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
  </div>
</template>

<style scoped>
/* 整行可点，给出鼠标手型，避免用户以为只有"查看详情"能点 */
:deep(.row-clickable) {
  cursor: pointer;
}
</style>
