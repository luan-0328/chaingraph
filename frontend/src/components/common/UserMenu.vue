<script setup lang="ts">
/** 用户菜单：显示昵称/角色，提供个人信息、切换到客服/管理端、退出登录 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const roleText = computed(() => {
  if (auth.role === 'ADMIN') return '管理员'
  if (auth.role === 'CUSTOMER_SERVICE') return '客服'
  return '普通用户'
})

async function handleCommand(command: string): Promise<void> {
  if (command === 'profile') {
    await router.push({ name: 'profile' })
    return
  }
  if (command === 'console') {
    await router.push(auth.homeRoute)
    return
  }
  if (command === 'logout') {
    try {
      await ElMessageBox.confirm('确认退出登录？', '提示', {
        confirmButtonText: '退出',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
    auth.logout()
    ElMessage.success('已退出登录')
    await router.push({ name: 'login' })
  }
}
</script>

<template>
  <el-dropdown trigger="click" @command="handleCommand">
    <span class="user-menu">
      <el-icon><UserFilled /></el-icon>
      <span class="user-menu__name">{{ auth.username || '未登录' }}</span>
      <el-tag size="small" type="info">{{ roleText }}</el-tag>
      <el-icon><ArrowDown /></el-icon>
    </span>

    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item command="profile">个人信息</el-dropdown-item>
        <el-dropdown-item v-if="auth.isStaff" command="console">
          {{ auth.isAdmin ? '管理后台' : '客服工作台' }}
        </el-dropdown-item>
        <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style scoped>
.user-menu {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--text-regular);
  outline: none;
}

.user-menu__name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
