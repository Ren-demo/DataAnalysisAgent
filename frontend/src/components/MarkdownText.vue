<template>
  <div class="markdown" v-html="html"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = defineProps<{ content: string }>()

const html = computed(() => DOMPurify.sanitize(marked.parse(props.content ?? '') as string))
</script>

<style scoped>
.markdown {
  line-height: 1.7;
  font-size: 15px;
  color: var(--app-text-1);
  word-break: break-word;
}
.markdown :deep(p) {
  margin: 0 0 10px;
}
.markdown :deep(ul),
.markdown :deep(ol) {
  margin: 0 0 10px;
  padding-left: 1.4em;
}
.markdown :deep(li) {
  margin: 4px 0;
}
.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4) {
  margin: 16px 0 8px;
  font-weight: 600;
  line-height: 1.4;
}
.markdown :deep(code) {
  background: var(--app-bg-code);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 1px 5px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 13px;
}
.markdown :deep(pre) {
  background: var(--app-bg-code);
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 12px 14px;
  overflow-x: auto;
}
.markdown :deep(pre code) {
  background: none;
  border: none;
  padding: 0;
}
.markdown :deep(blockquote) {
  margin: 10px 0;
  padding: 4px 12px;
  border-left: 3px solid var(--app-border);
  color: var(--app-text-2);
}
.markdown :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  max-width: 100%;
  overflow-x: auto;
  display: block;
}
.markdown :deep(th),
.markdown :deep(td) {
  border: 1px solid var(--app-border);
  padding: 6px 10px;
  text-align: left;
}
.markdown :deep(th) {
  background: var(--app-bg-code);
  font-weight: 600;
}
.markdown :deep(a) {
  color: var(--el-color-primary);
}
</style>
