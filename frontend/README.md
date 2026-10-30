# 智能数据分析系统 —— 前端

Vue 3 + TypeScript + Vite + Element Plus + ECharts 构建的对话式数据分析界面。前端通过 SSE 接收后端结果，展示 SQL 流式输出、结果表格、图表与决策报告，与 `backend/` 的 FastAPI 服务配合使用。

## 快速开始

### 前置条件

- 已安装 Node.js 与 npm。
- 后端已在 `http://localhost:8000` 运行。前端只做请求代理，不内置数据。

### 本地开发

```bash
npm install
npm run dev
```

浏览器访问 `http://localhost:5173`。开发服务器会把 `/api` 代理到 `http://localhost:8000`。

### 构建与预览

```bash
npm ci           # 按 package-lock.json 精确安装（CI / 换机推荐）
npm run build    # vue-tsc 类型检查 + vite 打包
npm run preview  # 预览构建产物
```

`package.json` 中的依赖多为 `^` / `~` 范围，直接 `npm install` 会随上游发版漂移；需要可复现构建时用 `npm ci`。

## Docker 部署

生产环境不使用 nginx：`server.mjs` 用 Node 内置模块伺服 `dist/`，并把 `/api/**` 反向代理到后端（同源、无 CORS），SSE 字节级透传。运行时镜像只包含构建产物与 `server.mjs`，不含 `node_modules`。

在项目根目录执行：

```bash
docker compose up -d --build frontend
```

| 配置项 | 说明 |
| --- | --- |
| `BACKEND_URL` | 后端地址，容器内默认 `http://backend:8000` |
| `FRONTEND_PORT` | 对外端口，由根目录 `.env` 控制，默认 `80` |

完整部署说明见根目录 README 的「六、Docker 部署」。

## 功能

- **对话分析**：自动识别意图并分流——数据查询（SQL 流式输出）、闲聊、文档检索问答、图表重绘；支持多轮上下文；分析 / 报告过程中显示实时步骤；随时可点「停止」。
- **数据接入**：上传 Excel / CSV；导入 `.md` / `.txt` / `.pdf` / `.docx` 文档；连接外部数据库（手动填写主机、端口、库名、账号）；三类数据源统一列出与删除。
- **结果呈现**：回答（explanation）与分析说明（analysis）分区展示，明细表格常驻；严格图表模式，仅显式指定时输出 `bar` / `line` / `pie`；决策报告卡片展示标题、核心结论、关键发现、建议与置信度。
- **会话与界面**：历史会话侧栏按相对时间分组，支持新建、切换、删除；亮 / 暗 / 系统主题切换；等待文案随真实意图变化（如「正在生成 SQL…」「正在检索文档…」）。

字段级实现说明见文末「实现细节补充」。

## 目录结构

```text
src/
├── main.ts              应用入口
├── App.vue              根组件
├── types.ts             前后端契约类型
├── api/index.ts         axios 封装 + SSE 客户端
├── composables/
│   └── useDark.ts       主题切换
├── views/
│   └── HomeView.vue     对话页
├── components/          组件（见下表）
└── styles/              全局样式与主题 token
```

| 分组 | 组件 | 职责 |
| --- | --- | --- |
| 对话壳 | `ChatInput` | 输入框与发送 / 停止按钮 |
| 对话壳 | `MessageList` | 消息列表、加载与报告占位 |
| 对话壳 | `SessionSidebar` | 会话侧栏与新建对话 |
| 对话壳 | `WelcomePanel` | 欢迎面板与示例提问 |
| 结果展示 | `SqlResult` | 回答、分析说明与数据表格 |
| 结果展示 | `ChartCard` | 按类型渲染柱状 / 折线 / 饼图 |
| 结果展示 | `ReportCard` | 决策报告卡片 |
| 结果展示 | `MarkdownText` | Markdown 渲染与 HTML 清洗 |
| 弹窗 | `ConnectionDialog` | 外部数据库连接管理 |
| 弹窗 | `DataSourceDialog` | 数据源列表与删除 |

## 技术栈与依赖

| 分类 | 依赖 | 用途 |
| --- | --- | --- |
| 框架 | `vue` / `element-plus` / `@element-plus/icons-vue` | 组件库与图标 |
| 图表 | `echarts` / `vue-echarts` | 结果可视化 |
| 网络与渲染 | `axios` / `marked` / `dompurify` | HTTP 请求、Markdown 解析、HTML 清洗 |
| 构建 | `vite` / `@vitejs/plugin-vue` / `typescript` / `vue-tsc` | 开发服务器、打包与类型检查 |

版本以 `package.json` 为准。

## 开发附录

### 实现细节补充

