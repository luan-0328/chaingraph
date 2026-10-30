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

/** 头像占位：取昵称/用户名的首字，中文用户也能得到一个可读的"头像" */
const initial = computed(() => (auth.username || '未').slice(0, 1).toUpperCase())

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
      <span class="user-menu__avatar" :class="{ 'user-menu__avatar--staff': auth.isStaff }">
        {{ initial }}
      </span>
      <span class="user-menu__meta">
        <span class="user-menu__name">{{ auth.username || '未登录' }}</span>
        <span class="user-menu__role">{{ roleText }}</span>
      </span>
      <el-icon class="user-menu__chevron"><ArrowDown /></el-icon>
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
  gap: 8px;
  padding: 4px 8px 4px 4px;
  border-radius: var(--radius-pill);
  cursor: pointer;
  outline: none;
  transition: background-color var(--dur) var(--ease);
}

.user-menu:hover {
  background: #f4f6fb;
}

.user-menu__avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #eef3ff;
  color: var(--brand);
  font-size: 13px;
  font-weight: 650;
  flex: none;
}

.user-menu__avatar--staff {
  background: #e8f7f1;
  color: var(--success);
}

.user-menu__meta {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  min-width: 0;
}

.user-menu__name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.user-menu__role {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-secondary);
}

.user-menu__chevron {
  color: var(--text-secondary);
  font-size: 12px;
}

@media (max-width: 960px) {
  .user-menu__meta {
    display: none;
  }
}
</style>
