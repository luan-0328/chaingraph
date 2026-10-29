<script setup lang="ts">
/**
 * 知识库管理（路由 admin-knowledge，v2.md §34）—— 这块直接决定 Agent 的 RAG 效果。
 *
 * 三件事要分清：
 * 1. 文档 CRUD：维护 knowledge_documents（原始知识，管理员手写的规则/政策）；
 * 2. 知识入库 ingest：把文档切分 + 向量化写进 knowledge_chunks，**不跑这一步 RAG 就检索不到**；
 *    幂等（已有分块的文档跳过），换 embedding 模型后要 force=true 全量重建向量；
 * 3. 检索调试：直接调检索器看命中了哪些片段和相似度，用来排查"为什么 Agent 答错了"。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import {
  createKnowledge,
  deleteKnowledge,
  ingestKnowledge,
  listKnowledge,
  searchKnowledge,
  updateKnowledge,
} from '@/api/admin'
import type { KnowledgeInfo, KnowledgeSearchHit } from '@/types'
import { formatDateTime, truncate } from '@/utils/format'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/** 文档启用状态（ENABLED/DISABLED）：dict.ts 未收录，管理端就近定义 */
const DOC_STATUS_MAP: Record<string, { label: string; tag: TagType }> = {
  ENABLED: { label: '启用', tag: 'success' },
  DISABLED: { label: '停用', tag: 'info' },
}

// ======================================================================
// 知识入库 / 强制重建向量
// ======================================================================
const ingesting = ref(false)
const rebuilding = ref(false)

async function handleIngest(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将把后端 knowledge/ 目录下的文档同步进库，并做切分 + 向量化。已有分块的文档会跳过（幂等）。',
      '知识入库',
      { type: 'info', confirmButtonText: '开始入库', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }

  ingesting.value = true
  try {
    const result = await ingestKnowledge(false)
    ElMessage.success(`入库完成：文档 ${result.documents} 篇 / 分块 ${result.chunks} 个`)
    // 入库会把 knowledge/*.md 同步成文档，列表可能多了新文档，所以顺带刷新
    await loadDocuments()
  } catch {
    // 拦截器已提示
  } finally {
    ingesting.value = false
  }
}

async function handleRebuild(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '强制重建会忽略已有分块，对全部文档重新切分并重算向量。换了 embedding 模型（向量维度会变）之后必须用它，否则检索会因维度不一致而失败。耗时较长，确认继续？',
      '强制重建向量',
      { type: 'warning', confirmButtonText: '确认重建', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }

  rebuilding.value = true
  try {
    const result = await ingestKnowledge(true)
    ElMessage.success(`重建完成：文档 ${result.documents} 篇 / 分块 ${result.chunks} 个`)
    await loadDocuments()
  } catch {
    // 拦截器已提示
  } finally {
    rebuilding.value = false
  }
}

// ======================================================================
// 检索调试
// ======================================================================
const searching = ref(false)
/** 区分"还没检索"和"检索了但没命中"：两种情况空状态文案不一样 */
const searched = ref(false)
const hits = ref<KnowledgeSearchHit[]>([])

const searchForm = reactive({ query: '', topK: 4 })

async function handleSearch(): Promise<void> {
  const query = searchForm.query.trim()
  if (!query) {
    ElMessage.warning('请输入检索内容')
    return
  }

  searching.value = true
  try {
    const result = await searchKnowledge(query, searchForm.topK)
    hits.value = result.hits
    searched.value = true
  } finally {
    // 错误提示由 request 拦截器统一负责
    searching.value = false
  }
}

/** 相似度保留 4 位：RAG 里 0.79 和 0.80 往往就是"该不该信这条"的分界线 */
function formatScore(score: number): string {
  return Number(score).toFixed(4)
}

// ======================================================================
// 文档列表
// ======================================================================
const loading = ref(false)
const documents = ref<KnowledgeInfo[]>([])

const docQuery = reactive<{ keyword: string; category: string | undefined }>({
  keyword: '',
  category: undefined,
})
const docPagination = reactive({ current: 1, limit: 10, total: 0 })

/**
 * 后端没有分类字典接口（入库时 category = 文件名去后缀），
 * 所以把已加载文档出现过的分类累积成下拉选项，同时允许手输（allow-create），
 * 免得新分类第一次筛选不了。注意 category 是精确匹配，不是模糊匹配。
 */
const categoryOptions = ref<string[]>([])

