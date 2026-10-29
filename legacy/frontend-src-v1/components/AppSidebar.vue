<script setup lang="ts">
/**
 * 左侧菜单。
 * 前端文档第四十二节：V1 不需要太多菜单（工作台 / 工单管理），
 * 而且首页不做 Dashboard 大屏 —— 所以这里只有"工单管理"和"创建工单"。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

// 详情页也要让"工单管理"保持高亮
const activeMenu = computed(() =>
  route.path.startsWith('/tickets/create') ? '/tickets/create' : '/tickets',
)
</script>

<template>
  <aside class="app-sidebar">
    <el-menu :default-active="activeMenu" router class="app-sidebar__menu">
      <el-menu-item index="/tickets">
        <el-icon><Tickets /></el-icon>
        <span>工单管理</span>
      </el-menu-item>

      <el-menu-item index="/tickets/create">
        <el-icon><DocumentAdd /></el-icon>
        <span>创建工单</span>
      </el-menu-item>
    </el-menu>
  </aside>
</template>

<style scoped>
.app-sidebar {
  width: 200px;
  flex-shrink: 0;
  background: #fff;
  border-right: 1px solid var(--border-color);
}

.app-sidebar__menu {
  border-right: none;
}
</style>
