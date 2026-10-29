<script setup lang="ts">
/** 公共状态标签：传字典 map + 值，统一渲染 Element Plus Tag */
import { computed } from 'vue'
import type { PropType } from 'vue'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

const props = defineProps({
  value: { type: String as PropType<string | null | undefined>, default: null },
  map: {
    type: Object as PropType<Record<string, { label: string; tag: TagType }>>,
    required: true,
  },
})

const meta = computed(
  () => props.map[props.value ?? ''] ?? { label: props.value ?? '—', tag: 'info' as TagType },
)
</script>

<template>
  <el-tag :type="meta.tag" disable-transitions>{{ meta.label }}</el-tag>
</template>
