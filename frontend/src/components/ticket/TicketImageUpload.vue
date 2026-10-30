<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { uploadTicketImage } from '@/api/tickets'

const props = defineProps<{ ticketId: number; disabled?: boolean }>()
const emit = defineEmits<{ (e: 'uploaded'): void }>()
const input = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
async function selected(event: Event): Promise<void> {
  const element = event.target as HTMLInputElement
  const file = element.files?.[0]
  if (!file) return
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) {
    ElMessage.warning('请选择 5 MB 以内的 PNG、JPEG 或 WebP 图片')
    element.value = ''
    return
  }
  uploading.value = true
  try {
    await uploadTicketImage(props.ticketId, file)
    emit('uploaded')
    ElMessage.success('图片已发送')
  } catch { /* 请求拦截器显示错误，保留文字沟通 */ } finally { uploading.value = false; element.value = '' }
}
</script>

<template>
  <div style="margin-top:8px">
    <input ref="input" type="file" accept="image/png,image/jpeg,image/webp" hidden aria-label="选择工单图片" @change="selected" />
    <el-button :disabled="disabled" :loading="uploading" @click="input?.click()">发送图片</el-button>
    <span class="text-secondary" style="margin-left:8px">PNG / JPEG / WebP，最大 5 MB</span>
  </div>
</template>
