<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getTicketImageUrl } from '@/api/tickets'
import type { TicketAttachment } from '@/types'

const props = defineProps<{ attachment: TicketAttachment }>()
const url = ref('')
const loading = ref(false)
async function load(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try { url.value = (await getTicketImageUrl(props.attachment.ticket_id, props.attachment.attachment_id)).url }
  catch { /* 鉴权或签名失败由拦截器提示，允许重试 */ }
  finally { loading.value = false }
}
onMounted(() => { void load().catch(() => {}) })
</script>

<template>
  <div v-loading="loading">
    <el-image v-if="url" :src="url" :preview-src-list="[url]" preview-teleported fit="contain" style="max-width:240px;max-height:180px;display:block">
      <template #error><el-button text @click="load">重新加载图片</el-button></template>
    </el-image>
    <el-button v-else text @click="load">查看图片</el-button>
    <small>{{ attachment.filename }}</small>
  </div>
</template>
