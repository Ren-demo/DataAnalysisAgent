<template>
  <div class="sql-result">
    <template v-if="showSql">
      <div class="section-title">生成的 SQL</div>
      <pre class="sql-block">{{ result.sql }}</pre>
    </template>

    <div v-if="result.explanation" class="analysis-section">
      <div class="section-title">回答</div>
      <MarkdownText :content="result.explanation" />
    </div>

    <div v-if="result.analysis" class="analysis-section">
      <div class="section-title">分析说明</div>
      <MarkdownText :content="result.analysis" />
    </div>

    <div class="section-title">查询结果（{{ result.row_count }} 条）</div>
    <div class="table-card">
      <el-table :data="tableData" stripe size="small" max-height="360">
        <el-table-column
          v-for="(col, idx) in result.columns"
          :key="idx"
          :prop="colKeys[idx]"
          :label="col"
          min-width="120"
        />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QueryResponse } from '../types'
import MarkdownText from './MarkdownText.vue'

const props = withDefaults(defineProps<{ result: QueryResponse; showSql?: boolean }>(), {
  showSql: true,
})

// 行对象一律用**列下标**作键（c0/c1/…），不用列名：
// 1) JOIN 结果常有同名列（两表都有 name），用列名作键会后写覆盖先写 —— 表格显示错误数值；
// 2) Element Plus 把 `prop` 当**路径**解析，列名含 `.` 时（如 `a.b`）取不到值，显示空白。
// 列名只作为 label 展示，不参与取值。
const colKeys = computed(() => props.result.columns.map((_, i) => `c${i}`))

const tableData = computed<Record<string, string | number | null>[]>(() =>
  props.result.rows.map((row) => {
    const obj: Record<string, string | number | null> = {}
    colKeys.value.forEach((key, i) => {
      obj[key] = row[i] ?? null
    })
    return obj
  }),
)
</script>

<style scoped>
.sql-result {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.section-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text-1);
}
.table-card {
  background: var(--app-bg-card);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  overflow: hidden;
}
.table-card :deep(.el-table) {
  --el-table-bg-color: var(--app-bg-card);
  --el-table-tr-bg-color: var(--app-bg-card);
  --el-table-header-bg-color: var(--app-bg-card);
  --el-table-border-color: var(--app-border);
}
.sql-block {
  background: var(--app-bg-code);
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 12px 14px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  overflow-x: auto;
}
.analysis-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
</style>
