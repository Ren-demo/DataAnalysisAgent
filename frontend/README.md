# 智能数据分析系统 —— 前端

Vue 3 + TypeScript + Vite + Element Plus + ECharts 的对话式数据分析界面。

## 快速开始

```bash
npm install      # 首次；要严格复现 package-lock.json 里的版本用 npm ci
npm run dev
```

访问 <http://localhost:5173>。开发服务器会将 `/api` 代理到 `http://localhost:8000`（需先启动后端）。

## 构建

```bash
npm ci           # 按 package-lock.json 精确安装（可复现路径，CI/换机推荐）
npm run build    # vue-tsc 类型检查 + vite 打包
npm run preview  # 预览构建产物
```

> `package.json` 内多为 `^` / `~` 范围，直接 `npm install` 会随着上游发版漂移；`package-lock.json` 已锁定实际解析到的版本并纳入版本控制，需要可复现构建时用 `npm ci`。

## Docker

生产环境不使用 nginx：`server.mjs` 用 Node 内置模块伺服 `dist/`，并把 `/api/**` 反向代理到后端（同源、无 CORS），SSE 字节级透传。运行时镜像不含 `node_modules`。

```bash
# 在项目根目录
docker compose up -d --build frontend
```

容器内 `BACKEND_URL` 默认 `http://backend:8000`，对外端口由根 `.env` 的 `FRONTEND_PORT` 控制（默认 80）。详细说明见根 README「六、Docker 部署」。

## 主要依赖

- 运行时：`vue`、`element-plus`、`@element-plus/icons-vue`、`echarts`、`vue-echarts`、`axios`、`marked`（Markdown 解析）、`dompurify`（HTML 清洗）
- 开发：`vite`、`@vitejs/plugin-vue`、`typescript`、`vue-tsc`

## 功能

- 对话式交互：意图分流后数据查询（SQL 流式）、闲聊（Markdown 流式）、文档检索问答（retrieval）与图表重绘（redraw）分路展示
- 等待文案按后端 `intent` 事件透传的真实意图区分（`Message.phase`）：「正在生成 SQL…」/「正在思考…」/「正在检索文档…」/「正在读取表结构…」/「正在做数据分析…」；意图到达前（分类是一次 2~5s 的 LLM 往返）兜底为**中性**的「正在思考…」，不再默认显示 SQL 字样。`phase` 与渲染家族 `MessageKind` 正交，不参与分区渲染也不进历史
- **实时步骤（`progress` 事件）**：分析 / 决策报告 agent 要跑十几轮 LLM 往返，期间后端逐步下发「正在调用 年均增长率…」「分组聚合 已完成」这类文案。分析阶段滚动显示在 loading 块内（`Message.progress`）；报告阶段**单独开位**（`Message.reportPending` / `reportProgress`）—— `onResult` 已把 `loading` 置 false，不单独标识的话 `result` 与 `report` 之间界面上没有任何指示。进度文案在 `finally` 里兜底清空（报告被关闭 / 报告节点静默跳过 / 用户点「停止」时都不会再有后续事件）
- 多轮对话历史（`history` 传入 `/api/chat/stream`）
- 数据源选择与上传：Excel/CSV（结构化）与 .md/.txt/.pdf/.docx 文档（非结构化，导入 `doc:` 数据源）
- 外部数据库连接管理：前端手动输入主机/端口/库名/账号，测试/保存/编辑（`/api/connections`）
- 数据源管理：统一列出并删除上传文件 / 文档 / 外部连接三类数据源（`/api/datasources/{id}`）
- 会话记忆：`localStorage` 存 `session_id`，左侧可折叠会话侧栏（按相对时间分组）+ 新对话
- 结果表格 + ECharts 图表（bar / line / pie，严格模式：仅显式指定才出图）
- 决策报告卡片（`ReportCard`：标题 / 核心结论 / 关键发现 / 建议 / 置信度）
- 亮 / 暗 / 系统主题切换
- 生成中止（停止按钮）：中止后当前轮标记「（已停止生成）」
- 结果区分「回答」（`explanation`）与「分析说明」（`analysis`）两个区块，明细表格始终保留

## SSE 流式解析（`src/api/index.ts`）

两条流式接口（`/api/query/stream`、`/api/chat/stream`）共用一套解析，几处都是「不做对就会**静默丢事件**、界面只是不动、看不出哪里错」的地方：

- **分隔符**按 `\r\n\r\n | \n\n | \r\r` 切分。只认 `\n\n` 时，经会把换行规范化成 CRLF 的代理/网关后永不命中，除第一个事件外全部丢失，表现为「一直在 loading，最后一闪出结果」。
- **多行 `data:`**：一个事件可有多行 `data:`，按 SSE 规范以换行拼接（只取首行会截断载荷）。
- **解析失败**至少 `console.warn`（静默 `catch {}` 时线上无从查起）；`:` 开头的注释行（如网关发来的心跳）与 `event:` / `id:` / `retry:` 字段被忽略。
- **响应校验**：`!resp.ok` 抛错；`content-type` 不含 `text/event-stream` 也抛错 —— 网关/反代出错时常返回 200 + HTML 错误页，不校验时前端只会一直转圈。
- **空闲超时** 600s，是**两次事件之间**的最大间隔而非整条流的总时长（查询 + 报告的正常链路本就可能跑几分钟）。`fetch` 没有 axios 那种 `timeout`，不设时后端卡死会让 `reader.read()` 永久挂起。超时触发时先 `reader.cancel()` 再抛错，避免 read 仍挂着、回调留在内存里。
  取值必须**明显大于后端最长静默**：`want_sql` 为假时 text2sql 的 token 不发，schema 反射 → SQL 生成 → 执行 → 结果分析全程零事件，实测复杂提问的 `intent` → `result` 静默可达 **205 秒**，其后的报告 agent 还会串行跑若干轮工具调用。早前取 120s，健康的慢查询会被误杀（界面上「停止」按钮始终可用，所以取大值不会困住用户）。
  **后端已加保活**：`/api/chat/stream` 静默期每 10s 下发一行 `: keepalive` 注释行，它同样会让 `reader.read()` 返回并重置本计时器，所以 600s 的实际含义已降级为「**进程真的不可达**」的兜底，而不是「慢任务」的判据。
