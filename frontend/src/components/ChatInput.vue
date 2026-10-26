<template>
  <div class="composer-card">
    <el-input
      v-model="text"
      type="textarea"
      :autosize="{ minRows: 1, maxRows: 8 }"
      resize="none"
      placeholder="输入你的数据问题，例如：统计各分类的数量"
      @keydown="onKeydown"
    />

    <div class="composer-footer">
      <span class="hint">Enter 发送 · Shift+Enter 换行</span>
      <el-button
        v-if="loading"
        type="primary"
        circle
        aria-label="停止生成"
        @click="emit('stop')"
      >
        <el-icon><VideoPause /></el-icon>
      </el-button>
      <el-button
        v-else
        type="primary"
        circle
        :disabled="!canSend"
        aria-label="发送"
        @click="submit"
      >
        <el-icon><Promotion /></el-icon>
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Promotion, VideoPause } from '@element-plus/icons-vue'

const props = defineProps<{ loading: boolean }>()
const emit = defineEmits<{ (e: 'submit', text: string): void; (e: 'stop'): void }>()

const text = ref('')
const canSend = computed(() => text.value.trim().length > 0)

function onKeydown(e: KeyboardEvent) {
  // isComposing：防止中文输入法确认候选词时误发送
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return
  e.preventDefault()
  submit()
}

function submit() {
  const value = text.value.trim()
  if (!value || props.loading) return
  emit('submit', value)
  text.value = ''
}
</script>

<style scoped>
.composer-card {
  display: flex;
  flex-direction: column;
  background: var(--app-bg-input);
  border: 1px solid var(--app-border);
  border-radius: 16px;
  box-shadow: var(--app-shadow-input);
  padding: 14px 16px 12px;
  transition: border-color 0.15s ease;
}
.composer-card:focus-within {
  border-color: var(--app-border-strong);
}
.composer-card :deep(.el-textarea__inner) {
  border: none;
  box-shadow: none;
  background: transparent;
  padding: 4px 6px;
  font-size: 17px;
  line-height: 1.6;
  color: var(--app-text-1);
  resize: none;
}
.composer-card :deep(.el-textarea__inner::placeholder) {
  color: var(--app-text-3);
}
.composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 4px;
}
.hint {
  font-size: 13px;
  color: var(--app-text-3);
}
</style>
