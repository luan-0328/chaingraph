<script setup lang="ts">
/**
 * 主布局：顶部 Header（全宽） + 左侧 Sidebar + 中间内容区。
 * 对应前端文档第三节的布局草图。
 */
import { onMounted } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import AppSidebar from '@/components/AppSidebar.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// 刷新页面后校验一次令牌，顺便把用户信息刷新成最新的
onMounted(() => {
  void auth.fetchCurrentUser()
})
</script>

<template>
  <div class="layout">
    <AppHeader />

    <div class="layout__body">
      <AppSidebar />

      <main class="layout__main">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.layout__body {
  display: flex;
  flex: 1;
  min-height: 0;
}

.layout__main {
  flex: 1;
  min-width: 0;
  padding: 20px;
  background: var(--page-bg);
  overflow: auto;
}
</style>
