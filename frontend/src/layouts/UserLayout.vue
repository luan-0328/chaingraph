<script setup lang="ts">
/**
 * 用户端布局：顶部导航（商城 / 订单 / 售后 / 工单）+ 内容区 + 页脚。
 * v2.md §71：用户端可以稍微偏现代电商，但仍然是浅色企业风格。
 *
 * 导航用自定义按钮而不是 el-menu：
 * 顶部导航只需要"当前在哪一节"这一种状态，自己控制高亮比覆盖 el-menu
 * 默认的下划线样式更简单，也更符合这里的胶囊式设计。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import UnreadTicketBadge from '@/components/ticket/UnreadTicketBadge.vue'
import UserMenu from '@/components/common/UserMenu.vue'
import { useAuthStore } from '@/stores/auth'
import { getCart } from '@/api/cart'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const cartCount = computed(() => auth.cartCount)

const navs = [
  { index: '/products', label: '商品', icon: 'Goods' },
  { index: '/orders', label: '我的订单', icon: 'List' },
  { index: '/after-sales', label: '我的售后', icon: 'RefreshLeft' },
  { index: '/tickets', label: '我的工单', icon: 'ChatDotRound' },
]

const activeMenu = computed(() => {
  const path = route.path
  if (path.startsWith('/products')) return '/products'
  if (path.startsWith('/cart')) return '/products'
  if (path.startsWith('/orders')) return '/orders'
  if (path.startsWith('/after-sales')) return '/after-sales'
  if (path.startsWith('/tickets')) return '/tickets'
  return path
})

onMounted(async () => {
  void auth.fetchCurrentUser()
  // 顶部购物车角标
  if (auth.isLoggedIn) {
    try {
      const cart = await getCart()
      auth.setCartCount(cart.total_quantity)
    } catch {
      /* 忽略 */
    }
  }
})

function go(path: string): void {
  void router.push(path)
}
</script>

<template>
  <div class="layout">
    <header class="layout__header">
      <div class="layout__inner layout__inner--header">
        <button class="layout__brand" type="button" @click="go('/products')">
          <span class="layout__logo">
            <el-icon><Shop /></el-icon>
          </span>
          <span class="layout__brand-text">
            <strong>智能电商</strong>
            <small>客服与售后工单平台</small>
          </span>
        </button>

        <nav class="topnav" aria-label="主导航">
          <button
            v-for="item in navs"
            :key="item.index"
            type="button"
            class="topnav__item"
            :class="{ 'is-active': activeMenu === item.index }"
            @click="go(item.index)"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </button>
        </nav>

        <div class="layout__right">
          <button type="button" class="icon-btn" aria-label="购物车" @click="go('/cart')">
            <el-badge :value="cartCount" :hidden="cartCount === 0" :max="99">
              <el-icon><ShoppingCart /></el-icon>
            </el-badge>
            <span class="icon-btn__label">购物车</span>
          </button>
          <UnreadTicketBadge to="/tickets" />
          <span class="layout__divider" aria-hidden="true"></span>
          <UserMenu />
        </div>
      </div>
    </header>

    <main class="layout__main">
      <div class="layout__inner">
        <router-view />
      </div>
    </main>

    <footer class="layout__footer">
      <div class="layout__inner layout__inner--footer">
        <span>智能电商客服与售后工单平台 · v2.0</span>
        <span class="layout__footer-points"> Agent 自动处理 · 人工审批兜底 · 全流程可追溯 </span>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.layout__inner {
  width: 100%;
  max-width: 1240px;
  margin: 0 auto;
  padding: 0 28px;
}

.layout__inner--header {
  display: flex;
  align-items: center;
  gap: 28px;
  height: 64px;
}

.layout__header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgb(255 255 255 / 88%);
  border-bottom: 1px solid var(--border-color);
  backdrop-filter: blur(12px);
}

.layout__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0;
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
  width: 36px;
  height: 36px;
  border-radius: 11px;
  background: var(--brand);
  color: #fff;
  font-size: 18px;
  box-shadow: var(--shadow-brand);
}

.layout__brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.15;
}

.layout__brand-text strong {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: 0.2px;
}

.layout__brand-text small {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-secondary);
  letter-spacing: 0.2px;
}

.topnav {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.topnav__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding: 0 14px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-regular);
  font-family: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition:
    background-color var(--dur) var(--ease),
    color var(--dur) var(--ease);
}

.topnav__item:hover {
  background: #f4f6fb;
  color: var(--text-primary);
}

.topnav__item.is-active {
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
}

.topnav__item .el-icon {
  font-size: 16px;
}

.layout__right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding: 0 12px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-regular);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
  transition:
    background-color var(--dur) var(--ease),
    color var(--dur) var(--ease);
}

.icon-btn:hover {
  background: #f4f6fb;
  color: var(--text-primary);
}

.icon-btn .el-icon {
  font-size: 18px;
}

.layout__divider {
  width: 1px;
  height: 20px;
  margin: 0 6px;
  background: var(--border-color);
}

.layout__main {
  flex: 1;
  padding: 24px 0 56px;
}

.layout__footer {
  border-top: 1px solid var(--border-color);
  background: #fff;
}

.layout__inner--footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 56px;
  color: var(--text-secondary);
  font-size: 12px;
  flex-wrap: wrap;
}

.layout__footer-points {
  letter-spacing: 0.3px;
}

@media (max-width: 960px) {
  .layout__inner--header {
    gap: 12px;
  }

  .layout__brand-text small,
  .icon-btn__label,
  .topnav__item span {
    display: none;
  }

  .topnav__item {
    padding: 0 10px;
  }
}

@media (max-width: 640px) {
  .layout__inner {
    padding: 0 16px;
  }

  .layout__brand-text {
    display: none;
  }

  .topnav {
    overflow-x: auto;
  }
}
</style>
