# 系统架构说明

> 赛题 1「智能数据分析系统」· 初级 · 前后端分离架构

## 一、分层架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                            前端 SPA                                 │
│        Vue 3 + TypeScript + Vite + Element Plus + ECharts           │
│        数据源选择 / 上传 · 亮暗主题 · Markdown 回答 · 流式对话        │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │  查询面板     │  │  结果表格     │  │  图表渲染（bar/line/pie）   │  │
│  │ (自然语言输入) │  │  (结构化结果) │  │  (vue-echarts)            │  │
│  └──────┬───────┘  └──────▲───────┘  └──────────▲────────────────┘  │
│         │  POST /api/chat/stream  (SSE)          │                  │
│         │                 └─── QueryResponse ────┘                  │
└─────────┼───────────────────────────────────────────────────────────┘
          │  HTTP (REST) · axios / fetch SSE · Vite dev 代理 /api → :8000
┌─────────▼───────────────────────────────────────────────────────────┐
│                        后端 FastAPI（Uvicorn）                        │
│                                                                     │
│  routers/meta.py ── GET /health · GET /schema                       │
│  routers/chat.py ── POST /query · /query/stream · /chat/stream      │
│  routers/datasource.py ── GET /datasources · POST /datasources/upload│
│                                                                     │
│  ┌─────────────────────────── 服务层 ────────────────────────────┐  │
│  │  intent.py          意图分类（query/chat/redraw/retrieval）     │  │
│  │  query_service.py   查询编排（取结构→生成SQL→执行→推断图表）     │  │
│  │  chat_service.py    闲聊/元数据咨询（schema 感知、流式）        │  │
│  │  text2sql.py        自然语言→SQL（多方言 few-shot+校验+重试）   │  │
│  │  agents/            LangGraph 编排 + create_agent 决策报告     │  │
│  │  chart_service.py   图表类型推断（严格模式）                    │  │
│  │  datasource_service.py  Excel/CSV 上传导入 SQLite              │  │
│  │  errors.py          统一业务异常（error_code + 友好中文提示）   │  │
│  └──────────────┬──────────────────────────────┬────────────────┘  │
│                 │                              │                    │
│        ┌────────▼─────────┐          ┌─────────▼─────────┐          │
│        │  数据层           │          │  LLM 层           │          │
│        │  SQLAlchemy 2.x  │          │  langchain-openai │          │
│        │  pandas          │          │  ChatOpenAI       │          │
│        │  SQLite/MySQL/PG │          │  (DeepSeek)       │          │
│        └────────┬─────────┘          └─────────┬─────────┘          │
└─────────────────┼──────────────────────────────┼────────────────────┘
                  │                              │
        ┌─────────▼─────────┐          ┌─────────▼──────────────┐
        │  backend/data/    │          │  DeepSeek API          │
        │  uploads.db       │          │  https://api.deepseek.com│
        │  MongoDB sessions │          │                        │
        └───────────────────┘          └────────────────────────┘
```

## 二、数据流（一次对话的完整链路）

> 编排已迁移到 LangGraph `StateGraph`（`app/agents/graph.py`）：`classify → 条件路由（query/chat/redraw/retrieval/analysis/schema）`，query 分支 `prepare_query → text2sql → execute → report`，analysis 分支 `prepare_query → analysis`（复用同一份已收窄的 schema），另有 `chat` / `retrieve` / `redraw` / `schema` 四条直通分支。下图中各步骤即对应图内节点，事件经 `stream_mode="custom"` + `get_stream_writer` 输出为 SSE。
>
> **SSE 保活**：`/api/chat/stream` 的图驱动跑在独立任务里、事件经队列搬运，生成器按 `sse_heartbeat_seconds`（默认 10s）超时取；静默期下发 `: keepalive` 注释行，前端与 `batch_answer.py` 均按既有的 `startsWith(':')` 逻辑跳过。analysis 与决策报告 agent 要跑十几轮 LLM 往返，此前全程零事件会把前端的 600s **空闲**超时踩满。**后端不设总时长上限**——只有 agent 步数兜底（`agent_recursion_limit`，默认 40，langgraph 默认 10000 等于不设限）。

```
用户提问
   │  "统计各分类的数量"（或闲聊，如「我们有哪些数据？」）
   ▼
[1] 前端 POST /api/chat/stream  {"question":"...", "data_source_id":"upload:<表名>", "history":[...]}
   ▼