async function loadDocuments(): Promise<void> {
  loading.value = true
  try {
    const result = await listKnowledge({
      limit: docPagination.limit,
      offset: (docPagination.current - 1) * docPagination.limit,
      keyword: docQuery.keyword.trim() || undefined,
      category: docQuery.category || undefined,
    })
    documents.value = result.items
    docPagination.total = result.total

    const seen = new Set(categoryOptions.value)
    result.items.forEach((item) => {
      if (item.category) seen.add(item.category)
    })
    categoryOptions.value = [...seen].sort()
  } finally {
    // 错误提示由 request 拦截器统一负责
    loading.value = false
  }
}

function searchDocuments(): void {
  docPagination.current = 1
  void loadDocuments()
}

function resetDocQuery(): void {
  docQuery.keyword = ''
  docQuery.category = undefined
  searchDocuments()
}

function handlePageChange(page: number): void {
  docPagination.current = page
  void loadDocuments()
}

function handleSizeChange(size: number): void {
  docPagination.limit = size
  docPagination.current = 1
  void loadDocuments()
}

// ======================================================================
// 新建 / 编辑文档
// ======================================================================
const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
/** null = 新建；有值 = 编辑该文档 */
const editingId = ref<number | null>(null)

const form = reactive({ title: '', content: '', category: '', source: '' })

const dialogTitle = computed(() =>
  editingId.value === null ? '新建知识文档' : `编辑知识文档 #${editingId.value}`,
)

const rules: FormRules = {
  title: [
    { required: true, message: '请输入文档标题', trigger: 'blur' },
    { max: 255, message: '标题最多 255 个字符', trigger: 'blur' },
  ],
  content: [
    { required: true, message: '请输入文档内容', trigger: 'blur' },
    // RAG 按段落/长度切块，太短的内容切不出有效分块，提前提醒
    { min: 20, message: '内容太短，检索效果会很差（建议至少 20 个字符）', trigger: 'blur' },
  ],
}

function openCreate(): void {
  editingId.value = null
  form.title = ''
  form.content = ''
  form.category = ''
  form.source = ''
  dialogVisible.value = true
  // 对话框关闭后 DOM 保留，上一次的校验红字会残留，打开时清一次
  void nextTick(() => formRef.value?.clearValidate())
}

function openEdit(row: KnowledgeInfo): void {
  editingId.value = row.document_id
  form.title = row.title
  form.content = row.content
  form.category = row.category ?? ''
  form.source = row.source ?? ''
  dialogVisible.value = true
  void nextTick(() => formRef.value?.clearValidate())
}

