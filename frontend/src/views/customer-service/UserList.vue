<script setup lang="ts">
/**
 * 用户查询（v2.md §23.5、§6）。
 *
 * 客服查用户只有一个目的：确认"这个人是谁、能不能操作这笔单"。
 * 所以列里保留了 user_id（工单/订单里到处是 id，能对上号很重要），
 * 关键词搜索走后端参数（username / nickname），不做前端过滤。
 *
 * 注意：后端 listUsers 的 keyword 只匹配 username / nickname，不支持按 user_id 搜，
 * 所以工单详情里的"提交用户 #12"没法直接反查到昵称 —— 这是接口边界，不是漏写。
 */
import { onMounted, reactive, ref } from 'vue'

import { listUsers } from '@/api/customerService'
import type { AdminUserInfo } from '@/types'
import { USER_ROLE_OPTIONS, roleLabel } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const rows = ref<AdminUserInfo[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  keyword: '',
  role: '',
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listUsers({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      keyword: filters.keyword.trim() || undefined,
      role: filters.role || undefined,
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
  filters.role = ''
  search()
}

function onSizeChange(size: number): void {
  pageSize.value = size
  page.value = 1
  void load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">用户查询</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="filters.keyword"
          placeholder="用户名 / 昵称"
          clearable
          style="width: 220px"
          @keyup.enter="search"
        />

        <el-select v-model="filters.role" placeholder="全部角色" clearable style="width: 150px">
          <el-option
            v-for="option in USER_ROLE_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="rows" size="default">
        <el-table-column prop="user_id" label="用户 ID" width="100" />
        <el-table-column prop="username" label="用户名" min-width="150" />
        <el-table-column label="昵称" min-width="150">
          <template #default="{ row }">{{ row.nickname || '—' }}</template>
        </el-table-column>
        <el-table-column label="角色" width="120">
          <template #default="{ row }">
            <el-tag size="small" disable-transitions>{{ roleLabel(row.role) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <!-- 用户状态后端只给了字符串（无对应中文字典），如实展示原始值 -->
          <template #default="{ row }">
            <el-tag size="small" disable-transitions>{{ row.status }}</el-tag>
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
