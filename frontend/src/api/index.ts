import axios from 'axios'
import type {
  ConnectionCreate,
  ConnectionCreateResult,
  ConnectionInfo,
  ConnectionTestResult,
  DataSourceInfo,
  HistoryMessage,
  MessagePhase,
  QueryResponse,
  ReportResult,
  SessionDetail,
  SessionSummary,
  StreamHandlers,
} from '../types'

const http = axios.create({
  baseURL: '',
  timeout: 120000,
})

/**
 * SSE 空闲超时：**两次事件之间**的最大间隔，不是整条流的时长。
 *
 * `fetch` 没有 axios 那种 `timeout`，后端卡死（连接建立后不再吐字）时
 * `reader.read()` 会**永久挂起**，界面一直停在 loading，用户只能刷新页面。
 * 用「整条流总时长」当超时是错的：报告 + 查询的正常链路本就可能跑几分钟。
 *
 * 取值要**明显大于后端最长静默**，否则健康的慢查询会被误杀：`want_sql` 为假时
 * text2sql 的 token 不发（`graph.py` 的 `_text2sql_node`），于是 schema 反射 →
 * SQL 流式生成 → 执行 → `summarize_query_result`（又一次 LLM 往返）全程零事件，
 * 实测复杂提问的 `intent` → `result` 静默达 **205 秒**；其后的决策报告 agent 还会
 * 串行跑若干轮工具调用（每轮 LLM 上限 60s、重试 1 次）。10 分钟留足余量，
 * 且界面的「停止」按钮（`AbortController`）始终可用，用户不会被卡住。
 *
 * 后端现已**每 10 秒下发一行 SSE 注释**（`: keepalive`，`app/routers/chat.py`）——
 * 注释行同样让 `reader.read()` 返回、重置这里的计时器，所以分析 / 决策报告这类
 * 连续跑十几轮 LLM 的长任务不会再触顶。这里的阈值退化为「后端进程整体失联」的兜底。
 */
const SSE_IDLE_TIMEOUT_MS = 600000

/**
 * 流式请求错误：`message` 面向用户，可直接展示在界面上。
 *
 * 与普通 `Error` 区分开，是为了让界面**如实**说明失败原因 —— 早前 `HomeView.vue`
 * 把所有异常一律显示成「网络请求失败，请确认后端服务已启动」，于是空闲超时、
 * HTTP 5xx、网关返回 HTML 错误页这些情况全被说成「后端没启动」，把用户支去排查
 * 一个其实健康的后端。只有 `streamPost` 里 `fetch` 自身的网络层失败才是真的不可达。
 */
export class StreamError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'StreamError'
  }
}

/** SSE 事件分隔符。`\r\n\r\n` 必须认：经会规范化换行的代理/网关后 CRLF 很常见。 */
const SSE_SEP_RE = /\r\n\r\n|\n\n|\r\r/

export async function queryData(
  question: string,
  dataSourceId = '',
): Promise<QueryResponse> {
  const { data } = await http.post<QueryResponse>('/api/query', {
    question,
    data_source_id: dataSourceId,
  })
  return data
}

/**
 * 带空闲超时的 `reader.read()`。
 *
 * 超时触发时先把 reader 取消掉再抛错：否则底层的 read 仍挂着，
 * 连接与回调会一直留在内存里（页面切走后尤其明显）。
 */
async function readWithIdleTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  idleMs: number,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  let timer: number | undefined
  const timeout = new Promise<never>((_, reject) => {
    timer = window.setTimeout(
      () => reject(new StreamError(`流式响应超过 ${Math.round(idleMs / 1000)} 秒无数据，已中断。`)),
      idleMs,
    )
  })
  try {
    return await Promise.race([reader.read(), timeout])
  } catch (e) {
    void reader.cancel().catch(() => {})
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}

/**
 * 消费一条 SSE 响应，把每个事件的 `data` 载荷交给 `onEvent`。
 *
 * 三处必须做对，否则会**静默丢事件**（界面只是「不动」，看不出哪里错）：
 * 1) 分隔符不能只认 `\n\n` —— 经会把换行规范化成 CRLF 的代理/网关时它永不命中，
 *    结果是除第一个事件外全部被丢弃，表现为「一直在 loading，最后一闪出结果」。
 * 2) 一个事件可以有多行 `data:` —— 按 SSE 规范应以换行拼接，只取首行会截断载荷。
 * 3) 解析失败要留 `console.warn` —— 静默吞掉时线上完全无从查起。
 */