[2] intent.classify_intent(question, history, data_source_type)
   │   → 返回 intent ∈ {query, chat, redraw, retrieval, analysis, schema}，并识别 chart_hint（bar/line/pie 或 None）
   │   → 先发出 SSE `intent` 事件告知前端分流结果（前端仅分 query/chat 两类渲染，
   │      intent 另驱动等待文案 MessagePhase，含 analysis）
   ├─ intent == query ──────────────────────────────────────────────┐
   │                                                                 │
   │ [3] prepare_query 节点：get_engine() → get_dialect() → build_schema_text()│
   │   │   （按数据源解析方言 sqlite/mysql/postgres，读取表结构）      │
   │   ▼                                                             │
   │ [4] text2sql.generate_sql_stream(question, schema_text, dialect) │
   │   ├─ 多方言 few-shot prompt → DeepSeek 流式生成 SQL token         │
   │   │   → sqlglot 只读校验 → 不合法回填错误并重试（限 3 次）        │
   │   ├─ 模型不可用 → AppError（NO_API_KEY / LLM_UNAVAILABLE）       │
   │   └─ 多次校验失败 → AppError（SQL_ERROR）                        │
   │   ▼                                                             │
   │ [5] execute 节点：pandas 执行 SQL → columns / rows / row_count    │
   │   │   → 严格模式：仅显式指定才返回 bar/line/pie，否则 table       │
   │   ▼                                                             │
   │ [6] 组装 QueryResponse → SSE `result` 事件                       │
   │ [7] report 节点：create_agent 对结果多步下钻推理                  │
   │     → 每轮工具调用发 SSE `progress`（scope=report）              │
   │     → 结构化报告 → SSE `report` 事件（前端经 `ReportCard` 展示）  │
   ├─ intent == analysis ───────────────────────────────────────────┐
   │   [7b] analysis 节点：create_agent + 预定义统计算子（只读 pandas/numpy）│
   │       → 每轮工具调用发 SSE `progress`（scope=analysis）          │
   │       → 失败回退到 text2sql 链路；成功 → result（含 analysis 说明）│
   ├─ intent == retrieval ──────────────────────────────────────────┐
   │   [8] retrieve 节点：retrieve_doc → retrieval_stream(question, context)│
   │       → Milvus 向量检索 top-k 片段 → 流式 answer_token → result   │
   ├─ intent == redraw ─────────────────────────────────────────────┐
   │   [9] redraw 节点：复用本会话最近一次查询结果出图（跳过 SQL）      │
   │       → result（无结果时 NO_PRIOR_RESULT）                      │
   ├─ intent == schema ─────────────────────────────────────────────┐
   │   [10b] schema 节点：build_schema_overview → schema_consult_stream│
   │        → SSE `answer_token` → `result`（LLM 不可用时确定性反射兜底）│
   └─ intent == chat ───────────────────────────────────────────────┐
       [10] chat_service.chat_stream(question, history, schema_text, summary)│
           → DeepSeek 流式生成回答 token → SSE `answer_token` → `result`│
       └─────────────────────────────────────────────────────────────┘
   ▼
[11] 若发生 AppError，发 SSE `error` 事件（含 error / error_code，不抛 500）
   ▼
[12] 前端解析：query 展示表格 + 图表（按 chart_type）；chat 渲染 Markdown；
         错误则 ElNotification 提示
   ▼
