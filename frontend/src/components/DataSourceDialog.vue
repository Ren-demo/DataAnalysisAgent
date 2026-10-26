<template>
  <el-dialog
    :model-value="visible"
    title="管理数据源"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="onVisibleChange"
    @open="onOpen"
  >
    <el-table v-if="dataSources.length" :data="dataSources" size="small">
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column label="类型" width="140">
        <template #default="{ row }">
          <el-tag :type="typeTagType(row.type)" size="small">{{ typeLabel(row.type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-else description="暂无数据源，请先上传文件或连接数据库。" :image-size="80" />

    <template #footer>
      <div class="dialog-footer">
        <el-button @click="emit('update:visible', false)">关闭</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteDataSource, listDataSources } from '../api'
import type { DataSourceInfo } from '../types'

defineProps<{ visible: boolean }>()
const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
  (e: 'deleted', id: string): void
}>()

const dataSources = ref<DataSourceInfo[]>([])

function onVisibleChange(v: boolean) {
  emit('update:visible', v)
}

async function onOpen() {
  await refreshDataSources()
}

async function refreshDataSources() {
  try {
    dataSources.value = await listDataSources()
  } catch {
    dataSources.value = []
  }
}

function typeLabel(type: string): string {
  switch (type) {
    case 'upload':
      return '上传文件'
    case 'doc':
      return '文档'
    case 'mysql':
      return 'MySQL 连接'
    case 'postgresql':
      return 'PostgreSQL 连接'
    default:
      return type
  }
}

function typeTagType(type: string): 'success' | 'warning' | 'info' {
  if (type === 'upload') return 'success'
  if (type === 'doc') return 'warning'
  return 'info'
}

async function onDelete(ds: DataSourceInfo) {
  try {
    await ElMessageBox.confirm(`确定删除数据源「${ds.name}」？此操作不可恢复。`, '删除数据源', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  try {
    await deleteDataSource(ds.id)
    await refreshDataSources()
    emit('deleted', ds.id)
    ElMessage.success('已删除')
  } catch {
    ElMessage.error('删除失败，请稍后重试。')
  }
}
</script>

<style scoped>
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
