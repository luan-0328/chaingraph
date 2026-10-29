<script setup lang="ts">
/**
 * 用户总览（路由 admin-users）。
 *
 * 用客服端的 listUsers（GET /customer-service/users）：与 /admin/users 返回同一套
 * AdminUserInfo DTO，但客服端接口是页面已经在用的那一个，保持一致更好排查。
 * 这里只读不写 —— v2.md 没给管理端"改用户角色/禁用账号"的接口，不虚构功能。
 */
import { onMounted, reactive, ref } from 'vue'

import { listUsers } from '@/api/customerService'
import type { AdminUserInfo } from '@/types'
import { USER_ROLE_OPTIONS, roleLabel } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/**
 * 账号状态（ACTIVE/DISABLED）dict.ts 未收录：它属于管理端只读展示，
 * 客服端的角色/状态文案里用不到，所以就近定义在页面里。
 */
const USER_STATUS_MAP: Record<string, { label: string; tag: TagType }> = {
  ACTIVE: { label: '正常', tag: 'success' },
  DISABLED: { label: '已禁用', tag: 'danger' },
}

const loading = ref(false)
const users = ref<AdminUserInfo[]>([])

const query = reactive<{ keyword: string; role: string | undefined }>({
  keyword: '',
  role: undefined,
})
const pagination = reactive({ current: 1, limit: 10, total: 0 })

async function loadUsers(): Promise<void> {
  loading.value = true
  try {
    const result = await listUsers({
      limit: pagination.limit,
      offset: (pagination.current - 1) * pagination.limit,
      keyword: query.keyword.trim() || undefined,
      role: query.role || undefined,
    })
    users.value = result.items
    pagination.total = result.total
  } finally {
    // 错误提示由 request 拦截器统一负责
    loading.value = false
  }
}

function search(): void {
  pagination.current = 1
  void loadUsers()
}

function resetQuery(): void {
  query.keyword = ''
  query.role = undefined
  search()
}

function handlePageChange(page: number): void {
  pagination.current = page
  void loadUsers()
}

function handleSizeChange(size: number): void {
  pagination.limit = size
  pagination.current = 1
  void loadUsers()
}

function statusMeta(status: string): { label: string; tag: TagType } {
  return USER_STATUS_MAP[status] ?? { label: status, tag: 'info' }
}

onMounted(loadUsers)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">用户总览</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="按用户名 / 昵称搜索"
          clearable
          style="width: 240px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select
          v-model="query.role"
          placeholder="全部角色"
          clearable
          style="width: 150px"
          @change="search"
        >
          <el-option
            v-for="option in USER_ROLE_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetQuery">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="users" border stripe>
        <el-table-column prop="user_id" label="用户 ID" width="100" />
        <el-table-column prop="username" label="用户名" min-width="160" show-overflow-tooltip />
        <el-table-column label="昵称" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.nickname || '—' }}</template>
        </el-table-column>
        <el-table-column label="角色" width="120">
          <!-- 角色文案统一走 dict 的 roleLabel，不在页面里另写一份映射 -->
          <template #default="{ row }">{{ roleLabel(row.role) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusMeta(row.status).tag" disable-transitions>
              {{ statusMeta(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的用户" />
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
