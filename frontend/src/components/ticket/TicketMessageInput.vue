<script setup lang="ts">
/**
 * 工单消息输入框：textarea + 发送按钮。
 *
 * 两个设计点值得注意：
 *   1. Enter 要留给"换行"（工单描述经常是多行的），所以用 Ctrl/Cmd + Enter 发送；
 *   2. 组件**不清空输入框**，只抛 send 事件；父组件在接口成功后才调 clear()。
 *      否则发送失败（例如已关闭的工单）时用户刚写的内容就被冲掉了。
 */
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 父组件正在发送（按钮转圈 + 禁止重复提交） */
    loading?: boolean
    disabled?: boolean
    placeholder?: string
  }>(),
  { loading: false, disabled: false, placeholder: '描述你的问题，或补充说明…（Ctrl + Enter 发送）' },
)

const emit = defineEmits<{ (e: 'send', content: string): void }>()

/** 后端 TicketMessageCreate.content 的上限是 2000，前端先卡住，避免提交才报 422 */
const MAX_LENGTH = 2000

const content = ref('')

const canSend = computed(
  () => !props.disabled && !props.loading && content.value.trim().length > 0,
)

function submit(): void {
  if (props.disabled) return
  const text = content.value.trim()
  if (!text) {
    ElMessage.warning('请输入内容')
    return
  }
  if (props.loading) return // 正在发送，忽略重复点击/重复回车
  emit('send', text)
}

function clear(): void {
  content.value = ''
}

/** 父组件在发送成功后调用（见顶部注释） */
defineExpose({ clear })
</script>

<template>
  <div class="message-input">
    <el-input
      v-model="content"
      type="textarea"
      :rows="3"
      :maxlength="MAX_LENGTH"
      show-word-limit
      :disabled="disabled"
      :placeholder="placeholder"
      resize="none"
      @keydown.ctrl.enter.prevent="submit"
      @keydown.meta.enter.prevent="submit"
    />

    <div class="message-input__actions">
      <span class="text-secondary message-input__hint">Ctrl + Enter 快速发送</span>
      <el-button type="primary" :loading="loading" :disabled="!canSend" @click="submit">
        发送
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.message-input {
  margin-top: 12px;
}

.message-input__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.message-input__hint {
  font-size: 12px;
}
</style>