async function consumeSSE(
  resp: Response,
  onEvent: (evt: Record<string, unknown>) => void,
  idleMs = SSE_IDLE_TIMEOUT_MS,
): Promise<void> {
  if (!resp.ok) {
    throw new StreamError(`请求失败：HTTP ${resp.status}`)
  }
  // 网关/反代出错时常返回 200 + HTML 错误页：不校验类型时前端只会一直转圈
  const contentType = resp.headers.get('content-type') || ''
  if (!contentType.includes('text/event-stream')) {
    throw new StreamError(`服务端返回了非流式内容（content-type: ${contentType || '缺失'}）。`)
  }
  if (!resp.body) {
    throw new StreamError('响应没有可读的流。')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let dataLines: string[] = []

  const flushEvent = () => {
    if (!dataLines.length) return
    const payload = dataLines.join('\n') // 多行 data: 按规范以换行拼接
    dataLines = []
    if (!payload.trim()) return
    let evt: unknown
    try {
      evt = JSON.parse(payload)
    } catch {
      console.warn('[sse] 无法解析的事件载荷，已忽略：', payload.slice(0, 200))
      return
    }
    if (evt && typeof evt === 'object') onEvent(evt as Record<string, unknown>)
  }

  const feedBlock = (block: string) => {
    for (const rawLine of block.split('\n')) {
      const line = rawLine.replace(/\r$/, '')
      if (!line || line.startsWith(':')) continue // 空行 / 注释（含心跳）
      if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).replace(/^ /, ''))
      }
      // 其余字段（event: / id: / retry:）本项目未使用，忽略
    }
    flushEvent()
  }

  /** 把 buffer 中已经完整的事件（以空行结尾）逐条取走。 */
  const drain = () => {
    for (;;) {
      const m = SSE_SEP_RE.exec(buffer) // 非 /g 正则：每次都能拿到**第一个**分隔符
      if (!m) break
      const block = buffer.slice(0, m.index)
      buffer = buffer.slice(m.index + m[0].length)
      feedBlock(block)
    }
  }

  while (true) {
    const { done, value } = await readWithIdleTimeout(reader, idleMs)
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    drain()
  }
  buffer += decoder.decode() // 冲刷解码器残留
  if (buffer.trim()) feedBlock(buffer) // 服务端末尾未补空行的收尾事件
  flushEvent()
}

/**
 * POST 一个 JSON 请求体并消费其 SSE 响应（两处流式接口共用）。
 *
 * 抛出的错误要么是 `AbortError`（用户主动停止，由调用方识别），要么是 `StreamError`
 * 且 `message` 已面向用户 —— 调用方直接展示即可，不必再套一层猜测性的兜底文案。
 */
async function streamPost(
  url: string,
  body: unknown,
  onEvent: (evt: Record<string, unknown>) => void,
  options?: { signal?: AbortSignal },
): Promise<void> {
  let resp: Response
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: options?.signal,
    })
  } catch (e) {
    // 用户点了「停止」：原样抛出，由调用方按 AbortError 走「已停止生成」分支
    if ((e as Error)?.name === 'AbortError') throw e
    // 到这里才是**真正连不上后端**（服务未启动 / 端口不通 / 被 CORS 拦下）。
    // 其余失败形态都在 consumeSSE 内各自带了更准确的原因。
    throw new StreamError('网络请求失败，请确认后端服务已启动。')
  }
  await consumeSSE(resp, onEvent)
}

const asStr = (v: unknown): string => (typeof v === 'string' ? v : '')
/**
 * 后端 intent 事件透传真实意图（chat / retrieval / schema / analysis），用于前置等待文案。
 * redraw 与 query 同属 SQL/表格家族，一并归入 'query'；未知值同样兜底为 'query'。
 *
 * analysis 必须**原样透传**：它一次 SQL 都不生成（走 pandas 统计算子），压成 'query'
 * 会让整段分析全程显示「正在生成 SQL…」。
 * 注意这里产出的是 MessagePhase 而非 MessageKind —— 渲染家族仍由 result.kind 决定。
 */
