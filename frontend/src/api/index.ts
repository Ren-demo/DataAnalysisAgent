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

/* -------------------------------------------------------------------------- */
/* 决策报告归一化                                                              */
/* -------------------------------------------------------------------------- */

/**
 * 报告卡片的数据清洗，作用在 **API 边界层**：SSE `report` 事件与
 * `GET /api/sessions/{id}` 的历史回放共用同一个函数。
 *
 * 为什么需要它：旧版后端解析失败后会把 agent 的原始输出（通常是整份报告 JSON）
 * 截断 500 字塞进 `summary`，前端原样渲染，于是页面上出现 `"title":`、
 * `"key_findings":[` 这类字段名与标点。这些坏数据已经写进 MongoDB，只修后端
 * 无法让历史会话自愈，所以读取时再做一次归一化。
 *
 * 归一化对「已经正常的报告」是幂等的：字段原样通过，不会被二次改写。
 */

const REPORT_FIELD_KEYS = [
  'title',
  'summary',
  'key_findings',
  'analysis',
  'recommendations',
  'confidence',
] as const

const SUMMARY_KEYS = [
  'summary',
  'conclusion',
  'core_conclusion',
  'core',
  'answer',
  'text',
  'content',
  '结论',
  '核心结论',
  '摘要',
]

const FIELD_LABELS: Record<string, string> = {
  summary: '摘要',
  title: '标题',
  conclusion: '结论',
  core: '核心结论',
  trend: '趋势',
  reason: '原因',
  reasons: '原因',
  evidence: '依据',
  detail: '明细',
  details: '明细',
  risk: '风险',
  risks: '风险',
  recommendation: '建议',
  recommendations: '建议',
  metric: '指标',
  value: '数值',
  change: '变化',
  impact: '影响',
  data: '数据',
}

const REPORT_FALLBACK_TITLE = '数据分析报告'
const REPORT_FALLBACK_SUMMARY =
  '本次分析已完成，但报告摘要暂时无法生成。请结合上方的查询结果与图表理解数据。'

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function isEmptyValue(v: unknown): boolean {
  return v == null || v === '' || (Array.isArray(v) && v.length === 0)
}

function tryJsonParse(s: string): unknown {
  try {
    return JSON.parse(s)
  } catch {
    return undefined
  }
}

function stripFence(s: string): string {
  const m = s.match(/```(?:json|markdown|md)?\s*([\s\S]*?)\s*```/i)
  return (m ? m[1] : s).trim()
}

function looksLikeJson(s: string): boolean {
  const t = s.trimStart()
  return (t.startsWith('{') || t.startsWith('[')) && /["']\s*:\s*/.test(t)
}

/** 返回与 s[start]（`{`）配对的 `}` 下标；跳过字符串内的花括号。 */
function matchingBraceEnd(s: string, start: number): number {
  let depth = 0
  let quote = ''
  let escaped = false
  for (let i = start; i < s.length; i += 1) {
    const ch = s[i]
    if (quote) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === quote) quote = ''
      continue
    }
    if (ch === '"' || ch === "'") quote = ch
    else if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) return i
    }
  }
  return -1
}

function reportKeyScore(candidate: string): number {
  return REPORT_FIELD_KEYS.reduce(
    (n, key) => n + (new RegExp(`["']${key}["']\\s*:`).test(candidate) ? 1 : 0),
    0,
  )
}

/** 扫描文本里的 JSON 对象候选；未闭合的尾片也保留，交给截断抢救。 */
function extractJsonCandidates(text: string): string[] {
  const out: string[] = []
  const limit = Math.min(text.length, 20000)
  let i = 0
  while (i < limit && out.length < 50) {
    if (text[i] !== '{') {
      i += 1
      continue
    }
    const end = matchingBraceEnd(text, i)
    if (end < 0) {
      out.push(text.slice(i, limit))
      break
    }
    out.push(text.slice(i, end + 1))
    i = end + 1
  }
  return out.sort((a, b) => reportKeyScore(b) - reportKeyScore(a) || b.length - a.length)
}

function unescapeLoose(s: string): string {
  try {
    return JSON.parse(`"${s}"`) as string
  } catch {
    return s.replace(/\\n/g, '\n').replace(/\\"/g, '"').replace(/\\'/g, "'")
  }
}

