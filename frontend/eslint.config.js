/**
 * ESLint 扁平配置（ESLint 9）。
 *
 * 组合：
 *   pluginVue            —— Vue 官方规则
 *   vueTsConfigs         —— Vue + TypeScript 推荐规则
 *   prettierConfig       —— 关掉所有和 Prettier 冲突的格式规则
 *                          （格式化交给 Prettier，Lint 只管代码质量）
 */
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import pluginVue from 'eslint-plugin-vue'
import prettierConfig from '@vue/eslint-config-prettier'

export default defineConfigWithVueTs(
  // 不检查的目录
  { ignores: ['dist/**', 'node_modules/**', '*.d.ts'] },

  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  prettierConfig,

  {
    rules: {
      // 页面组件用 Login.vue / TicketList.vue 这种单词命名是刻意的，关掉这条限制
      'vue/multi-word-component-names': 'off',
    },
  },
)