const asMessagePhase = (v: unknown): MessagePhase =>
  v === 'chat' || v === 'retrieval' || v === 'schema' || v === 'analysis' ? v : 'query'

export async function queryDataStream(
  question: string,
  dataSourceId: string,
  handlers: StreamHandlers,
  options?: { signal?: AbortSignal },
): Promise<void> {
  await streamPost(
    '/api/query/stream',
    { question, data_source_id: dataSourceId },
    (evt) => {
      if (evt.type === 'sql_token') handlers.onToken(asStr(evt.content))
      else if (evt.type === 'result') handlers.onResult(evt.data as QueryResponse)
      else if (evt.type === 'error') handlers.onError(asStr(evt.message), asStr(evt.error_code))
    },
    options,
  )
}

export async function chatStream(
  question: string,
  dataSourceId: string,
  history: HistoryMessage[],
  sessionId: string | null,
  handlers: StreamHandlers,
  options?: { signal?: AbortSignal },
): Promise<void> {
  await streamPost(
    '/api/chat/stream',
    { question, data_source_id: dataSourceId, history, session_id: sessionId },
    (evt) => {
      if (evt.type === 'intent') handlers.onIntent?.(asMessagePhase(evt.intent))
      else if (evt.type === 'sql_token') handlers.onToken(asStr(evt.content))
      else if (evt.type === 'answer_token') handlers.onAnswerToken?.(asStr(evt.content))
      else if (evt.type === 'progress') handlers.onProgress?.(asStr(evt.text), asStr(evt.scope))
      else if (evt.type === 'result') handlers.onResult(evt.data as QueryResponse)
      else if (evt.type === 'report') handlers.onReport?.(evt.data as ReportResult)
      else if (evt.type === 'error') handlers.onError(asStr(evt.message), asStr(evt.error_code))
    },
    options,
  )
}

export async function listDataSources(): Promise<DataSourceInfo[]> {
  const { data } = await http.get<{ data_sources: DataSourceInfo[] }>('/api/datasources')
  return data.data_sources
}

export async function uploadDataSource(file: File): Promise<string> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post<{ data_source_id?: string; error?: string }>(
    '/api/datasources/upload',
    form,
  )
  if (data.error || !data.data_source_id) {
    throw new Error(data.error || '上传失败')
  }
  return data.data_source_id
}

// 路径参数一律 encodeURIComponent：数据源 id 刻意保留原始文件名，
// 中文/空格/#/? 都在标识符中合法。`销售#1.csv` → id `upload:销售#1`，
// 不编码时 `#` 之后会被当 URL 片段丢弃，服务端只收到 `upload:销售` → 删错对象或 404。
const enc = encodeURIComponent

export async function deleteDataSource(id: string): Promise<void> {
  await http.delete(`/api/datasources/${enc(id)}`)
}

export async function listSessions(): Promise<SessionSummary[]> {
  const { data } = await http.get<SessionSummary[]>('/api/sessions')
  return data
}

export async function listConnections(): Promise<ConnectionInfo[]> {
  const { data } = await http.get<ConnectionInfo[]>('/api/connections')
  return data
}

export async function testConnection(payload: ConnectionCreate): Promise<ConnectionTestResult> {
  const { data } = await http.post<ConnectionTestResult>('/api/connections/test', payload)
  return data
}

export async function createConnection(payload: ConnectionCreate): Promise<ConnectionCreateResult> {
  const { data } = await http.post<ConnectionCreateResult>('/api/connections', payload)
  return data
}

export async function updateConnection(
  id: string,
  payload: ConnectionCreate,
): Promise<ConnectionCreateResult> {
  const { data } = await http.put<ConnectionCreateResult>(`/api/connections/${enc(id)}`, payload)
  return data
}

export async function deleteConnection(id: string): Promise<void> {
  await http.delete(`/api/connections/${enc(id)}`)
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const { data } = await http.get<SessionDetail>(`/api/sessions/${enc(sessionId)}`)
  return data
}

export async function deleteSession(sessionId: string): Promise<void> {
  await http.delete(`/api/sessions/${enc(sessionId)}`)
}
