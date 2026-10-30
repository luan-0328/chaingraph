<script setup lang="ts">
/**
 * 客服端 / 管理端布局：左侧菜单 + 顶部栏 + 内容区（v2.md §23.1、§52）。
 * 同一个布局服务两种角色：菜单按 role 显示（权限仍由后端校验）。
 *
 * 视觉上保持"企业管理后台"：浅色侧栏、清晰的当前项高亮、足够大的点击区。
 * 客服长时间盯屏，侧栏不做深色/高饱和，减少视觉疲劳。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import UnreadTicketBadge from '@/components/ticket/UnreadTicketBadge.vue'
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

const sectionLabel = computed(() => (auth.isAdmin ? '运营管理' : '客服中心'))
const roleLabel = computed(() => (auth.isAdmin ? '管理员' : '客服'))

onMounted(() => {
  void auth.fetchCurrentUser()
})

function go(index: string): void {
  void router.push(index)
}
</script>

<template>
  <div class="layout">
    <aside class="layout__aside">
      <button type="button" class="layout__brand" @click="router.push(auth.homeRoute)">
        <span class="layout__logo">
          <el-icon><Service /></el-icon>
        </span>
        <span class="layout__brand-text">
          <strong>{{ auth.isAdmin ? '运营管理后台' : '客服工作台' }}</strong>
          <small>智能电商工单平台</small>
        </span>
      </button>

      <div class="layout__section">{{ sectionLabel }}</div>

      <nav class="sidemenu" aria-label="后台导航">
        <button
          v-for="menu in menus"
          :key="menu.index"
          type="button"
          class="sidemenu__item"
          :class="{ 'is-active': activeMenu === menu.index }"
          @click="go(menu.index)"
        >
          <el-icon><component :is="menu.icon" /></el-icon>
          <span class="sidemenu__label">{{ menu.label }}</span>
        </button>
      </nav>

      <div class="layout__aside-foot">
        <div class="layout__tip">
          <span class="status-dot"></span>
          <span>Agent 异步处理中</span>
        </div>
        <div class="layout__version">v2.0</div>
      </div>
    </aside>

    <div class="layout__body">
      <header class="layout__header">
        <div class="layout__title-wrap">
          <span class="layout__crumb">{{ sectionLabel }} /</span>
          <h1 class="layout__title">{{ title }}</h1>
          <span class="layout__role">{{ roleLabel }}</span>
        </div>

        <div class="layout__right">
          <el-button v-if="!auth.isAdmin" text class="layout__shop-link" @click="go('/products')">
            <el-icon><Shop /></el-icon>
            <span>回到商城</span>
          </el-button>
          <UnreadTicketBadge to="/customer-service/tickets" />
          <span class="layout__divider" aria-hidden="true"></span>
          <UserMenu />
        </div>
      </header>

      <main class="layout__main">
        <div class="layout__content">
          <router-view />
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  min-height: 100vh;
}

.layout__aside {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  width: 232px;
  height: 100vh;
  flex: none;
  background: #fff;
  border-right: 1px solid var(--border-color);
  padding: 0 12px 14px;
}

.layout__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 64px;
  padding: 0 8px;
  border: none;
  background: none;
  cursor: pointer;
  color: inherit;
  text-align: left;
  flex: none;
}

.layout__logo {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--brand);
  color: #fff;
  font-size: 17px;
  box-shadow: var(--shadow-brand);
  flex: none;
}

.layout__brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  min-width: 0;
}

.layout__brand-text strong {
  font-size: 14px;
  font-weight: 650;
  white-space: nowrap;
}

.layout__brand-text small {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.layout__section {
  padding: 14px 12px 8px;
  color: #a9b0bf;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 1.2px;
}

.sidemenu {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  overflow-y: auto;
}

.sidemenu__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 40px;
  padding: 0 12px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-regular);
  font-family: inherit;
  font-size: 13.5px;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
  white-space: nowrap;
  transition:
    background-color var(--dur) var(--ease),
    color var(--dur) var(--ease);
}

.sidemenu__item .el-icon {
  font-size: 17px;
  color: var(--text-secondary);
  transition: color var(--dur) var(--ease);
}

.sidemenu__item:hover {
  background: #f6f8fc;
  color: var(--text-primary);
}

.sidemenu__item.is-active {
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
}

.sidemenu__item.is-active::before {
  content: '';
  position: absolute;
  left: -12px;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--brand);
}

.sidemenu__item.is-active .el-icon {
  color: var(--brand);
}

.layout__aside-foot {
  flex: none;
  padding: 12px 10px 2px;
  border-top: 1px solid var(--border-color);
  color: var(--text-secondary);
  font-size: 11px;
}

.layout__tip {
  display: flex;
  align-items: center;
  gap: 6px;
}

.layout__tip .status-dot {
  background: var(--success);
  box-shadow: 0 0 0 3px rgb(22 163 116 / 12%);
}

.layout__version {
  margin-top: 4px;
  letter-spacing: 0.4px;
  color: #b3bac8;
}

.layout__body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.layout__header {
  position: sticky;
  top: 0;
  z-index: 15;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: 64px;
  padding: 0 28px;
  background: rgb(255 255 255 / 88%);
  border-bottom: 1px solid var(--border-color);
  backdrop-filter: blur(12px);
}

.layout__title-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.layout__crumb {
  color: var(--text-secondary);
  font-size: 13px;
  white-space: nowrap;
}

.layout__title {
  margin: 0;
  font-size: 17px;
  font-weight: 650;
  letter-spacing: -0.2px;
  white-space: nowrap;
}

.layout__role {
  margin-left: 2px;
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  background: #f1f3f7;
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}

.layout__right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.layout__divider {
  width: 1px;
  height: 20px;
  margin: 0 4px;
  background: var(--border-color);
}

.layout__main {
  flex: 1;
  min-width: 0;
  background: var(--page-bg);
}

.layout__content {
  max-width: 1560px;
  margin: 0 auto;
  padding: 24px 28px 48px;
}

@media (max-width: 1080px) {
  .layout__aside {
    width: 72px;
    padding: 0 8px 14px;
  }

  .layout__brand {
    justify-content: center;
    padding: 0;
  }

  .layout__brand-text,
  .layout__section,
  .sidemenu__label,
  .layout__aside-foot {
    display: none;
  }

  .sidemenu__item {
    justify-content: center;
    padding: 0;
  }

  .sidemenu__item.is-active::before {
    left: -8px;
  }
}

@media (max-width: 768px) {
  .layout__header {
    padding: 0 16px;
  }

  .layout__crumb,
  .layout__role {
    display: none;
  }

  .layout__content {
    padding: 16px 16px 40px;
  }
}
</style>