async function submitForm(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const title = form.title.trim()
    const content = form.content.trim()
    const category = form.category.trim()
    const source = form.source.trim()

    if (editingId.value === null) {
      await createKnowledge({
        title,
        content,
        category: category || undefined,
        source: source || undefined,
      })
      ElMessage.success('知识文档已创建，记得执行「知识入库」生成向量')
    } else {
      // 后端 KnowledgeUpdate 只接受 title/content/category/status：
      // source 表示"来源文件"，属于文档出处而不是内容，编辑时不提交（输入框里只读展示）。
      // 改了 content 后，后端会重新切分并重算该文档的向量。
      await updateKnowledge(editingId.value, { title, content, category: category || undefined })
      ElMessage.success('知识文档已更新，内容变更会重新切分向量')
    }

    dialogVisible.value = false
    await loadDocuments()
  } catch {
    // 失败原因已由拦截器提示，保持对话框打开让用户修改
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row: KnowledgeInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除知识文档「${row.title}」？删除后它立刻不再参与 RAG 检索，Agent 不会再引用这份内容。`,
      '删除知识文档',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }

  try {
    await deleteKnowledge(row.document_id)
    ElMessage.success('知识文档已删除')
    await loadDocuments()
  } catch {
    // 拦截器已提示
  }
}

onMounted(loadDocuments)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">知识库管理</h2>
      <div class="header-actions">
        <el-button type="primary" :loading="ingesting" @click="handleIngest">
          <el-icon><Upload /></el-icon>
          <span>知识入库（切分+向量化）</span>
        </el-button>
        <el-button :loading="rebuilding" @click="handleRebuild">
          <el-icon><RefreshRight /></el-icon>
          <span>强制重建向量</span>
        </el-button>
      </div>
    </div>

    <!-- 检索调试：调知识库时最常用的一块 -->
    <div class="page-card">
      <h3 class="page-card__title">检索调试</h3>

      <div class="toolbar">
        <el-input
          v-model="searchForm.query"
          placeholder="输入用户会问的问题，例如：七天无理由退货怎么算"
          clearable
          style="width: 380px"
          @keyup.enter="handleSearch"
        />
        <span class="text-secondary">召回条数</span>
        <el-input-number v-model="searchForm.topK" :min="1" :max="20" :precision="0" />
        <el-button type="primary" :loading="searching" @click="handleSearch">检索</el-button>
      </div>

      <el-table v-loading="searching" :data="hits" border size="small">
        <el-table-column type="expand">
          <template #default="{ row }">
            <!-- 展开看完整片段：判断命中是否真的对题，只看截断文本不够 -->
            <div class="pre-wrap hit__full">{{ row.content }}</div>
          </template>
        </el-table-column>
        <el-table-column label="序号" type="index" width="70" align="center" />
        <el-table-column label="标题" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title || '—' }}</template>
        </el-table-column>
        <el-table-column label="来源" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.source || '—' }}</template>
        </el-table-column>
        <el-table-column label="分类" width="150">
          <template #default="{ row }">{{ row.category || '—' }}</template>
        </el-table-column>
        <el-table-column label="相似度" width="100" align="right">
          <template #default="{ row }">{{ formatScore(row.score) }}</template>
        </el-table-column>
        <el-table-column label="命中片段" min-width="260">
          <template #default="{ row }">
            <span class="text-ellipsis">{{ truncate(row.content, 60) }}</span>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty
            :description="
              searched ? '没有命中任何知识片段，检查是否已入库' : '输入问题后点击「检索」'
            "
          />
        </template>
      </el-table>
    </div>

    <!-- 文档列表 -->
    <div class="page-card">
      <div class="page-header">
        <h3 class="page-card__title">知识文档</h3>
        <el-button type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>
          <span>新建文档</span>
        </el-button>
      </div>

      <div class="toolbar">
        <el-input
          v-model="docQuery.keyword"
          placeholder="按标题搜索"
          clearable
          style="width: 240px"
          @keyup.enter="searchDocuments"
          @clear="searchDocuments"
        />
        <el-select
          v-model="docQuery.category"
          placeholder="全部分类"
          clearable
          filterable
          allow-create
          default-first-option
          style="width: 180px"
          @change="searchDocuments"
        >
          <el-option v-for="item in categoryOptions" :key="item" :label="item" :value="item" />
        </el-select>
        <el-button type="primary" @click="searchDocuments">查询</el-button>
        <el-button @click="resetDocQuery">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="documents" border stripe>
        <el-table-column prop="document_id" label="ID" width="80" />
        <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="分类" width="150">
          <template #default="{ row }">{{ row.category || '—' }}</template>
        </el-table-column>
        <el-table-column label="来源" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.source || '—' }}</template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="90" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="DOC_STATUS_MAP[row.status]?.tag ?? 'info'" disable-transitions>
              {{ DOC_STATUS_MAP[row.status]?.label ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="内容预览" min-width="180">
          <!-- 知识正文很长，列表里只给截断预览，全文在"编辑内容"对话框里看 -->
          <template #default="{ row }">
            <span class="text-ellipsis">{{ truncate(row.content, 40) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑内容</el-button>
            <el-button link type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无知识文档，先新建或执行「知识入库」" />
        </template>
      </el-table>

      <div class="pagination">
        <el-pagination
          :current-page="docPagination.current"
          :page-size="docPagination.limit"
          :page-sizes="[10, 20, 50]"
          :total="docPagination.total"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogTitle"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="标题" prop="title">
          <el-input v-model="form.title" placeholder="例如：七天无理由退货规则" clearable />
        </el-form-item>
        <el-form-item label="分类" prop="category">
          <el-select
            v-model="form.category"
            placeholder="按业务域分类，便于检索时过滤（如 refund_policy）"
            clearable
            filterable
            allow-create
            default-first-option
            style="width: 100%"
          >
            <el-option v-for="item in categoryOptions" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源" prop="source">
          <!-- 编辑时禁用：后端更新接口不接受 source（来源是文档出处，不随内容编辑改变） -->
          <el-input
            v-model="form.source"
            :disabled="editingId !== null"
            placeholder="来源文件名，如 refund_policy.md"
            clearable
          />
        </el-form-item>
        <el-form-item label="内容" prop="content">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            placeholder="支持 Markdown；入库时会按标题/段落切分成多个分块"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header-actions {
  display: flex;
  gap: 12px;
}

.hit__full {
  padding: 4px 8px;
  color: var(--text-regular);
  font-size: 13px;
}

.page-header .page-card__title {
  margin: 0;
}
</style>