/** 从无法解析的文本里用正则捞回报告字段（截断 JSON 的最后手段）。 */
function salvageFields(text: string): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const key of ['title', 'summary', 'analysis'] as const) {
    const complete = text.match(
      new RegExp(`["']${key}["']\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`, 's'),
    )
    const partial = text.match(new RegExp(`["']${key}["']\\s*:\\s*"([^"\\n]{1,800})`, 's'))
    const raw = complete?.[1] ?? partial?.[1] ?? ''
    const value = raw ? unescapeLoose(raw) : ''
    if (value) out[key] = value
  }
  for (const key of ['key_findings', 'recommendations'] as const) {
    const m = text.match(new RegExp(`["']${key}["']\\s*:\\s*\\[([\\s\\S]*?)(?:\\]|$)`, 's'))
    if (!m) continue
    const body = m[1].replace(/,\s*$/, '')
    try {
      const parsed = JSON.parse(`[${body}]`)
      if (Array.isArray(parsed)) out[key] = parsed
    } catch {
      const items = [...body.matchAll(/["']((?:[^"'\\]|\\.)*)["'](?!\s*:)/g)]
        .map((x) => unescapeLoose(x[1]))
        .filter(Boolean)
      if (items.length) out[key] = items
    }
  }
  const confidence = text.match(/["']confidence["']\s*:\s*["'](high|medium|low)["']/i)?.[1]
  if (confidence) out.confidence = confidence
  return out
}

/** 把对象 / JSON 字符串 / 数组收敛成一个报告对象。 */
function parseObjectLoose(raw: unknown, depth = 0): Record<string, unknown> | null {
  if (depth > 3 || raw == null) return null
  if (isRecord(raw)) return raw
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const got = parseObjectLoose(item, depth + 1)
      if (got && REPORT_FIELD_KEYS.some((k) => k in got)) return got
    }
    return null
  }
  if (typeof raw !== 'string') return null

  const text = stripFence(raw)
  if (!text) return null
  const direct = tryJsonParse(text)
  if (isRecord(direct)) return direct
  if (typeof direct === 'string' && direct !== text) return parseObjectLoose(direct, depth + 1)

  // 配对扫描出的候选；再尝试去尾逗号 / 折叠裸换行两个变体
  for (const candidate of extractJsonCandidates(text)) {
    for (const fixed of [
      candidate,
      candidate.replace(/,\s*([}\]])/g, '$1'),
      candidate.replace(/[\r\n\t]+/g, ' '),
    ]) {
      const parsed = tryJsonParse(fixed)
      if (isRecord(parsed)) return parsed
    }
  }
  return null
}

/** 把嵌套在 summary 里的「整份报告」还原成对象。 */
function maybeReportDict(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value
  if (typeof value !== 'string') return null
  const candidates = [stripFence(value), ...extractJsonCandidates(value)]
  for (const candidate of candidates) {
    const parsed = tryJsonParse(candidate)
    if (isRecord(parsed) && REPORT_FIELD_KEYS.some((k) => k in parsed)) return parsed
  }
  return null
}

