<template>
  <div v-if="hasContent" class="report-card">
    <div class="report-header">
      <el-icon class="report-icon"><DataAnalysis /></el-icon>
      <span class="report-title">{{ title }}</span>
      <el-tag v-if="confidence" :type="confidenceTagType" size="small" effect="plain">
        {{ confidenceLabel }}
      </el-tag>
    </div>

    <div v-if="summary" class="report-section">
      <div class="section-title">核心结论</div>
      <div class="report-summary">
        <MarkdownText :content="summary" />
      </div>
    </div>

    <div v-if="findings.length" class="report-section">
      <div class="section-title">关键发现</div>
      <ul class="finding-list">
        <li v-for="(f, i) in findings" :key="i">{{ f }}</li>
      </ul>
    </div>

    <div v-if="analysis" class="report-section">
      <div class="section-title">详细分析</div>
      <MarkdownText :content="analysis" />
    </div>

    <div v-if="recommendations.length" class="report-section">
      <div class="section-title">建议</div>
      <ol class="reco-list">
        <li v-for="(r, i) in recommendations" :key="i">{{ r }}</li>
      </ol>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { DataAnalysis } from '@element-plus/icons-vue'
import MarkdownText from './MarkdownText.vue'
import type { ReportResult } from '../types'

const props = defineProps<{ report: ReportResult }>()

// 空值守卫：历史数据里字段可能为空，逐块判断，不为空字段渲染标题
const title = computed(() => props.report.title?.trim() || '数据分析报告')
const summary = computed(() => (props.report.summary ?? '').trim())
const findings = computed(() =>
  (props.report.key_findings ?? []).map((f) => f.trim()).filter(Boolean),
)
const analysis = computed(() => (props.report.analysis ?? '').trim())
const recommendations = computed(() =>
  (props.report.recommendations ?? []).map((r) => r.trim()).filter(Boolean),
)

const hasContent = computed(
  () =>
    !!summary.value ||
    findings.value.length > 0 ||
    !!analysis.value ||
    recommendations.value.length > 0,
)

const confidence = computed(() => props.report.confidence ?? null)

const confidenceLabel = computed(() => {
  if (confidence.value === 'high') return '高置信度'
  if (confidence.value === 'medium') return '中置信度'
  if (confidence.value === 'low') return '低置信度'
  return ''
})

const confidenceTagType = computed(() => {
  if (confidence.value === 'high') return 'success'
  if (confidence.value === 'medium') return 'warning'
  if (confidence.value === 'low') return 'danger'
  return 'info'
})
</script>

<style scoped>
.report-card {
  background: var(--app-bg-card);
  border: 1px solid var(--app-border);
  border-left: 3px solid var(--el-color-primary);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.report-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.report-icon {
  color: var(--el-color-primary);
  font-size: 18px;
}
.report-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--app-text-1);
  flex: 1;
}
.report-summary {
  background: var(--app-bg-code);
  border-left: 2px solid var(--el-color-primary);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 15px;
  line-height: 1.7;
  color: var(--app-text-1);
}
/* MarkdownText 自带段落间距，结论块内收掉多余空白 */
.report-summary :deep(p) {
  margin: 0;
}
.report-summary :deep(p + p) {
  margin-top: 8px;
}
.report-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.section-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text-1);
}
.finding-list,
.reco-list {
  margin: 0;
  padding-left: 1.4em;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.finding-list li,
.reco-list li {
  font-size: 14px;
  line-height: 1.7;
  color: var(--app-text-2);
}
</style>
