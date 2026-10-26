<template>
  <div class="home">
    <SessionSidebar
      :sessions="sessions"
      :active-session-id="sessionId"
      :collapsed="sidebarCollapsed"
      @new="onNewConversation"
      @select="onSelectSession"
      @delete="onDeleteSession"
    />

    <div class="main">
      <header class="topbar">
        <div class="brand">
          <el-button circle aria-label="折叠/展开侧栏" @click="toggleSidebar">
            <el-icon><Expand v-if="sidebarCollapsed" /><Fold v-else /></el-icon>
          </el-button>
          <span class="brand-name">智能数据分析</span>
        </div>

        <div class="topbar-actions">
          <el-select
            v-model="dataSourceId"
            placeholder="选择数据源"
            style="width: 250px"
          >
            <el-option v-for="ds in dataSources" :key="ds.id" :label="ds.name" :value="ds.id" />
          </el-select>

          <el-button @click="showDataSourceDialog = true">
            <el-icon><Setting /></el-icon>
            <span>管理</span>
          </el-button>

          <el-upload :show-file-list="false" accept=".csv,.xlsx,.xls,.md,.txt,.pdf,.docx" :before-upload="onUpload">
            <el-button :loading="uploading">
              <el-icon><Upload /></el-icon>
              <span>上传</span>
            </el-button>
          </el-upload>

          <el-button @click="openConnectDialog">
            <el-icon><Connection /></el-icon>
            <span>连接数据库</span>
          </el-button>

          <el-dropdown trigger="click" @command="onThemeCommand">
            <el-button circle aria-label="切换主题">
              <el-icon>
                <Moon v-if="isDark" />
                <Sunny v-else />
              </el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="system" :disabled="preference === 'system'">
                  <el-icon><Monitor /></el-icon>跟随系统
                </el-dropdown-item>
                <el-dropdown-item command="light" :disabled="preference === 'light'">
                  <el-icon><Sunny /></el-icon>浅色
                </el-dropdown-item>
                <el-dropdown-item command="dark" :disabled="preference === 'dark'">
                  <el-icon><Moon /></el-icon>深色
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main ref="scrollEl" class="chat-scroll">
        <div class="chat-column">
          <WelcomePanel v-if="messages.length === 0" @ask="onSubmit" />
          <MessageList v-else :messages="messages" />
        </div>
      </main>

      <footer class="composer">
        <div class="composer-inner">
          <ChatInput :loading="loading" @submit="onSubmit" @stop="onStop" />
        </div>
      </footer>
    </div>

    <ConnectionDialog
      v-model:visible="showConnectionDialog"
      :edit-target-id="editTargetId"
      @saved="onConnectionSaved"
    />

    <DataSourceDialog v-model:visible="showDataSourceDialog" @deleted="onDataSourceDeleted" />
  </div>
</template>

<script setup lang="ts">
import { h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElButton, ElMessageBox, ElNotification } from 'element-plus'
import { Connection, DataAnalysis, Expand, Fold, Monitor, Moon, Setting, Sunny, Upload } from '@element-plus/icons-vue'
import ChatInput from '../components/ChatInput.vue'
import ConnectionDialog from '../components/ConnectionDialog.vue'
import DataSourceDialog from '../components/DataSourceDialog.vue'
import MessageList from '../components/MessageList.vue'
import SessionSidebar from '../components/SessionSidebar.vue'
import WelcomePanel from '../components/WelcomePanel.vue'
import { useDark } from '../composables/useDark'
import type { ThemePreference } from '../composables/useDark'
import { StreamError, chatStream, deleteSession, getSession, listDataSources, listSessions, uploadDataSource } from '../api'
import type { ConnectionCreateResult, DataSourceInfo, HistoryMessage, Message, QueryResponse, SessionMessage, SessionSummary } from '../types'

const messages = ref<Message[]>([])
const loading = ref(false)
const dataSources = ref<DataSourceInfo[]>([])
const dataSourceId = ref('')
const uploading = ref(false)
const showConnectionDialog = ref(false)
const showDataSourceDialog = ref(false)
const editTargetId = ref<string | null>(null)
const abortController = ref<AbortController | null>(null)
const scrollEl = ref<HTMLElement | null>(null)
const sessionId = ref<string>('')
const sessions = ref<SessionSummary[]>([])