function cleanText(s: string): string {
  let text = stripFence(s)
    .trim()
    .replace(/^["']+|["']+$/g, '')
    .trim()
  text = text.replace(
    /["'](?:title|summary|key_findings|analysis|recommendations|confidence)["']\s*:\s*/gi,
    '',
  )
  if (text.startsWith('{') || text.startsWith('[')) {
    text = text.replace(/^[{[\]\s]+/, '').replace(/[}\],\s]+$/, '')
  }
  text = text.replace(/^:+|:+$/g, '').trim()
  // 仍有 ≥2 个 "键": 形态的残片：判定不可读，交给降级文案
  const keyMatches = text.match(/["'][^"'\n]{1,40}["']\s*:/g)
  if (keyMatches && keyMatches.length >= 2) return ''
  return text
}

function scalarText(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return cleanText(v)
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'number') return String(v)
  return ''
}

function labelOf(key: string): string {
  return FIELD_LABELS[key] ?? FIELD_LABELS[key.toLowerCase()] ?? key
}

/** 把一个 dict 元素压成一行可读文本；绝不 String(dict) 泄漏花括号。 */
function dictLine(v: Record<string, unknown>): string {
  const primaryKeys = [
    'text',
    'finding',
    'content',
    'description',
    'summary',
    'title',
    'point',
    'label',
    'name',
  ]
  const primary = primaryKeys.find((k) => scalarText(v[k]))
  if (primary) {
    const main = scalarText(v[primary]).replace(/\s+/g, ' ').trim()
    const extras = Object.entries(v)
      .filter(([k, val]) => k !== primary && scalarText(val))
      .map(([k, val]) => `${labelOf(k)}：${scalarText(val)}`)
    return main + (extras.length ? `（${extras.join('；')}）` : '')
  }
  return Object.entries(v)
    .map(([k, val]) => (scalarText(val) ? `${labelOf(k)}：${scalarText(val)}` : ''))
    .filter(Boolean)
    .join('；')
}

function asText(value: unknown, depth = 0): string {
  if (value == null || depth > 3) return ''
  if (typeof value === 'string') {
    const text = stripFence(value).trim()
    if (!text || /^(null|none|\{\}|\[\])$/i.test(text)) return ''
    if (looksLikeJson(text)) {
      const parsed = tryJsonParse(text)
      if (isRecord(parsed) || Array.isArray(parsed)) return asText(parsed, depth + 1)
      return cleanText(text)
    }
    return text
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (isRecord(item)) {
          const line = dictLine(item)
          return line ? `- ${line}` : ''
        }
        const sub = asText(item, depth + 1)
        if (!sub) return ''
        return sub.includes('\n') ? sub : `- ${sub}`
      })
      .filter(Boolean)
      .join('\n')
  }
  if (isRecord(value)) {
    for (const key of ['analysis', 'detail', 'details', 'text', 'content', 'summary']) {
      if (value[key] != null && value[key] !== '') {
        const got = asText(value[key], depth + 1)
        if (got) return got
      }
    }
    return Object.entries(value)
      .map(([k, val]) => {
        if (isRecord(val) || Array.isArray(val)) {
          const sub = asText(val, depth + 1)
          if (!sub) return ''
          return sub.includes('\n')
            ? `- **${labelOf(k)}**：\n${sub}`
            : `- **${labelOf(k)}**：${sub}`
        }
        const scalar = scalarText(val)
        return scalar ? `- **${labelOf(k)}**：${scalar}` : ''
      })
      .filter(Boolean)
      .join('\n')
  }
  return scalarText(value)
}

