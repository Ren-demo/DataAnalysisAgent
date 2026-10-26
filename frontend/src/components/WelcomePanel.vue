<template>
  <div class="welcome">
    <h1 class="greeting">你好，我是智能数据分析助手</h1>
    <p class="subtitle">用自然语言提问，自动生成 SQL 并可视化结果</p>

    <div class="suggestions">
      <button
        v-for="q in suggestions"
        :key="q"
        class="suggestion-chip"
        type="button"
        @click="emit('ask', q)"
      >
        {{ q }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
const suggestions = [
  '统计各分类的数量',
  '各分类的合计值',
  '各分类占总量的比例',
  '数值最高的前5条记录',
]

const emit = defineEmits<{ (e: 'ask', question: string): void }>()
</script>

<style scoped>
.welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 80px 16px 40px;
}
.greeting {
  margin: 0 0 12px;
  font-size: 32px;
  font-weight: 700;
  color: var(--app-text-1);
}
.subtitle {
  margin: 0 0 36px;
  font-size: 16px;
  color: var(--app-text-2);
}
.suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 12px;
  max-width: 640px;
}
.suggestion-chip {
  padding: 10px 16px;
  font-size: 15px;
  color: var(--app-text-1);
  background: var(--app-bg-card);
  border: 1px solid var(--app-border);
  border-radius: 999px;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
}
.suggestion-chip:hover {
  background: var(--app-bg-hover);
  border-color: var(--app-border-strong);
  transform: translateY(-1px);
}
</style>
