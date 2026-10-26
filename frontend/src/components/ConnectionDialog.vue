<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? '编辑连接' : '连接数据库'"
    width="640px"
    :close-on-click-modal="false"
    @update:model-value="onVisibleChange"
    @open="onOpen"
  >
    <!-- 已保存连接列表 -->
    <div v-if="connections.length" class="saved-list">
      <div class="saved-title">已保存的连接</div>
      <el-table :data="connections" size="small">
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">{{ row.type === 'postgresql' ? 'PostgreSQL' : 'MySQL' }}</template>
        </el-table-column>
        <el-table-column prop="host" label="主机" min-width="120" />
        <el-table-column prop="database" label="库名" min-width="100" />
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="startEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-divider v-if="connections.length" />

    <el-form label-width="88px" @submit.prevent>
      <el-form-item label="名称" required>
        <el-input v-model="form.name" placeholder="例如：生产库" />
      </el-form-item>
      <el-form-item label="类型">
        <el-radio-group v-model="form.type">
          <el-radio value="mysql">MySQL</el-radio>
          <el-radio value="postgresql">PostgreSQL</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="主机 IP" required>
        <el-input v-model="form.host" placeholder="例如：127.0.0.1" />
      </el-form-item>
      <el-form-item label="端口">
        <el-input-number v-model="form.port" :min="1" :max="65535" controls-position="right" style="width: 160px" />
      </el-form-item>
      <el-form-item label="数据库名" required>
        <el-input v-model="form.database" placeholder="例如：sales" />
      </el-form-item>
      <el-form-item label="用户名" required>
        <el-input v-model="form.user" placeholder="例如：root" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input v-model="form.password" type="password" show-password placeholder="可留空" />
        <div v-if="isEdit" class="field-hint">留空则保持原密码不变</div>
      </el-form-item>
    </el-form>

    <!-- 连接结果 / 错误提示 -->
    <el-alert
      v-if="errorMsg"
      :title="errorMsg"
      type="error"
      :closable="false"
      show-icon
      class="result-alert"
    />
    <el-alert
      v-else-if="savedOk"
      title="连接已保存"
      type="success"
      :closable="false"
      show-icon
      class="result-alert"
    />

    <!-- 表结构展示 -->
    <div v-if="tables.length" class="schema-section">
      <div class="schema-title">表结构（{{ tables.length }} 张表）</div>
      <el-collapse>
        <el-collapse-item v-for="t in tables" :key="t.name" :title="t.name">
          <div class="col-chips">
            <el-tag v-for="c in t.columns" :key="c.name" size="small" class="col-tag" type="info">
              {{ c.name }} <span class="col-type">{{ c.type }}</span>
            </el-tag>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>
    <el-empty
      v-else-if="savedOk || tested"
      description="连接成功，但该数据库中暂无表。"
      :image-size="60"
    />

    <template #footer>
      <div class="dialog-footer">
        <el-button @click="emit('update:visible', false)">取消</el-button>
        <el-button :loading="testing" @click="onTest">测试连接</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">
          {{ isEdit ? '保存修改' : '保存并连接' }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createConnection,
  deleteConnection,
  listConnections,
  testConnection,
  updateConnection,
} from '../api'
import type { ConnectionCreate, ConnectionCreateResult, ConnectionInfo, TableInfo } from '../types'

const props = defineProps<{ visible: boolean; editTargetId: string | null }>()
const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
  (e: 'saved', r: ConnectionCreateResult): void
}>()

const form = reactive<ConnectionCreate>({
  name: '',
  type: 'mysql',
  host: '',
  port: 3306,
  database: '',
  user: '',
  password: '',
})

const connections = ref<ConnectionInfo[]>([])
const editingId = ref<string | null>(null)
const tables = ref<TableInfo[]>([])
const testing = ref(false)
const saving = ref(false)
const errorMsg = ref<string | null>(null)
const savedOk = ref(false)
const tested = ref(false)

const isEdit = computed(() => editingId.value !== null)

function onVisibleChange(v: boolean) {
  emit('update:visible', v)
}

watch(
  () => form.type,
  (t) => {
    if (!isEdit.value) form.port = t === 'postgresql' ? 5432 : 3306
  },
)

function resetForm() {
  form.name = ''
  form.type = 'mysql'
  form.host = ''
  form.port = 3306
  form.database = ''
  form.user = ''
  form.password = ''
  editingId.value = null
  tables.value = []
  errorMsg.value = null
  savedOk.value = false
  tested.value = false
}

async function onOpen() {
  resetForm()
  await refreshConnections()
  if (props.editTargetId) {
    const target = connections.value.find((c) => c.id === props.editTargetId)
    if (target) startEdit(target)
  }
}

async function refreshConnections() {
  try {
    connections.value = await listConnections()
  } catch {
    connections.value = []
  }
}

function startEdit(conn: ConnectionInfo) {
  editingId.value = conn.id
  form.name = conn.name
  form.type = conn.type
  form.host = conn.host
  form.port = conn.port
  form.database = conn.database
  form.user = conn.user
  form.password = ''
  tables.value = []
  errorMsg.value = null
  savedOk.value = false
  tested.value = false
}

function validate(): string | null {
  if (!form.name.trim()) return '请填写连接名称。'
  if (!form.host.trim()) return '请填写主机 IP。'
  if (!form.database.trim()) return '请填写数据库名。'
  if (!form.user.trim()) return '请填写用户名。'
  if (!form.port) return '请填写端口。'
  return null
}

async function onTest() {
  const msg = validate()
  if (msg) {
    errorMsg.value = msg
    return
  }
  testing.value = true
  errorMsg.value = null
  savedOk.value = false
  try {
    const res = await testConnection({ ...form })
    if (res.ok) {
      tables.value = res.tables
      tested.value = true
      ElMessage.success('连接成功')
    } else {
      tables.value = []
      errorMsg.value = res.error || '连接失败。'
    }
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '连接失败。'
  } finally {
    testing.value = false
  }
}

async function onSave() {
  const msg = validate()
  if (msg) {
    errorMsg.value = msg
    return
  }
  saving.value = true
  errorMsg.value = null
  try {
    const res = isEdit.value
      ? await updateConnection(editingId.value as string, { ...form })
      : await createConnection({ ...form })
    if (res.error) {
      tables.value = []
      savedOk.value = false
      errorMsg.value = res.error
      return
    }
    tables.value = res.tables
    savedOk.value = true
    editingId.value = res.id
    await refreshConnections()
    ElMessage.success('连接已保存')
    emit('saved', res)
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '保存失败。'
  } finally {
    saving.value = false
  }
}

async function onDelete(conn: ConnectionInfo) {
  try {
    await ElMessageBox.confirm(`确定删除连接「${conn.name}」？`, '删除连接', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteConnection(conn.id)
    await refreshConnections()
    if (editingId.value === conn.id) resetForm()
    ElMessage.success('已删除')
  } catch {
    ElMessage.error('删除失败，请稍后重试。')
  }
}
</script>

<style scoped>
.saved-list {
  margin-bottom: 4px;
}
.saved-title {
  font-size: 13px;
  color: var(--app-text-2);
  margin-bottom: 8px;
}
.field-hint {
  font-size: 12px;
  color: var(--app-text-3);
  margin-top: 4px;
  width: 100%;
}
.result-alert {
  margin-top: 8px;
}
.schema-section {
  margin-top: 12px;
}
.schema-title {
  font-size: 13px;
  color: var(--app-text-2);
  margin-bottom: 8px;
}
.col-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 0;
}
.col-type {
  opacity: 0.7;
  margin-left: 4px;
}
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