- **等待文案**：`Message.phase` 与渲染家族 `MessageKind` 正交，不参与分区渲染也不进历史。`phase` 取值为 `query` / `chat` / `retrieval` / `schema` / `analysis`；`intent` 事件到达前兜底为「正在思考…」，避免每次提问的前几秒都显示 SQL 相关文案。
- **实时进度**：分析阶段的步骤文案写入 `Message.progress`；报告阶段因 `onResult` 已把 `loading` 置 false，另开 `Message.reportPending` / `reportProgress` 占位。所有进度文案在 `finally` 中兜底清空（报告关闭、报告节点跳过、用户停止时都不会再收到后续事件）。
- **会话标识**：`session_id` 存 `localStorage`（key `chongqing_ai_session_id`）。`crypto.randomUUID` 仅在安全上下文（HTTPS / localhost）可用，非安全上下文用 `crypto.getRandomValues` 兜底生成，避免 `session_id` 恒为空、历史会话无法保存。
- **API 客户端**：`api/index.ts` 导出 `StreamError` 与 14 个请求函数，路径参数一律经 `encodeURIComponent` 编码（数据源 id 保留原始文件名，含 `#` / `?` 时不编码会被 URL 截断）。

### SSE 流式解析（src/api/index.ts）

两条流式接口（/api/query/stream、/api/chat/stream）共用一套解析，几处都是「不做对就会**静默丢事件**、界面只是不动、看不出哪里错」的地方：

- **分隔符**按 \r\n\r\n | \n\n | \r\r 切分。只认 \n\n 时，经会把换行规范化成 CRLF 的代理/网关后永不命中，除第一个事件外全部丢失，表现为「一直在 loading，最后一闪出结果」。
- **多行 `data:`**：一个事件可有多行 data:，按 SSE 规范以换行拼接（只取首行会截断载荷）。
- **解析失败**至少 console.warn（静默 catch {} 时线上无从查起）；`:` 开头的注释行（如网关发来的心跳）与 `event:` / `id:` / `retry:` 字段被忽略。
- **响应校验**：`!resp.ok` 抛错；`content-type` 不含 `text/event-stream` 也抛错 —— 网关/反代出错时常返回 200 + HTML 错误页，不校验时前端只会一直转圈。
- **空闲超时** 600s，是**两次事件之间**的最大间隔而非整条流的总时长（查询 + 报告的正常链路本就可能跑几分钟）。`fetch` 没有 axios 那种 `timeout`，不设时后端卡死会让 `reader.read()` 永久挂起。超时触发时先 `reader.cancel()` 再抛错，避免 read 仍挂着、回调留在内存里。
  取值必须**明显大于后端最长静默**：want_sql 为假时 text2sql 的 token 不发，schema 反射 → SQL 生成 → 执行 → 结果分析全程零事件，实测复杂提问的 intent → result 静默可达 **205 秒**，其后的报告 agent 还会串行跑若干轮工具调用。早前取 120s，健康的慢查询会被误杀（界面上「停止」按钮始终可用，所以取大值不会困住用户）。
  **后端已加保活**：/api/chat/stream 静默期每 10s 下发一行 `: keepalive` 注释行，它同样会让 `reader.read()` 返回并重置本计时器，所以 600s 的实际含义已降级为「**进程真的不可达**」的兜底，而不是「慢任务」的判据。
- **错误分类（`StreamError`）**：上述失败各自带面向用户的 `message` 抛出，`HomeView.vue` 直接展示 —— 空闲超时说「响应超时」、网关问题说「返回了非流式内容」。**只有 `fetch` 自身的网络层失败**（服务未启动 / 端口不通 / 被 CORS 拦下）才显示「网络请求失败，请确认后端服务已启动」；非 `StreamError`（代码缺陷）不把技术细节抛给用户。
- **请求中止**经 `AbortSignal` 透传（停止按钮 / 切会话 / 组件卸载）。

### 会话切换与竞态防护（src/views/HomeView.vue）

- 每次发送自增 `streamSeq`，该轮所有流回调（`onIntent` / `onToken` / `onAnswerToken` / `onResult` / `onReport` / `onError`）写状态前先校验序号，且 `messages.value[idx]` 一律做存在性判断 —— 否则流式中途点「新建对话」，`messages.value` 被换成空数组，回调访问 `undefined.kind` 会抛 `TypeError` 白屏。
- 会话切换 / 新建 / 删除入口统一调用 `cancelActiveStream()`（自增 `streamSeq` + `abort()` 在途流）；`onStop()` **只** abort 不自增序号，以便中止分支能正常写入「（已停止生成）」。
- `onSelectSession` / `restoreSession` 另用 `sessionLoadSeq` 守卫，`await` 之后校验序号再赋值 —— 否则快速点 A→B 时 A 的响应后到，界面显示 A 而 `sessionId` 是 B，下一轮会把 A 的上下文发给 B。
- 组件 `onUnmounted` 中自增序号并 abort 在途流。
- `useDark()` 是**刻意**的模块级单例（主题属于整个应用，无「组件卸载时 dispose」之物）；唯一真实的重复注册来源是 Vite HMR，故用 `import.meta.hot?.dispose(...)` 显式摘掉旧监听（生产构建下这段会被摇掉）。

## 相关文档

| 文档 | 内容 |
| --- | --- |
| [根目录 README](../README.md) | 系统总览与部署（含 Docker 细节） |
| [后端 README](../backend/README.md) | 后端接口与分析链路 |
