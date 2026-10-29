<script setup lang="ts">
/**
 * 客服端 / 管理端布局：左侧菜单 + 顶部栏 + 内容区（v2.md §23.1、§52）。
 * 同一个布局服务两种角色：菜单按 role 显示（权限仍由后端校验）。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import UserMenu from '@/components/common/UserMenu.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const menus = computed(() => {
  if (auth.isAdmin) {
    return [
      { index: '/admin/products', label: '商品管理', icon: 'Goods' },
      { index: '/admin/orders', label: '订单总览', icon: 'List' },
      { index: '/admin/tickets', label: '工单总览', icon: 'Tickets' },
      { index: '/admin/users', label: '用户总览', icon: 'User' },
      { index: '/admin/knowledge', label: '知识库', icon: 'Reading' },
      { index: '/customer-service/workbench', label: '客服工作台', icon: 'Service' },
    ]
  }
  return [
    { index: '/customer-service/workbench', label: '工作台', icon: 'DataBoard' },
    { index: '/customer-service/tickets', label: '工单处理', icon: 'Tickets' },
    { index: '/customer-service/after-sales', label: '售后审核', icon: 'RefreshLeft' },
    { index: '/customer-service/orders', label: '订单查询', icon: 'List' },
    { index: '/customer-service/users', label: '用户查询', icon: 'User' },
  ]
})

const activeMenu = computed(() => {
  const path = route.path
  // 工单详情页要让它所属的列表菜单保持高亮
  if (path.startsWith('/customer-service/tickets')) return '/customer-service/tickets'
  return path
})

const title = computed(() => (route.meta.title as string) || '工作台')

onMounted(() => {
  void auth.fetchCurrentUser()
})

function go(index: string): void {
  void router.push(index)
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="210px" class="layout__aside">
      <div class="layout__brand">
        <el-icon><Service /></el-icon>
        <span>{{ auth.isAdmin ? '运营管理后台' : '客服工作台' }}</span>
      </div>

      <el-menu :default-active="activeMenu" class="layout__menu">
        <el-menu-item
          v-for="menu in menus"
          :key="menu.index"
          :index="menu.index"
          @click="go(menu.index)"
        >
          <el-icon><component :is="menu.icon" /></el-icon>
          <span>{{ menu.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header height="56px" class="layout__header">
        <div class="layout__title">{{ title }}</div>
        <div class="layout__right">
          <el-button v-if="!auth.isAdmin" text @click="go('/products')">
            <el-icon><Shop /></el-icon>
            <span>回到商城</span>
          </el-button>
          <UserMenu />
        </div>
      </el-header>

      <el-main class="layout__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
}

.layout__aside {
  background: #fff;
  border-right: 1px solid var(--border-color);
}

.layout__brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 56px;
  padding: 0 16px;
  font-weight: 600;
  border-bottom: 1px solid var(--border-color);
}

.layout__menu {
  border-right: none;
}

.layout__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid var(--border-color);
}

.layout__title {
  font-size: 16px;
  font-weight: 600;
}

.layout__right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.layout__main {
  background: var(--page-bg);
  padding: 20px;
}
</style>
