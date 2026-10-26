export type ChartType = 'bar' | 'line' | 'pie' | 'table' | null
/** 渲染家族：'query' 出表格/图表/SQL，'chat' 出自然语言答案。也是 QueryResponse.kind 的契约。 */
export type MessageKind = 'query' | 'chat'
/**
 * 等待文案的阶段标签，与 MessageKind **正交**：仅驱动 loading 期间的提示文字，
 * 不参与渲染分区，也不会写进 QueryResponse.kind / SessionMessage.kind。
 * 由后端 intent 事件透传（redraw 归入 'query'）。
 *
 * 'analysis' 必须与 'query' 分开：统计链路一次 SQL 都不生成（走 pandas 统计算子），
 * 压成 'query' 会让整段分析全程显示「正在生成 SQL…」。
 */
export type MessagePhase = 'query' | 'chat' | 'retrieval' | 'schema' | 'analysis'
export type Confidence = 'high' | 'medium' | 'low' | null

export interface ReportResult {
  title: string
  summary: string
  key_findings: string[]
  analysis?: string | null
  recommendations?: string[] | null
  confidence?: Confidence
}

export interface SourceRef {
  title: string
  block_id: string
}

export interface QueryResponse {
  question: string
  kind: MessageKind
  answer: string | null
  sql: string | null
  columns: string[]
  rows: (string | number | null)[][]
  row_count: number
  chart_type: ChartType
  explanation: string | null
  analysis: string | null
  error: string | null
  error_code: string | null
  sources: SourceRef[]
}

export interface DataSourceInfo {
  id: string
  name: string
  type: string
}

export type DbType = 'mysql' | 'postgresql'

export interface TableColumnInfo {
  name: string
  type: string
}

export interface TableInfo {
  name: string
  columns: TableColumnInfo[]
}

export interface RelationshipInfo {
  from_table: string
  from_column: string
  to_table: string
  to_column: string
  kind: 'fk' | 'inferred'
}

export interface ConnectionInfo {
  id: string
  name: string
  type: DbType
  host: string
  port: number
  database: string
  user: string
}

export interface ConnectionCreate {
  name: string
  type: DbType
  host: string
  port: number | null
  database: string
  user: string
  password: string
}

export interface ConnectionTestResult {
  ok: boolean
  tables: TableInfo[]
  relationships: RelationshipInfo[]
  error: string | null
  error_code: string | null
}

export interface ConnectionCreateResult {
  id: string
  name: string
  type: string
  tables: TableInfo[]
  relationships: RelationshipInfo[]
  error: string | null
  error_code: string | null
}

export interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  kind?: MessageKind
  phase?: MessagePhase
  result?: QueryResponse
  answer?: string
  report?: ReportResult | null
  sources?: SourceRef[]
  loading?: boolean
  streamingSql?: string
  streamingText?: string
  /** 分析 agent 的实时步骤（「正在调用 group_cagr…」），loading 期间展示 */
  progress?: string
  /**
   * 决策报告阶段单独开位：onResult 会把 loading 置 false，而 loading 块受
   * v-if="msg.loading" 守卫 —— result 与 report 之间报告 agent 还要跑多轮 LLM，
   * 不单独标识的话这段时间界面上没有任何指示。
   */
  reportPending?: boolean
  reportProgress?: string
}

export interface StreamHandlers {
  onIntent?: (intent: MessagePhase) => void
  onToken: (content: string) => void
  onAnswerToken?: (content: string) => void
  onResult: (result: QueryResponse) => void
  onReport?: (report: ReportResult) => void
  /** 后端 progress 事件：分析 / 报告阶段的步骤文案，scope 为 'analysis' | 'report' */
  onProgress?: (text: string, scope: string) => void
  onError: (message: string, code: string) => void
}

export interface SessionMessage {
  role: 'user' | 'assistant'
  content: string
  kind?: MessageKind
  data_source_id?: string | null
  sql?: string | null
  result?: QueryResponse | null
  report?: ReportResult | null
  sources?: SourceRef[] | null
  created_at?: string | null
}

export interface SessionSummary {
  session_id: string
  title: string
  last_data_source_id: string | null
  message_count: number
  created_at: string | null
  updated_at: string | null
}

export interface SessionDetail {
  session_id: string
  last_data_source_id: string | null
  created_at: string | null
  updated_at: string | null
  messages: SessionMessage[]
}