function asSummary(value: unknown, depth = 0): string {
  if (value == null || depth > 3) return ''
  if (typeof value === 'string') {
    const text = stripFence(value).trim()
    if (!text || /^(null|none|\{\}|\[\])$/i.test(text)) return ''
    const parsed = tryJsonParse(text)
    if (isRecord(parsed) || Array.isArray(parsed)) return asSummary(parsed, depth + 1)
    if (looksLikeJson(text)) {
      const nested = maybeReportDict(text)
      if (nested) {
        const got = asSummary(nested.summary, depth + 1)
        if (got) return got
      }
      const m = text.match(
        /["'](?:summary|conclusion|core|content|text)["']\s*:\s*["']((?:[^"'\\]|\\.)*)/i,
      )
      return m ? asSummary(unescapeLoose(m[1]), depth + 1) : ''
    }
    return cleanText(text)
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => asSummary(item, depth + 1))
      .filter(Boolean)
      .join('；')
  }
  if (isRecord(value)) {
    for (const key of SUMMARY_KEYS) {
      const got = asSummary(value[key], depth + 1)
      if (got) return got
    }
    return Object.values(value)
      .map((item) => asSummary(item, depth + 1))
      .filter(Boolean)
      .join('；')
  }
  if (typeof value === 'number') return String(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  return ''
}

function asStringList(value: unknown, limit = 6): string[] {
  if (value == null || value === '') return []
  if (typeof value === 'string') {
    const text = stripFence(value).trim()
    if (!text || /^(null|none|\{\}|\[\])$/i.test(text)) return []
    const parsed = tryJsonParse(text)
    if (Array.isArray(parsed)) return asStringList(parsed, limit)
    if (isRecord(parsed)) {
      const line = dictLine(parsed)
      return line ? [line] : []
    }
    if (looksLikeJson(text)) {
      const nested = maybeReportDict(text)
      const line = nested ? dictLine(nested) : ''
      return line ? [line] : []
    }
    const cleaned = cleanText(text)
    return cleaned ? [cleaned] : []
  }
  if (isRecord(value)) {
    const line = dictLine(value)
    return line ? [line] : []
  }
  if (Array.isArray(value)) {
    const out: string[] = []
    for (const item of value) {
      out.push(...asStringList(item, limit))
      if (out.length >= limit) break
    }
    const seen = new Set<string>()
    const result: string[] = []
    for (const item of out) {
      const s = item.trim()
      if (s && !seen.has(s)) {
        seen.add(s)
        result.push(s)
      }
      if (result.length >= limit) break
    }
    return result
  }
  const s = scalarText(value)
  return s ? [s] : []
}

function asConfidence(value: unknown): ReportResult['confidence'] {
  if (typeof value !== 'string') return null
  const v = value.trim()
  if (v === 'high' || v === 'medium' || v === 'low') return v
  const map: Record<string, ReportResult['confidence']> = {
    高: 'high',
    中: 'medium',
    低: 'low',
    高置信度: 'high',
    中置信度: 'medium',
    低置信度: 'low',
  }
  return map[v] ?? null
}

function truncate(s: string, limit: number): string {
  const text = (s ?? '').trim()
  return text.length <= limit ? text : `${text.slice(0, limit).trimEnd()}…`
}

export function normalizeReport(raw: unknown): ReportResult | null {
  try {
    if (raw == null) return null
    if (typeof raw === 'string' && !raw.trim()) return null

    let source = parseObjectLoose(raw)
    if (!source) {
      const salvaged = typeof raw === 'string' ? salvageFields(raw) : {}
      if (Object.keys(salvaged).length) {
        source = salvaged
      } else if (typeof raw === 'string') {
        // 整份载荷是纯文本，说明报告结构已丢失；按后端同一约定，原文只进日志、不回显
        return {
          title: REPORT_FALLBACK_TITLE,
          summary: REPORT_FALLBACK_SUMMARY,
          key_findings: [],
        }
      } else {
        return null
      }
    }

    let data: Record<string, unknown> = { ...source }
    if (!REPORT_FIELD_KEYS.some((k) => k in data)) {
      for (const wrapper of ['report', 'data', 'result']) {
        const inner = maybeReportDict(data[wrapper])
        if (inner) {
          data = { ...inner }
          break
        }
      }
    }

    // 旧降级形态：summary 是整份 JSON，其余字段全空
    const degraded =
      asStringList(data.key_findings).length === 0 &&
      isEmptyValue(data.analysis) &&
      isEmptyValue(data.recommendations) &&
      isEmptyValue(data.confidence)

    let nested = maybeReportDict(data.summary)
    // summary 是被截断的报告 JSON 时无法整体解析，仍按字段抢救（线上实际形态）
    if (!nested && typeof data.summary === 'string' && looksLikeJson(data.summary)) {
      const salvaged = salvageFields(data.summary)
      if (Object.keys(salvaged).length) nested = salvaged
    }
    if (nested) {
      for (const key of REPORT_FIELD_KEYS) {
        if (isEmptyValue(data[key])) data[key] = nested[key]
      }
      if (nested.summary != null) data.summary = nested.summary
      // 降级记录的外层标题是「用户问题」，内层标题更有信息量
      if (degraded && nested.title != null) data.title = nested.title
    }

    let summary = asSummary(data.summary).replace(/\s+/g, ' ').trim()
    summary = truncate(summary, 600)
    const findings = asStringList(data.key_findings)
    const analysis = truncate(asText(data.analysis).trim(), 6000) || null
    const recommendations = asStringList(data.recommendations)
    const title = truncate(asSummary(data.title).replace(/\s+/g, ' ').trim(), 40)

    if (!summary) summary = findings[0] ?? ''
    if (!summary && analysis) summary = '本次分析已完成，详见下方详细分析。'
    if (!summary && recommendations.length) summary = '本次分析已完成，建议如下。'

    if (!summary && !findings.length && !analysis && !recommendations.length) {
      return {
        title: title || REPORT_FALLBACK_TITLE,
        summary: REPORT_FALLBACK_SUMMARY,
        key_findings: [],
      }
    }

    return {
      title: title || REPORT_FALLBACK_TITLE,
      summary,
      key_findings: findings,
      analysis,
      recommendations: recommendations.length ? recommendations : null,
      confidence: asConfidence(data.confidence),
    }
  } catch {
    // 归一化是展示增强：任何异常都不该让页面白屏
    return null
  }
}

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
      else if (evt.type === 'report') {
        // 归一化后仍为空说明数据不可用，宁可不显示卡片也不渲染 JSON 残片
        const report = normalizeReport(evt.data)
        if (report) handlers.onReport?.(report)
      }
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
  // 历史会话里可能存着旧版降级产生的坏报告，读取时统一归一化，无需数据迁移
  return {
    ...data,
    messages: (data.messages ?? []).map((m) => ({ ...m, report: normalizeReport(m.report) })),
  }
}

export async function deleteSession(sessionId: string): Promise<void> {
  await http.delete(`/api/sessions/${enc(sessionId)}`)
}