[13] 后端 astream 结束后若带 session_id 且 result 无 error，写入会话记忆（MongoDB，失败静默跳过）
```

## 三、模块职责与文件对应关系

| 文件 | 职责 | 关键函数 / 模型 |
| --- | --- | --- |
| `app/config.py` | 配置管理，加载项目根目录 `.env` | `Settings`（deepseek_* / mongo_* / embedding_* / milvus_* / chat_history_* / doc_* / web_search_* / knowledge_base_enabled / `sse_heartbeat_seconds`（SSE 保活间隔，默认 10）/ `agent_recursion_limit`（agent 步数兜底，默认 40））；`settings` 单例 |
| `app/schemas.py` | Pydantic v2 请求/响应模型 | `ColumnInfo` / `TableInfo` / `DataSourceInfo` / `SchemaResponse` / `QueryRequest` / `ChatRequest` / `ChatMessage` / `QueryResponse`（含 `kind`/`answer`/`error_code`/`chart_type`） |
| `app/errors.py` | 统一业务异常 | `AppError`（携带 `message` 与 `error_code`） |
| `app/database/engine.py` | 多数据源路由 + 引擎缓存 + 结构读取 + 关联关系识别 | `get_engine()` / `get_dialect()` / `get_schema_info()` / `build_schema_text()` / `resolve_data_source_type()` |
| `app/database/mongo.py` | MongoDB 连接（sessions 会话记忆 + connections 连接） | `get_client()` / `get_sessions_collection()` / `get_connections_collection()` / `ensure_indexes()` / `close_client()` |
| `app/database/milvus.py` | Milvus 向量库（langchain-milvus） | `get_vector_store()` / `vector_index()` / `vector_search()` |
| `app/database/datasource_config.py` | 加载外部数据源配置（静态 + 运行时合并） | `get_datasource_by_id()` / `list_external_datasources()` / `set_runtime_datasources()` |
| `app/llm/client.py` | LLM 客户端工厂 | `get_llm() -> ChatOpenAI`（无 Key / 构造失败抛 `AppError`） |
| `app/llm/embedding.py` | 外部 embedding 客户端（向量检索硬依赖） | `get_embeddings() -> OpenAIEmbeddings` |
| `app/llm/intent.py` | 意图分类 + 图表类型识别 | `classify_intent(question, history, data_source_type)` → `IntentResult(intent, chart_hint)`，五类 query/chat/redraw/retrieval/analysis，带关键词规则兜底 |
| `app/llm/text2sql.py` | 自然语言 → SQL（多方言） | `generate_sql()` / `generate_sql_stream()`（few-shot + sqlglot + 重试）；`validate_sql()` / `extract_sql()` |
| `app/llm/summary.py` | 滚动摘要生成 | `generate_summary(previous_summary, new_messages)`（旧对话压缩，静默降级） |
| `app/llm/title.py` | 会话标题生成 | `generate_title(question)`（LLM 概括首条问题 + 截断兜底） |
| `app/agents/graph.py` | LangGraph 编排图 | `build_graph()` / `graph`（classify/prepare_query/text2sql/execute/report/redraw/chat/retrieve/analysis 9 节点） |
| `app/agents/state.py` | 图状态定义 | `OrchestratorState`（question/intent/engine/sql/result/report 等） |
| `app/agents/tools.py` | 报告 agent 工具集 | `build_tools()`（get_schema/generate_sql/execute_sql + 可选 web_search/knowledge_search，只读守卫） |
| `app/agents/report.py` | 决策报告模型与解析 | `ReportResult` / `parse_report()`（DeepSeek 思考模式用手动 JSON 解析） |
| `app/agents/stat_tools.py` | 统计建模算子 | `build_stat_tools()`（get_schema/load_table（支持 `filters` 多条件 AND）/group_aggregate/group_correlate/group_mom_growth/group_mom_growth_rate/group_cagr（年均增长率 CAGR）/group_share/group_vs_overall_avg/intersect_groups（条件值可引用另一列）/linear_regression/forecast_time_series/detect_anomalies/cluster_kmeans，只读，df_store 请求级隔离） |
| `app/agents/analysis.py` | 分析结果模型与解析 | `AnalysisResult` / `parse_analysis()` / `_ANALYSIS_SYSTEM_PROMPT` / `build_analysis_system_prompt()`（注入 schema、要求一次性规划并行调用；DeepSeek 思考模式用手动 JSON 解析） |
| `app/services/chart_service.py` | 图表类型推断（严格模式） | `suggest_chart_type()` |
| `app/services/query_service.py` | 查询执行 | `run_query()` / `execute_sql()` / `_to_json_safe()` |
| `app/services/chat_service.py` | 闲聊 / 元数据咨询 + 文档问答 + 查询结果简短分析 | `chat_stream(question, history, schema_text, summary)` / `retrieval_stream(question, context)` / `summarize_query_result(question, columns, rows, row_count)` |
| `app/services/context_service.py` | 闲聊上下文治理 | `build_chat_context()`（近期窗口截断 + 滚动摘要编排） |
| `app/services/datasource_service.py` | Excel/CSV 上传导入 | `import_file()` / `list_datasources()` |
| `app/services/connection_service.py` | 外部数据库连接管理 | `inspect_external()` / `list_saved_connections()` / `save_connection()` / `delete_connection()` / `seed_runtime_datasources()` |
| `app/services/document_service.py` | 非结构化文档解析 + 检索 | `import_document()` / `get_doc_tables()` / `retrieve_doc()`（Milvus 向量检索） |
| `app/services/chunking.py` | 文本分块 | `chunk_text()`（滑动窗口重叠分块） |
| `app/services/knowledge_service.py` | 本地知识库检索 | `load_corpus()` / `search_knowledge()`（Milvus 向量检索） |
| `app/services/memory_service.py` | 会话记忆读写 | `record_turn()` / `list_sessions()` / `get_session()` / `delete_session()` / `attach_report()` / `get_last_query_result()` / `get_summary_state()` / `save_summary_state()` |
| `app/routers/memory.py` | 会话记忆路由 | `GET /api/sessions`、`GET /api/sessions/{session_id}`、`DELETE /api/sessions/{session_id}` |
| `app/routers/meta.py` | 元信息路由 | `GET /api/health`、`GET /api/schema` |
| `app/routers/chat.py` | 对话/查询路由 | `POST /api/query`、`POST /api/query/stream`、`POST /api/chat/stream` |
| `app/routers/datasource.py` | 数据源管理路由 | `GET /api/datasources`、`POST /api/datasources/upload` |
| `app/routers/connections.py` | 外部连接路由 | `GET/POST /api/connections`、`POST /api/connections/test`、`PUT/DELETE /api/connections/{ds_id}` |
| `app/main.py` | 应用入口 | 挂载 `meta.router`、`chat.router`、`datasource.router`、`connections.router`、`memory.router`（均 prefix=`/api`） |

## 四、准确性保障机制

为保证「自然语言 → SQL」的查询准确性（赛题客观评分依据），后端内置多层保障：

1. **多方言 few-shot 通用示例**：每方言 11 组（6 通用 + 3 JOIN + 2 日期，sqlite/mysql/postgres 各一组），以抽象占位表/列名（`t`/`category`/`value`/`date_col`）演示 SQL 结构与函数写法（含多表关联 JOIN），引导模型按真实表结构生成符合方言日期/字符串/关联的 SQL。
2. **sqlglot 语法校验**：LLM 输出后先做语法与方言校验，并强制只读（仅 SELECT / WITH），非法 SQL 直接拦截。
3. **失败自动重试**：校验不通过时，将错误信息回传给模型，要求其修正后重新生成（限 3 次）。

## 五、错误处理（不设规则兜底）

模型不可用时，**不**静默降级为规则匹配伪造结果，而是通过统一异常 `AppError` 返回用户友好的系统错误提示：

| error_code | 触发场景 | 用户提示（示例） |
| --- | --- | --- |
| `NO_API_KEY` | 未配置 `DEEPSEEK_API_KEY` | 「智能问答服务暂未开通，请联系管理员完成配置后重试。」（配置项写在项目根目录 `.env`） |
| `LLM_UNAVAILABLE` | 初始化失败 / 网络异常 / 超时 | 「大模型服务暂时不可用，请稍后重试或检查网络与 API Key 配置。」 |
| `SQL_ERROR` | 生成的 SQL 无法执行或多次校验未通过 | 「多次生成 SQL 均未通过语法校验，请换一种方式描述问题。」 |
| `DATASOURCE_UNAVAILABLE` | 外部数据源不可用（连接失败等） | 「数据源不可用，请检查连接配置。」 |
| `DATASOURCE_CONNECT_FAILED` | 新增/编辑连接时测试连接失败（主机/端口/账号/库名不可用） | 「连接失败：请检查主机、端口、用户名、密码与数据库名是否正确。」 |
| `NO_PRIOR_RESULT` | 重绘图表前当前会话尚无可用查询结果 | 「当前会话还没有可复用的查询结果，请先进行一次数据查询。」 |
| `SYSTEM_ERROR` | 其它未预期异常 | 「系统出现异常，请稍后重试。」 |

后端始终返回 HTTP 200 + JSON（`error` / `error_code` 字段），前端据此弹出 `ElNotification` 并在消息区展示 `el-alert`，不暴露内部堆栈细节。

## 六、扩展方向

| 方向 | 说明 |
| --- | --- |
| 外部数据源验证 | 在 `datasources.json` 填入真实 MySQL/PostgreSQL 连接，端到端验证外部数据源查询 |
| 中级能力 | 统计分析、关联查询、公式计算（扩充 few-shot 复杂 SQL 模式） |
| 高级能力 | ✅ 已实现：自动数据建模（线性回归 / 时序预测 / 异常检测 / K-Means 聚类，`app/agents/stat_tools.py`，numpy 手写） |
| 决策报告 | ✅ 已实现（LangGraph + `create_agent` 下钻推理产出 `report` 事件；可选 web_search 联网搜索 + knowledge_search 本地知识库检索，前端经 `ReportCard` 展示） |
| 查询准确性 | 扩充 few-shot 样本、引入查询结果校验、执行失败自动纠错重试 |
