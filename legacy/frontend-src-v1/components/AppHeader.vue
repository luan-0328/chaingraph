<script setup lang="ts">
/** 顶部栏：系统名称 + 当前用户 + 退出登录 */
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

async function handleCommand(command: string): Promise<void> {
  if (command !== 'logout') return

  try {
    await ElMessageBox.confirm('确认退出登录？', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户点了取消
  }

  auth.logout()
  ElMessage.success('已退出登录')
  await router.push({ name: 'login' })
}
</script>

<template>
  <header class="app-header">
    <div class="app-header__brand">工单智能处理平台</div>

    <el-dropdown trigger="click" @command="handleCommand">
      <span class="app-header__user">
        <el-icon><User /></el-icon>
        <span class="app-header__username">{{ auth.username || '未登录' }}</span>
        <el-icon><ArrowDown /></el-icon>
      </span>

      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="logout">退出登录</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.app-header__brand {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: 0.5px;
}

.app-header__user {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--text-regular);
  outline: none;
}

.app-header__username {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