- **错误分类（`StreamError`）**：上述失败各自带面向用户的 `message` 抛出，`HomeView.vue` 直接展示 —— 空闲超时说「响应超时」、网关问题说「返回了非流式内容」。**只有 `fetch` 自身的网络层失败**（服务未启动 / 端口不通 / 被 CORS 拦下）才显示「网络请求失败，请确认后端服务已启动」；非 `StreamError`（代码缺陷）不把技术细节抛给用户。
- **请求中止**经 `AbortSignal` 透传（停止按钮 / 切会话 / 组件卸载）。

## 会话切换与竞态防护（`src/views/HomeView.vue`）

- 每次发送自增 `streamSeq`，该轮所有流回调（`onIntent` / `onToken` / `onAnswerToken` / `onResult` / `onReport` / `onError`）写状态前先校验序号，且 `messages.value[idx]` 一律做存在性判断 —— 否则流式中途点「新建对话」，`messages.value` 被换成空数组，回调访问 `undefined.kind` 会抛 `TypeError` 白屏。
- 会话切换 / 新建 / 删除入口统一调用 `cancelActiveStream()`（自增 `streamSeq` + `abort()` 在途流）；`onStop()` **只** abort 不自增序号，以便中止分支能正常写入「（已停止生成）」。
- `onSelectSession` / `restoreSession` 另用 `sessionLoadSeq` 守卫，`await` 之后校验序号再赋值 —— 否则快速点 A→B 时 A 的响应后到，界面显示 A 而 `sessionId` 是 B，下一轮会把 A 的上下文发给 B。
- 组件 `onUnmounted` 中自增序号并 abort 在途流。
- `useDark()` 是**刻意**的模块级单例（主题属于整个应用，无「组件卸载时 dispose」之物）；唯一真实的重复注册来源是 Vite HMR，故用 `import.meta.hot?.dispose(...)` 显式摘掉旧监听（生产构建下这段会被摇掉）。

## 目录结构

```
src/
├── main.ts               应用入口（挂载 Element Plus + 样式）
├── App.vue               根组件（渲染 HomeView）
├── env.d.ts              Vite 类型声明
├── api/index.ts          axios 封装 + fetch SSE（consumeSSE/streamPost 统一解析 + 空闲超时）；导出 StreamError 与 14 个函数：queryData/queryDataStream/chatStream、listDataSources/uploadDataSource/deleteDataSource、listConnections/testConnection/createConnection/updateConnection/deleteConnection、listSessions/getSession/deleteSession；路径参数一律 encodeURIComponent（数据源 id 保留原始文件名，含 # / ? 时不编码会被 URL 截断）
├── types.ts              与后端契约一致的类型定义（MessageKind 渲染家族 / MessagePhase 等待文案阶段，二者正交；Message.progress/reportProgress/reportPending）
├── composables/
│   └── useDark.ts        主题切换（light/dark/system，模块级单例）
├── views/
│   └── HomeView.vue      对话页：消息状态 + 发送/停止 + 数据源选择 + 上传 + 数据源管理弹窗 + 主题 + 常驻左侧会话侧栏 + 连接数据库弹窗 + 决策报告回写；onProgress 按 scope 分流写 progress / reportProgress，finally 兜底清空进度文案
├── components/
│   ├── ChatInput.vue     输入框 + 发送/停止按钮
│   ├── MessageList.vue   消息列表（用户气泡 / 助手回答 / 错误提示 / Markdown）；loading 块滚动显示 agent 步骤文案，result 与 report 之间另有报告占位块
│   ├── SqlResult.vue     回答（explanation）+ 分析说明（analysis）+ 数据表格（SQL 默认不展示，仅用户明确要求时出现代码块）
│   ├── ChartCard.vue     按 chart_type 渲染 bar/line/pie
│   ├── ReportCard.vue    决策报告卡片（title/summary/key_findings/analysis/recommendations）
│   ├── MarkdownText.vue  Markdown 答案渲染（marked + DOMPurify）
│   ├── SessionSidebar.vue 会话侧栏（新对话按钮 + 按相对时间分组的历史列表）
│   ├── WelcomePanel.vue  欢迎面板 + 示例提问
│   ├── ConnectionDialog.vue 连接数据库弹窗（表单 + 测试/保存/编辑 + 表结构展示）
│   └── DataSourceDialog.vue 数据源管理弹窗（列出三类数据源 + 删除）
└── styles/
    ├── base.css          全局基础样式
    └── theme.css         设计 token（亮/暗主题）
```