const SESSION_STORAGE_KEY = 'chongqing_ai_session_id'
const SIDEBAR_STORAGE_KEY = 'chongqing_ai_sidebar_collapsed'

const sidebarCollapsed = ref(localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1')

/**
 * 在途流式请求的序号。新建/切换/删除会话时自增，用以**作废**旧流的全部回调。
 *
 * 只 abort 是不够的：abort 后读取循环会陆续抛 AbortError，但已经排队进微任务队列的
 * 回调仍会跑完 —— 而那一刻 `messages.value` 早已被换成另一个会话的历史（或空数组），
 * 回调里的 `messages.value[idx]` 就是 `undefined`，紧接着的 `.kind` 读取抛
 * `TypeError` 白屏；`onSelectSession` 的情形更隐蔽：A 会话的模型输出会被写进 B 会话界面。
 */
const streamSeq = ref(0)
/** 会话加载序号：快速点 A→B 时 A 的响应后到，会把界面覆盖成 A（而 sessionId 已是 B）。 */
const sessionLoadSeq = ref(0)

const { isDark, preference, setPreference } = useDark()

onMounted(async () => {
  try {
    dataSources.value = await listDataSources()
  } catch {
    // 后端未启动时静默，前端仍可输入
  }
  await restoreSession()
  refreshSessions()
})

onUnmounted(() => {
  // 卸载时作废并中断在途流：否则回调会一直持有组件作用域里的 messages，
  // 直到整条流跑完（后端可能还在跑决策报告，几十秒到几分钟）
  streamSeq.value += 1
  abortController.value?.abort()
})

// 流式输出时若用户接近底部，则自动贴底；正在向上翻阅时不打扰
watch(
  messages,
  () => nextTick(scrollToBottomIfNear),
  { deep: true },
)

function scrollToBottomIfNear() {
  const el = scrollEl.value
  if (!el) return
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  if (nearBottom) el.scrollTop = el.scrollHeight
}

function onThemeCommand(cmd: ThemePreference) {
  setPreference(cmd)
}

async function onUpload(file: File): Promise<boolean> {
  uploading.value = true
  try {
    const id = await uploadDataSource(file)
    dataSources.value = await listDataSources()
    dataSourceId.value = id
    ElNotification({
      title: '上传成功',
      message: '文件已导入为数据源。',
      type: 'success',
      duration: 3000,
      position: 'top-right',
    })
  } catch (e) {
    notifyError(e instanceof Error ? e.message : '上传失败', 'SYSTEM_ERROR')
  } finally {
    uploading.value = false
  }
  return false // 阻止 el-upload 默认上传
}

function openConnectDialog() {
  editTargetId.value = null
  showConnectionDialog.value = true
}

function openEditConnection(id: string) {
  editTargetId.value = id
  showConnectionDialog.value = true
}

async function onConnectionSaved(r: ConnectionCreateResult) {
  showConnectionDialog.value = false
  try {
    dataSources.value = await listDataSources()
  } catch {
    // 刷新失败静默，仍选中新连接
  }
  dataSourceId.value = r.id
  const tableNames = r.tables.map((t) => t.name).join('、') || '（暂无表）'
  ElNotification({
    title: '连接成功',
    message: `已保存「${r.name}」，包含表：${tableNames}`,
    type: 'success',
    duration: 5000,
    position: 'top-right',
  })
}

async function onDataSourceDeleted(id: string) {
  try {
    dataSources.value = await listDataSources()
  } catch {
    // 刷新失败静默
  }
  // 删除的是当前选中数据源时清空选择，避免下次提问用失效 id
  if (dataSourceId.value === id) dataSourceId.value = ''
}

function buildHistory(): HistoryMessage[] {
  const history: HistoryMessage[] = []
  for (const m of messages.value) {
    if (m.loading) continue
    if (m.role === 'user') {
      history.push({ role: 'user', content: m.content })
    } else {
      const text = m.answer ?? m.result?.explanation ?? ''
      if (text) history.push({ role: 'assistant', content: text })
    }
  }
  return history
}

function newSessionId(): string {
  const id = crypto.randomUUID()
  localStorage.setItem(SESSION_STORAGE_KEY, id)
  return id
}

function toMessage(m: SessionMessage): Message {
  if (m.role === 'user') {
    return { role: 'user', content: m.content }
  }
  if (m.kind === 'chat') {
    return {
      role: 'assistant',
      content: '',
      kind: 'chat',
      answer: m.content,
      report: m.report ?? undefined,
      sources: m.sources ?? undefined,
    }
  }
  return {
    role: 'assistant',
    content: '',
    kind: 'query',
    result: m.result ?? undefined,
    report: m.report ?? undefined,
    streamingSql: m.sql ?? '',
  }
}

function applyDataSource(id: string | null) {
  if (id && dataSources.value.some((d) => d.id === id)) {
    dataSourceId.value = id
  } else {
    // 数据源已不存在或未记录：回到空数据源，等待上传/选择
    dataSourceId.value = ''
  }
}

async function restoreSession() {
  const saved = localStorage.getItem(SESSION_STORAGE_KEY)
  if (saved) {
    sessionId.value = saved
    const seq = ++sessionLoadSeq.value
    try {
      const detail = await getSession(saved)
      // 恢复期间用户可能已经点了别的会话，后到的响应不得覆盖（同 onSelectSession）
      if (seq !== sessionLoadSeq.value) return
      messages.value = detail.messages.map(toMessage)
      applyDataSource(detail.last_data_source_id)
    } catch {
      // 会话不存在或后端未启动：继续使用该 id，从空会话开始
    }
  } else {
    sessionId.value = newSessionId()
  }
}

/**
 * 中止在途流并作废其回调（切换/新建/删除会话时调用）。
 *
 * 与「停止生成」按钮不同：停止按钮只 abort、**不作废**，好让本轮的 AbortError 分支
 * 照常收尾（补上「已停止生成」）；切换会话则必须作废，否则旧流的输出会写进新会话。
 */
function cancelActiveStream() {
  streamSeq.value += 1
  abortController.value?.abort()
  abortController.value = null
  loading.value = false
}

async function refreshSessions() {
  try {
    sessions.value = await listSessions()
  } catch {
    // 后端未启动时静默
  }
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem(SIDEBAR_STORAGE_KEY, sidebarCollapsed.value ? '1' : '0')
}

function onNewConversation() {
  sessionLoadSeq.value += 1 // 作废在途的会话加载，避免它的响应把新会话界面覆盖掉
  cancelActiveStream()
  sessionId.value = newSessionId()
  messages.value = []
  refreshSessions()
}

async function onSelectSession(s: SessionSummary) {
  const seq = ++sessionLoadSeq.value
  cancelActiveStream()
  try {
    const detail = await getSession(s.session_id)
    if (seq !== sessionLoadSeq.value) return // 期间又切了别的会话，丢弃本次结果
    sessionId.value = s.session_id
    localStorage.setItem(SESSION_STORAGE_KEY, s.session_id)
    messages.value = detail.messages.map(toMessage)
    applyDataSource(detail.last_data_source_id)
  } catch {
    if (seq !== sessionLoadSeq.value) return
    ElNotification({
      title: '系统提示',
      message: '加载会话失败，请稍后重试。',
      type: 'error',
      duration: 3000,
      position: 'top-right',
    })
  }
}

async function onDeleteSession(s: SessionSummary) {
  try {
    await ElMessageBox.confirm(
      `确定删除会话「${s.title}」？此操作不可恢复。`,
      '删除会话',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteSession(s.session_id)
    if (s.session_id === sessionId.value) {
      sessionLoadSeq.value += 1
      cancelActiveStream()
      sessionId.value = newSessionId()
      messages.value = []
      dataSourceId.value = ''
    }
    refreshSessions()
  } catch {
    ElNotification({
      title: '系统提示',
      message: '删除失败，请稍后重试。',
      type: 'error',
      duration: 3000,
      position: 'top-right',
    })
  }
}

async function onSubmit(question: string) {
  const text = question.trim()
  if (!text) return
  if (!dataSourceId.value) {
    ElNotification({
      title: '系统提示',
      message: '请先上传或选择数据源。',
      type: 'warning',
      duration: 3000,
      position: 'top-right',
    })
    return
  }

  const history = buildHistory()
  messages.value.push({ role: 'user', content: text })
  const idx = messages.value.length
  messages.value.push({
    role: 'assistant',
    content: '',
    loading: true,
    streamingSql: '',
    streamingText: '',
    progress: '',
  })

  const controller = new AbortController()
  abortController.value = controller
  loading.value = true
  // 本轮流式请求的序号：会话一旦被切换/新建/删除，streamSeq 就会变，下面所有写回自动失效
  const seq = ++streamSeq.value
  const alive = () => seq === streamSeq.value
  /**
   * 按变更改写指定位置的消息；本轮流已失效、或该位置的消息已被移除时什么都不做。
   *
   * 全部写回都必须走这里 —— 直接 `messages.value[idx] = {...}` 在「流式输出中点新建对话」
   * 时会写到一个不存在的槽位，而回调里对 `messages.value[idx].kind` 的读取会直接抛
   * `TypeError: Cannot read properties of undefined`（白屏）。
   */
  const patch = (i: number, change: Partial<Message>) => {
    if (!alive()) return
    const current = messages.value[i]
    if (!current) return
    messages.value[i] = { ...current, ...change }
  }
  let sqlText = ''
  let answerText = ''
  try {
    await chatStream(
      text,
      dataSourceId.value,
      history,
      sessionId.value,
      {
        onIntent: (phase) => {
          // 只更新等待文案的阶段标签；渲染家族（kind）在 onResult 时按 result.kind 落定
          patch(idx, { phase })
        },
        onToken: (t) => {
          sqlText += t
          patch(idx, { streamingSql: sqlText })
        },
        onAnswerToken: (t) => {
          answerText += t
          patch(idx, { streamingText: answerText })
        },
        onProgress: (text, scope) => {
          // 分析 / 报告 agent 连续跑十几轮 LLM 往返且中途没有任何事件，这里把
          // 后端下发的步骤文案滚动出来，让「在做什么」可见（否则界面全程静止）。
          // 报告阶段单独开位：onResult 已把 loading 置 false，loading 块不再渲染。
          if (scope === 'report') patch(idx, { reportPending: true, reportProgress: text })
          else patch(idx, { progress: text })
        },
        onResult: (result) => {
          if (result.kind === 'chat') {
            patch(idx, {
              role: 'assistant',
              content: '',
              kind: 'chat',
              answer: result.answer ?? answerText,
              streamingText: answerText,
              sources: result.sources,
              loading: false,
              progress: '',
            })
          } else {
            patch(idx, {
              role: 'assistant',
              content: '',
              kind: 'query',
              result,
              loading: false,
              streamingSql: sqlText,
              progress: '',
            })
          }
          if (result.error) notifyError(result.error, result.error_code)
        },
        onReport: (report) => {
          // 决策报告在 result 之后到达：回写进同一条 assistant 消息，不覆盖已有内容
          patch(idx, { report, reportPending: false, reportProgress: '' })
        },
        onError: (message, code) => {
          const current = messages.value[idx]
          if (!alive() || !current) return
          const kind = current.kind ?? 'query'
          const result: QueryResponse = {
            question: text,
            kind,
            answer: null,
            sql: null,
            columns: [],
            rows: [],
            row_count: 0,
            chart_type: null,
            explanation: null,
            analysis: null,
            error: message,
            error_code: code,
            sources: [],
          }
          patch(idx, {
            role: 'assistant',
            content: '',
            kind,
            result,
            loading: false,
            streamingSql: sqlText,
            streamingText: answerText,
            progress: '',
            reportPending: false,
            reportProgress: '',
          })
          notifyError(message, code)
        },
      },
      { signal: controller.signal },
    )
    // 成功后刷新历史会话列表
    void refreshSessions()
  } catch (e) {
    if ((e as Error).name === 'AbortError') {
      // 用户主动停止：保留已流式内容，标记结束，不弹网络错误。
      // 这里之所以还能写回，是因为「停止」只 abort、不作废（见 cancelActiveStream 的说明）
      patch(idx, { loading: false, content: '（已停止生成）' })
    } else {
      // 如实展示失败原因：`streamPost` 抛出的 StreamError 已带面向用户的说明
      // （空闲超时 / HTTP 状态 / 网关返回非流式内容 / 真的连不上后端）。
      // 早前一律显示「网络请求失败，请确认后端服务已启动」，把空闲超时说成后端没启动，
      // 用户据此去排查一个其实健康的服务 —— 文案必须与事实一致。
      // 非 StreamError 属代码缺陷（如回调里的 TypeError），技术细节不外泄给用户。
      const error = e instanceof StreamError ? e.message : '请求失败，请稍后重试。'
      const current = messages.value[idx]
      if (!alive() || !current) return
      const kind = current.kind ?? 'query'
      const result: QueryResponse = {
        question: text,
        kind,
        answer: null,
        sql: null,
        columns: [],
        rows: [],
        row_count: 0,
        chart_type: null,
        explanation: null,
        analysis: null,
        error,
        error_code: 'NETWORK_ERROR',
        sources: [],
      }
      patch(idx, {
        role: 'assistant',
        content: '',
        kind,
        result,
        loading: false,
        streamingSql: sqlText,
        streamingText: answerText,
        progress: '',
        reportPending: false,
        reportProgress: '',
      })
      notifyError(error, 'NETWORK_ERROR')
    }
  } finally {
    // 只在本轮仍是当前流时收尾：会话切换后可能已经开了新的一轮，
    // 这里无条件清空会把新流的 loading/controller 一并抹掉。
    if (alive()) {
      loading.value = false
      abortController.value = null
      // 进度文案必须在这里兜底清掉，不能只在 onReport / onResult 里清：
      // 报告被关闭（REPORT_ENABLED=false）、报告节点失败静默跳过、或用户中途
      // 点「停止」时，都不会再有后续事件，占位块会永久停在「正在生成决策报告…」。
      patch(idx, { progress: '', reportPending: false, reportProgress: '' })
    }
  }
}

function onStop() {
  abortController.value?.abort()
}

function notifyError(message: string, code: string | null) {
  const isConnErr = code === 'DATASOURCE_UNAVAILABLE' || code === 'DATASOURCE_CONNECT_FAILED'
  const repairable = isConnErr && dataSourceId.value.startsWith('ext_')
  if (repairable) {
    let notif: ReturnType<typeof ElNotification> | null = null
    notif = ElNotification({
      title: '连接失败',
      message: h('div', { style: 'display:flex;flex-direction:column;gap:8px;' }, [
        h('span', null, message),
        h('div', null, [
          h(
            ElButton,
            {
              size: 'small',
              type: 'primary',
              onClick: () => {
                notif?.close()
                openEditConnection(dataSourceId.value)
              },
            },
            () => '修复连接',
          ),
        ]),
      ]),
      type: 'error',
      duration: 0,
      position: 'top-right',
    })
    return
  }

  ElNotification({
    title: '系统提示',
    message,
    type: code === 'NO_API_KEY' ? 'warning' : 'error',
    duration: 5000,
    position: 'top-right',
  })
}
</script>

<style scoped>
.home {
  display: flex;
  flex-direction: row;
  height: 100%;
  background: var(--app-bg-page);
}
.main {
  flex: 1;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--app-bg-header);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  box-shadow: inset 0 -1px 0 var(--app-border);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 20px;
  font-weight: 600;
  color: var(--app-text-1);
}
.logo {
  font-size: 20px;
  color: var(--el-color-primary);
}
.topbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat-scroll {
  flex: 1;
  overflow-y: auto;
}
.chat-column {
  max-width: 820px;
  margin: 0 auto;
  padding: 24px 16px 40px;
  width: 100%;
}
.composer {
  padding: 16px 16px 48px;
  display: flex;
  justify-content: center;
}
.composer-inner {
  width: 100%;
  max-width: 820px;
}
</style>
