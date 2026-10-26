<template>
  <div class="message-list">
    <div v-for="(msg, i) in messages" :key="i" class="message" :class="msg.role">
      <div v-if="msg.role === 'user'" class="user-bubble">{{ msg.content }}</div>

      <template v-else>
        <div class="avatar">
          <el-icon><DataAnalysis /></el-icon>
        </div>
        <div class="assistant-body">
          <div v-if="msg.loading" class="loading">
            <span class="typing-dots"><span></span><span></span><span></span></span>
            <span>{{ loadingText(msg) }}</span>
            <!-- 后端 progress 事件：分析 agent 每轮工具调用的实时步骤 -->
            <span v-if="msg.progress" class="progress-text">{{ msg.progress }}</span>
          </div>

          <div v-if="msg.content" class="stopped-hint">{{ msg.content }}</div>

          <!-- 闲聊：流式文本 / 最终回答（markdown 渲染） -->
          <MarkdownText
            v-if="msg.kind === 'chat'"
            :content="msg.streamingText || msg.answer || ''"
          />

          <!-- RAG 检索出处引用（仅文档问答带 sources 时展示） -->
          <div v-if="msg.kind === 'chat' && msg.sources?.length" class="sources-block">
            <div class="section-title">参考来源</div>
            <ul class="source-list">
              <li v-for="(s, i) in msg.sources" :key="i">
                <span class="source-title">{{ s.title }}</span>
                <span class="source-block">片段 {{ s.block_id }}</span>
              </li>
            </ul>
          </div>

          <!-- SQL：流式累积优先，否则成功结果里的 sql -->
          <div v-if="msg.kind !== 'chat' && sqlText(msg)" class="sql-section">
            <div class="section-title">生成的 SQL</div>
            <pre class="sql-block">{{ sqlText(msg) }}</pre>
          </div>

          <template v-if="!msg.loading && msg.result">
            <div v-if="msg.result.error" class="error-block">
              <el-alert
                :title="msg.result.error"
                :type="alertType(msg.result.error_code)"
                :closable="false"
                show-icon
              />
            </div>
            <template v-else-if="msg.result.kind === 'query'">
              <SqlResult :result="msg.result" :show-sql="false" />
              <ChartCard :result="msg.result" />
            </template>
          </template>

          <!--
            报告阶段的占位：result 事件已把 loading 置 false（上面的 loading 块不再渲染），
            而报告 agent 还要串行跑若干轮 LLM 往返 —— 不单独开位的话，这段界面全程静止。
          -->
          <div v-if="msg.reportPending && !msg.report" class="loading report-pending">
            <span class="typing-dots"><span></span><span></span><span></span></span>
            <span>正在生成决策报告…</span>
            <span v-if="msg.reportProgress" class="progress-text">{{ msg.reportProgress }}</span>
          </div>

          <ReportCard v-if="msg.report" :report="msg.report" />
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { DataAnalysis } from '@element-plus/icons-vue'
import SqlResult from './SqlResult.vue'
import ChartCard from './ChartCard.vue'
import ReportCard from './ReportCard.vue'
import MarkdownText from './MarkdownText.vue'
import type { Message } from '../types'

defineProps<{ messages: Message[] }>()

/**
 * 等待期间的提示文案，按后端透传的阶段标签（msg.phase）区分。
 *
 * 兜底必须是中性的「正在思考…」：意图分类是一次 2~5s 的 LLM 往返，phase 在 intent
 * 事件到达前一直是 undefined，早前按「非 chat 即 SQL」兜底，导致**包括文档问答在内**
 * 的每一次提问前几秒都在显示「正在生成 SQL…」。
 */
function loadingText(msg: Message): string {
  switch (msg.phase) {
    case 'query':
      return '正在生成 SQL…'
    case 'analysis':
      // 统计链路一次 SQL 都不生成（走 pandas 统计算子），不能沿用 query 的文案
      return '正在做数据分析…'
    case 'retrieval':
      return '正在检索文档…'
    case 'schema':
      return '正在读取表结构…'
    default:
      return '正在思考…'
  }
}

function sqlText(msg: Message): string {
  return msg.streamingSql || msg.result?.sql || ''
}

function alertType(code: string | null): 'error' | 'warning' | 'info' {
  if (code === 'NO_API_KEY') return 'warning'
  if (code === 'SQL_ERROR') return 'info'
  return 'error'
}
</script>

<style scoped>
.message-list {
  display: flex;
  flex-direction: column;
  gap: 28px;
}
.message {
  display: flex;
}
.message.user {
  justify-content: flex-end;
}
.message.assistant {
  justify-content: flex-start;
  gap: 12px;
}
.user-bubble {
  max-width: 75%;
  background: var(--app-user-bubble);
  color: var(--app-user-bubble-text);
  border-radius: 16px;
  padding: 10px 16px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.avatar {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 18px;
  background: linear-gradient(135deg, #4d6bfe, #38bdf8);
}
.assistant-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  color: var(--app-text-1);
}
.loading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--app-text-2);
  font-size: 15px;
}
.typing-dots {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.typing-dots span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--app-text-3);
  animation: bounce 1.2s infinite ease-in-out;
}
.typing-dots span:nth-child(2) {
  animation-delay: 0.15s;
}
.typing-dots span:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes bounce {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
}
.progress-text {
  color: var(--app-text-3);
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.report-pending {
  margin-top: -2px;
}
.stopped-hint {
  font-size: 15px;
  color: var(--app-text-3);
}
.sql-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.section-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text-1);
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
.sources-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.source-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.source-list li {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.source-title {
  color: var(--app-text-1);
  font-weight: 500;
  background: var(--app-bg-code);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 3px 8px;
}
.source-block {
  color: var(--app-text-3);
}
.error-block {
  width: 100%;
}
</style>
