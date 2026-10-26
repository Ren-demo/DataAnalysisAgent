<template>
  <aside class="sidebar" :class="{ collapsed }">
    <div class="sidebar-head">
      <el-button class="new-chat" type="primary" @click="emit('new')">
        <el-icon><Plus /></el-icon>
        <span>新对话</span>
      </el-button>
    </div>

    <div class="sidebar-body">
      <div v-if="sessions.length === 0" class="session-empty">暂无历史会话</div>
      <template v-for="group in groups" :key="group.key">
        <div class="group-label">{{ group.key }}</div>
        <div
          v-for="s in group.items"
          :key="s.session_id"
          class="session-item"
          :class="{ active: s.session_id === activeSessionId }"
          @click="emit('select', s)"
        >
          <span class="session-title">{{ s.title }}</span>
          <el-icon class="delete-btn" @click.stop="emit('delete', s)"><Delete /></el-icon>
        </div>
      </template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'
import type { SessionSummary } from '../types'

const props = defineProps<{
  sessions: SessionSummary[]
  activeSessionId: string
  collapsed: boolean
}>()

const emit = defineEmits<{
  (e: 'new'): void
  (e: 'select', session: SessionSummary): void
  (e: 'delete', session: SessionSummary): void
}>()

const GROUP_KEYS = ['今天', '昨天', '过去 7 天', '过去 30 天', '更早'] as const
type GroupKey = (typeof GROUP_KEYS)[number]

function groupKeyOf(iso: string | null): GroupKey {
  if (!iso) return '更早'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '更早'
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  if (d >= startOfToday) return '今天'
  if (d >= new Date(startOfToday.getTime() - 86400000)) return '昨天'
  if (d >= new Date(startOfToday.getTime() - 6 * 86400000)) return '过去 7 天'
  if (d >= new Date(startOfToday.getTime() - 29 * 86400000)) return '过去 30 天'
  return '更早'
}

const groups = computed(() => {
  const map = new Map<GroupKey, SessionSummary[]>()
  for (const s of props.sessions) {
    const key = groupKeyOf(s.updated_at)
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(s)
  }
  return GROUP_KEYS.filter((k) => map.has(k)).map((key) => ({
    key,
    items: map.get(key)!,
  }))
})
</script>

<style scoped>
.sidebar {
  flex-shrink: 0;
  width: 260px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--app-bg-sidebar, var(--app-bg-page));
  border-right: 1px solid var(--app-border);
  overflow: hidden;
  transition: width 0.2s ease;
}
.sidebar.collapsed {
  width: 0;
  border-right: none;
}
.sidebar-head {
  padding: 12px 12px 8px;
}
.new-chat {
  width: 100%;
  justify-content: center;
  display: flex;
  align-items: center;
  gap: 6px;
}
.sidebar-body {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 16px;
}
.session-empty {
  padding: 24px 0;
  text-align: center;
  color: var(--app-text-3);
  font-size: 14px;
}
.group-label {
  padding: 12px 8px 6px;
  font-size: 12px;
  color: var(--app-text-3);
}
.session-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.15s ease;
}
.session-item:hover {
  background: var(--app-bg-hover);
}
.session-item.active {
  background: var(--app-bg-hover);
}
.session-title {
  flex: 1;
  min-width: 0;
  display: block;
  font-size: 14px;
  color: var(--app-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.delete-btn {
  display: none;
  flex-shrink: 0;
  font-size: 14px;
  color: var(--app-text-3);
}
.session-item:hover .delete-btn {
  display: inline-flex;
}
.delete-btn:hover {
  color: var(--el-color-danger);
}
</style>
