# 智能数据分析系统 —— 后端

FastAPI 后端，职责是把用户的自然语言问题变成可执行、可解释的数据分析结果。

一次提问的处理过程：

```text
自然语言 → 意图分类 → LangGraph 编排 → 分支执行 → SSE 流式返回
```

按意图分成五条分支：

| 分支 | 做什么 |
| --- | --- |
| 数据查询 | 组装 schema → 多方言 text2sql 流式生成 → 只读校验 → 执行 → 自然语言作答 → 图表判定 → 可选决策报告 |
| 统计建模 | 用 pandas / numpy 预定义算子做回归、预测、异常检测、聚类等 |
| 文档检索 | Milvus 向量检索 + 流式回答 |
| 表结构咨询 | 确定性反射 + 模型作答，回答列名 / 列序 / 行数 |
| 闲聊 | 带上下文治理的流式对话 |

## 本文导航

- [环境要求与快速开始](#环境要求与快速开始)
- [代码结构](#代码结构)
- [接口契约](#接口契约)
- [数据源](#数据源)
- [会话记忆与上下文治理](#会话记忆与上下文治理)
- [输出与展示](#输出与展示)
- [决策报告与编排](#决策报告与编排)
- [数据分析（统计建模）](#数据分析统计建模)
- [表结构与列名咨询](#表结构与列名咨询)
- [运行配置与资源上限](#运行配置与资源上限)
- [错误处理](#错误处理)
- [离线自检](#离线自检)

## 环境要求与快速开始

### 环境要求

| 依赖 | 说明 |
| --- | --- |
| Python 3.10+ | 运行后端 |
| DeepSeek API | 项目根目录 `.env` 配置 `DEEPSEEK_API_KEY` |
| MongoDB | 默认 `localhost:27017`。会话记忆依赖它，缺失时记忆功能静默跳过 |
| Milvus + 外部 embedding API | 默认 `localhost:19530`。文档问答与知识库检索的硬依赖，任一不可用即报错 |

### 本地启动

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS / Linux：source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

本机若已有 conda 环境 `ChongqingAIApplication`（Python 3.13，依赖已装好），直接 `conda activate ChongqingAIApplication` 后启动即可，跳过 venv / pip 两步。

启动后可访问：

| 地址 | 用途 |
| --- | --- |
| `http://localhost:8000/docs` | 接口文档 |
| `http://localhost:8000/api/health` | 健康检查 |
| `http://localhost:8000/api/schema` | 表结构 |

### Docker 部署（摘要）

后端不单独在 `backend/` 目录启动，统一由项目根目录的 `docker-compose.yml` 编排：

- 多阶段构建，依赖按 `requirements.txt` 安装（含 `python-docx` / `pypdf` / `tavily-python` 三个可选依赖）；
- 以非 root 用户运行，数据目录 `/app/data` 挂命名卷 `chongqing-ai_backend_data`；
- 容器内通过服务名访问 `mongodb:27017` 与 `standalone:19530`。

完整步骤、运维、备份与排障见根目录 README 的「六、Docker 部署」一节。

### 配置来源

- 项目根目录的 `.env` 是唯一配置来源，本地开发与 docker compose 共用，由 `app/config.py` 经 pydantic-settings 读取。
- 容器内 `PROJECT_DIR` 解析为 `/`，`/.env` 不存在时 pydantic-settings 静默跳过，配置全部由 compose 的 `environment` 注入。

## 代码结构

### 目录树

```text
backend/
├── app/
│   ├── main.py                   # FastAPI 应用、CORS、lifespan、挂载 5 个 router
│   ├── config.py                 # Settings：读取根目录 .env，集中默认值与数据路径
│   ├── schemas.py                # Pydantic 请求 / 响应模型
│   ├── errors.py                 # AppError(message, error_code)
│   ├── agents/
│   │   ├── graph.py              # LangGraph 10 节点编排与 SSE 事件写出
│   │   ├── state.py              # OrchestratorState TypedDict
│   │   ├── tools.py              # 决策报告 agent 工具
│   │   ├── stat_tools.py         # 统计算子（pandas / numpy）
│   │   ├── analysis.py           # 分析 agent 提示词、结果模型与解析
│   │   └── report.py             # 决策报告提示词、结果模型与解析
│   ├── database/
│   │   ├── engine.py             # 多源引擎、schema 反射、关联识别、文档表定位
│   │   ├── mongo.py              # Motor 客户端（sessions / connections）
│   │   ├── milvus.py             # Milvus vector store 单例与超时
│   │   └── datasource_config.py  # 静态 + 运行时数据源注册表
│   ├── llm/
│   │   ├── client.py             # get_llm()：DeepSeek（OpenAI 兼容）
│   │   ├── embedding.py          # get_embeddings()：DashScope embedding
│   │   ├── intent.py             # 意图分类、规则兜底、schema 识别、输出格式
│   │   ├── text2sql.py           # 多方言流式 SQL 生成、只读校验、重试
│   │   ├── summary.py            # 旧对话滚动摘要
│   │   └── title.py              # 会话标题生成
│   ├── routers/
│   │   ├── meta.py               # /api/health、/api/schema
│   │   ├── chat.py               # /api/query、/api/query/stream、/api/chat/stream
│   │   ├── datasource.py         # 数据源列表 / 上传 / 删除
│   │   ├── connections.py        # 外部连接 CRUD 与测试
│   │   └── memory.py             # 历史会话
│   └── services/
│       ├── chart_service.py      # 严格图表模式
│       ├── query_service.py      # SQL 执行与结果截断
│       ├── chat_service.py       # 闲聊 / 检索 / schema 咨询的流式回答
│       ├── context_service.py    # 闲聊上下文治理
│       ├── datasource_service.py # Excel / CSV 导入
│       ├── connection_service.py # 外部连接持久化与测试
│       ├── document_service.py   # 文档解析、表格提取、向量化、检索
│       ├── chunking.py           # 结构感知文本分块
│       ├── knowledge_service.py  # 本地知识库索引与检索
│       ├── memory_service.py     # 会话记忆读写
│       └── schema_service.py     # schema 总览与确定性元数据回答
├── scripts/                      # 17 个 test_*.py + batch_answer.py
├── data/                         # uploads.db / docs.db（knowledge/ 为可选目录，当前不存在）
├── requirements.txt
└── Dockerfile
```

### 模块职责

#### app/agents

| 模块 | 职责 |
| --- | --- |
| `graph.py` | 10 节点编排、SSE 事件写出、progress 播报、`analysis` 失败回退 text2sql |
| `state.py` | `OrchestratorState` TypedDict，定义图内字段 |
| `tools.py` | 报告 agent 工具：`get_schema` / `generate_sql` / `execute_sql`，按配置追加联网与知识库工具 |
| `stat_tools.py` | 14 个统计 / 建模工具与请求级 `df_store`；`build_stat_tools` 统一登记 |
| `analysis.py` | 分析结果模型、`build_analysis_system_prompt`（内联 schema、要求并行规划）、JSON 解析 |
| `report.py` | 决策报告模型、系统提示、输入组装与解析 |

#### app/database

| 模块 | 职责 |
| --- | --- |
| `engine.py` | 多数据源引擎路由、schema 反射、关联识别、doc 表解析与块定位、`build_source_schema_text` |
| `mongo.py` | Motor 客户端与 `sessions` / `connections` 集合访问，失败静默降级 |
| `milvus.py` | vector store 单例、写入 / 检索超时处理 |
| `datasource_config.py` | 可选静态配置 + 运行时（MongoDB）注册表合并 |

#### app/llm

| 模块 | 职责 |
| --- | --- |
| `client.py` | `get_llm()`：DeepSeek，未配置 Key 抛 `AppError` |
| `embedding.py` | `get_embeddings()`：OpenAI 兼容的 embedding 端点 |
| `intent.py` | 6 类 `IntentResult`、规则兜底、schema 识别、doc 正文改判、`want_sql` / `answer_format` |
| `text2sql.py` | 多方言流式 SQL 生成、sqlglot 只读校验、失败重试 |
| `summary.py` | 旧对话滚动摘要，失败静默 |
| `title.py` | 会话标题生成，失败回退截断原文 |

#### app/routers

| 模块 | 职责 |
| --- | --- |
| `meta.py` | `GET /api/health`、`GET /api/schema` |
| `chat.py` | 三个查询接口、SSE 心跳、错误映射、会话落库 |
| `datasource.py` | 数据源列表、上传、删除（upload / doc / 外部连接按前缀分流） |
| `connections.py` | 外部连接列表、测试、新增、编辑、删除 |
| `memory.py` | 历史会话列表、详情、删除 |

#### app/services

| 模块 | 职责 |
| --- | --- |
| `chart_service.py` | 严格图表模式：仅显式 hint 时返回 bar / line / pie，否则 table |
| `query_service.py` | `execute_sql` / `run_query`，结果行数截断，异常转 `error_code` |
| `chat_service.py` | 闲聊、文档检索、schema 咨询三条流式回答，以及 `summarize_query_result` |
| `context_service.py` | 闲聊上下文：近期窗口截断 + 滚动摘要 |
| `datasource_service.py` | Excel / CSV 解析并导入 `uploads.db` |
| `connection_service.py` | 外部连接持久化、启动播种、连接测试与 schema inspect |
| `document_service.py` | 文档解析导入、`doc_meta` / `doc_blocks`、表格提取、分块向量化、检索与孤儿向量对账 |
| `chunking.py` | 结构感知文本分块（段落 / 句子 / overlap） |
| `knowledge_service.py` | 扫描知识库目录、文件清单缓存、Milvus 索引与检索 |
| `memory_service.py` | 会话记忆读写、最近查询结果、报告补写 |
| `schema_service.py` | schema 总览、列名 / 列序 / 行数确定性回答、doc 原始表头还原 |

#### 根模块与 scripts

| 模块 | 职责 |
| --- | --- |
| `main.py` | FastAPI 应用、CORS、lifespan、挂载 5 个 router（prefix `/api`）、`GET /` |
| `config.py` | pydantic-settings 读取项目根 `.env`，集中全部默认值与数据路径 |
| `schemas.py` | Pydantic v2 请求 / 响应模型（`QueryRequest` / `ChatRequest` / `QueryResponse` 等） |
| `errors.py` | `AppError`：`message` + `error_code` |
| `scripts/` | 17 个离线自检脚本 + `batch_answer.py` 批量答题 |
| `data/` | SQLite 数据文件与上传暂存；知识库目录为可选 |

## 接口契约

### 接口一览

#### 服务信息

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务信息（name / docs / health），由 `main.py` 直接提供，不属于任何 router |
| GET | `/api/health` | 健康检查 |
| GET | `/api/schema` | 数据源、表结构与关联关系（含 relationships） |

#### 数据源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/datasources` | 列出已上传、外部与文档数据源（每个文档只暴露一个文档级条目 `doc:<id>`） |
| POST | `/api/datasources/upload` | 上传 Excel / CSV，或文档 `.md` / `.txt` / `.pdf` / `.docx` |
| DELETE | `/api/datasources/{data_source_id}` | 删除数据源（upload / doc / 外部连接按 id 前缀分流） |

#### 外部连接

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/connections` | 列出已保存的外部数据库连接（不含密码） |
| POST | `/api/connections/test` | 测试连接（不保存），返回实时表结构或友好错误 |
| POST | `/api/connections` | 新增连接（先测通再保存到 MongoDB），返回新连接与表结构 |
| PUT | `/api/connections/{ds_id}` | 编辑已有连接（password 留空保留旧密码） |
| DELETE | `/api/connections/{ds_id}` | 删除连接 |

#### 会话

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sessions` | 历史会话（按更新时间降序） |
| GET | `/api/sessions/{session_id}` | 会话详情（含消息列表） |
| DELETE | `/api/sessions/{session_id}` | 永久删除会话及其全部消息 |

#### 查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/query` | 非流式查询（默认不返回 SQL，仅明确要求时返回） |
| POST | `/api/query/stream` | 流式查询（SSE：`result` / `error`；明确要求时含 `sql_token`） |
| POST | `/api/chat/stream` | 统一流式对话（SSE：`intent` / `answer_token` / `progress` / `result` / `report` / `error`；静默期下发 `: keepalive`） |

### 查询接口

`/api/query` 与 `/api/query/stream` 是旁路接口，前端主入口走 `/api/chat/stream`。三者共用同一份 schema 文本组装逻辑 `build_source_schema_text`：doc 源同样先做块定位收窄，再附取值区。

早前这两个旁路接口直接调用 `build_scoped_schema_text`，对 doc 源会把该文档全部几百张表的 DDL 原样喂给模型，故障形态与主链路一致：模型挑错表，或猜出不存在的过滤字面量，最终 0 行。

#### POST /api/query

```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "统计各分类的数量", "data_source_id": "upload:<表名>"}'
```

#### POST /api/query/stream

- 逐 token 推送 `sql_token`，仅当用户明确要求输出 SQL（`detect_want_sql` 为真）时才发；
- 完成后推送一条 `result`（`QueryResponse`）或 `error`；
- 数据源不可用时映射为 `DATASOURCE_UNAVAILABLE`，与 `/api/chat/stream` 同口径。

#### POST /api/chat/stream

前端主入口。先做意图分类（`intent` 事件），再分流到数据查询或闲聊回答；数据查询成功后，末尾附 `report` 决策报告事件。

```bash
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "统计各分类的数量", "data_source_id": "upload:<表名>", "history": []}'
```

意图有三层口径，不要混用：

| 层 | 取值 | 说明 |
| --- | --- | --- |
| 内部 `IntentResult` | `query` / `chat` / `redraw` / `retrieval` / `analysis` / `schema` | `app/llm/intent.py` 的完整意图，决定图分支 |
| LLM 直接判定 | `query` / `chat` / `redraw` / `analysis` | `schema` 由 `detect_schema_question` 规则锁定，`retrieval` 由 doc 正文规则改判 |
| SSE `intent` 事件 | `query` / `chat` / `retrieval` / `schema` / `analysis` | 仅供前端显示等待文案；`redraw` 并入 `query` |

`intent` 事件透传真实意图，供前端显示「正在生成 SQL…」「正在思考…」「正在检索文档…」「正在读取表结构…」「正在做数据分析…」。它只是等待文案的标签，渲染家族仍由 `result` 事件的 `QueryResponse.kind` 决定（只发 `query` / `chat`），两者互不影响。

`analysis` 必须原样透传：该分支一次 SQL 都不生成（走 pandas 统计算子），压成 `query` 会让整段分析全程显示「正在生成 SQL…」。

`progress` 事件（`{"type": "progress", "scope": "analysis" | "report", "text": "正在调用 group_cagr…"}`）由分析 / 决策报告 agent 的每一轮工具调用产出。`scope` 决定前端显示位置：

- `analysis` 显示在 loading 块内（此时 `loading` 仍为真）；
- `report` 单独开位（`result` 事件已把 `loading` 置 false，报告 agent 仍在跑）。

最多播报 `_MAX_PROGRESS`（40）条，超限不再发。

`result` 事件之后若 `report_enabled` 为真，报告 agent 会串行产出 `report` 事件，前端经 `ReportCard` 卡片展示标题 / 核心结论 / 关键发现 / 建议 / 置信度。

### SSE 事件与保活

| 事件 | 载荷 | 含义 |
| --- | --- | --- |
| `intent` | `intent`、`chart_hint` | 意图分类结果（等待文案） |
| `sql_token` | `content` | SQL 片段，仅用户明确要求输出 SQL 时发 |
| `answer_token` | `content` | 闲聊 / 检索回答的流式片段 |
| `progress` | `scope`、`text` | agent 工具调用进度 |
| `result` | `QueryResponse` | 最终结构化结果 |
| `report` | 结构化报告 | 决策报告（可选） |
| `error` | `message`、`error_code` | 友好错误 |

保活：图内节点可能连续静默数分钟。`want_sql` 为假时 text2sql 的 token 不发，schema 反射 → SQL 流式生成 → 执行 → `summarize_query_result` 全程零事件，实测曾出现约 205 秒静默；其后的分析 / 报告 agent 更久。

后端静默超过 `SSE_HEARTBEAT_SECONDS`（默认 10 秒）即下发一行 SSE 注释（`: keepalive`）。注释行同样让 `reader.read()` 返回并重置前端的空闲计时器，而前端 `consumeSSE` 与 `batch_answer.py` 都会跳过它，两条既有消费端无需改动。

不设总时长上限：答案质量优先，允许跑到自然结束，用户可随时点「停止」（前端 `AbortController`）。生成器被关闭时会 `task.cancel()` 掉图任务，不留继续烧 LLM 的孤儿任务。

> ⚠️ 改动 `app/agents/graph.py` 的 `_route` 时注意：它的返回值是 `_CLASSIFY_BRANCHES` 的键名（意图名），不是节点名。LangGraph 在 `_branch._finish` 里用它查表，查不到就抛 `KeyError`。`scripts/test_intent_routing.py` 第 11 段为此加了分支闭合性断言。

## 数据源

### 上传数据源（upload:）

- 标识：`upload:<表名>`。
- 来源：Excel / CSV 经 `POST /api/datasources/upload` 导入 `backend/data/uploads.db`。
- 上传文件名由客户端指定。`_clean_identifier` 只替换引号、分号与控制字符，保留中文 / 空格 / 数字开头；SQL 侧一律经 `identifier_preparer.quote` 按方言加引号。

### 文档数据源（doc:）

- 标识：`doc:<id>`。
- 解析：`.md` / `.txt` / `.pdf` / `.docx`。表格入 `backend/data/docs.db`，文本结构感知分块后向量化入 Milvus。
- 每个文档只暴露一个文档级条目 `doc:<id>`（`document_service.list_documents`）。选中后由程序自动解析出该文档提取出的全部表格，单张提取表的条目不会出现在前端下拉里。
- 块 id 寻址：多块文档（如被 `=== <块id> ===` 切成数百张小表的赛题 `Table_Query-数据.md`）支持 `doc:<doc_id>:<块id>`。`app/database/engine.py` 的 `_resolve_doc_table` 把第三段经 `get_doc_table_by_block` 映射到真实表，`resolve_table_names` 返回单表。该寻址只在题干显式带块 id 时走，不是下拉项。
- 块 id 合法性走 `get_doc_tables(doc_id)` + `get_doc_table_by_block(doc_id, segment)` 的白名单，只认本文档提取出的表。`doc_meta` / `doc_blocks` 等内部表不在白名单内，无法寻址。
- 原始列名：入库时保留原始名（中文 / 空格 / 数字开头均保留）。`doc_blocks.columns` 另外持久化清洗前的原始表头，供 schema 咨询与 RAG 分块按原始名作答；旧 `data/docs.db` 里已清洗的表同样经此还原。
- 索引限制：Milvus 里表格块只索引「列名 + 前 5 行样本」，而 `docs.db` 存的是全量数据。故 `graph._chunk_header` 会给片段抬头加「【表格摘录：以下仅为该表的部分样本行，全表共 N 行，非全部数据】」，`chat_service._RETRIEVAL_SYSTEM_PROMPT` 配套要求不得据此回答计数 / 合计 / 平均值 / 占比。

不交代这层限制的话，模型会拿 5 行样本报出一个形态完好的假统计量。需要全量计算的问句应走 SQL 或统计算子。

### 外部连接（静态配置与运行时连接）

- 静态配置：可选文件 `backend/datasources.json`，用于配置 MySQL / PostgreSQL。仓库未提供该文件；`app/database/datasource_config.py` 在文件缺失或损坏时视为空列表，不影响启动。
- 运行时连接（推荐）：前端「连接数据库」手动输入主机 IP / 端口 / 库名 / 用户名 / 密码，保存到 MongoDB `connections` 集合并跨会话复用。连接成功即返回实时表结构，提问时后端先 `inspect` 表字段再生成 SQL。接口见 `/api/connections`。
- 连接列表读取失败返回 `None`（区别于「读到了但为空」）。此时保持现有注册表不动，避免 Mongo 抖动把已保存连接全部变成「未知数据源」。

### 文档表格定位与取值区

schema 文本的唯一入口是 `engine.build_source_schema_text`，三条查询链路共用：

- `graph._prepare_query_node`（经 `asyncio.to_thread`）；
- `routers/chat.py` 的 `/query/stream`（经 `asyncio.to_thread`）；
- `query_service.run_query`（同步 `def`，跑在 Starlette 线程池里直接调）。

新增查询入口时一律走它，不要直接调 `build_scoped_schema_text`。唯一例外是 `graph._chat_node`：闲聊不生成 SQL，用最便宜的 `build_scoped_schema_text` 即可。

#### 多表收窄

文档源下 `doc:<id>` 会解析出该文档提取出的全部表格（赛题 `Table_Query` 500 张、`Multi-step` 48 张），直接交给模型等于让它盲选。定位按两级收窄：

1. 题干块 id（`engine.extract_block_id`，精确）→ 直接映射到该块的表；
2. 否则用向量检索召回 top-k 块，把其中的表格块映射回表名。

约束：

- 召回走 `document_service.retrieve_doc_scored`，按 L2 距离阈值过滤（`doc_table_max_distance`）。最优命中都超阈值就判「没定位到」，只保留不差于「最优 + `doc_table_score_margin`」的命中。
- 命中块映射到真实表名后按内容去重。同一张表常被复制成多个块（赛题咖啡表 4 份、能源表 5 份，内容逐字相同）；不去重会把同构表的多个副本一起交给模型，它分别聚合再相加，数值直接翻倍且形态完全正常。
- 只认本数据源的表（`table_names` 白名单），防跨文档读数。
- 定位不到就回退全量 schema，并打 `logger.info`。收窄方向是会反转的风险：不定位的最坏情况是「模型可能选错」，定位错则确定错，故宁可回退不可猜测。

> ⚠️ 阈值是上界：Milvus metric 是 L2 距离，越小越相似。默认 `0.95` / `0.10` 由 `text-embedding-v4` 在赛题 `Multi-step` 数据上实测标定（真命中 0.87，语义无关 0.98~1.03，完全无关 1.43）。换 `EMBEDDING_MODEL` 必须重新标定。

实现位置：`locate_doc_tables` 住在 `engine.py` 而非 `graph.py`。原因是 `graph.py` 反向依赖 `query_service.execute_sql`，而 `query_service` 再 import `graph` 会成环；旁路接口又必须复用这套定位逻辑，故下沉到 `engine.py`。`graph._locate_doc_tables` 只是 `asyncio.to_thread` 薄壳，供 `_schema_node` 使用。

`locate_doc_tables` 是同步阻塞函数（embedding HTTP + Milvus），在 async 上下文中调用方必须自行 `await asyncio.to_thread(...)`。

#### 取值区

定位对了表，不等于答得出题。文档抽出的表大多是「行标签 + 数值」的透视表，语义全在某一列的取值里（如 `col = 'less : amount representing interest'` → `73512`）。若 schema 文本只有 DDL、零取值，模型只能照中文问题猜一个不存在的值，必然 0 行，且形态完全正常。

因此 `build_schema_text(..., with_samples=True)` 会在每张表的 `CREATE TABLE` 之后追加以 `--` 注释行书写的取值区：

```text
-- 表 doc_a85671194933_10 共 9 行，全部数据：
-- col | "amount ( in thousands )"
-- 2009 | $ 32452
...
-- less : amount representing interest | 73512     ← 问题要的就是这一行
```

规则：

- 行数 ≤ `SCHEMA_FULL_DUMP_ROWS`（默认 50）→ 给全部行；
- 否则 → 前 `SCHEMA_SAMPLE_ROWS`（默认 20）行，另加文本列的完整去重取值（限 `SCHEMA_MAX_DISTINCT_VALUES`，默认 30 个）。去重值超上限就整列不给 —— 截断的清单会让模型以为「就这些」，比不给更糟；
- 总预算 `SCHEMA_MAX_CHARS`（默认 60000），用尽即停止追加，并在末尾注明「另有 N 张表的取值区因超出字符预算已省略」；
- 单表读值失败只 `logger.warning(..., exc_info=True)` 跳过该表，不毁掉整份 schema；
- 表数超过 `SCHEMA_MAX_TABLES`（默认 5）就整份不附取值。doc 源回退全量（239 张表）正落在这一支：实测全带样本会多出约 7.9 万字符（DDL 本身才 3.9 万），prompt 直接翻三倍；
- `with_samples=False` 时与改造前逐字节一致（纯 DDL，取值区是纯追加）。

text2sql 的 system prompt 配套要求：WHERE / HAVING 的取值必须来自 schema 里给出的真实值，原样照抄，不得杜撰；取值不足时先写不带该条件的查询（必要时 `SELECT *` / `SELECT DISTINCT`）而不是猜。

### 问答分流：正文 RAG vs 表格 SQL

文档数据源下，指向文档正文的提问一律走 RAG（Milvus 检索 + 流式回答 + 出处引用）；指向表格取值的提问走 SQL 查从文档抽出的表。

- 判定入口是 `app/llm/intent.py` 的 `_route_doc_intent`，只作用于 `query` / `chat`（`schema` 已在上一步锁定，`analysis` 不参与）。
- 聚合词不得否决检索意图：「报告里提到的退款金额是多少」问的是正文，不能因为一个「多少」就回落 SQL 去查表格。聚合词只描述答案的形态，不指示答案在哪里。
- 反向边界：不含检索词问句（如「ANON-…-0001 这张卡 2024-03-01 的消费金额是多少」）仍走 SQL。Milvus 里表格块只索引「列名 + 前 5 行样本」，纯 RAG 找不到具体行，按行查找必须由 SQL 覆盖全量数据。
- 改判是就地修改 `intent`，`chart_hint` / `want_sql` / `answer_format` 原样保留。

词表分三类，判序即优先级：表格内部定位信号一票否决 → 文档名词 → 正文指向短语。

| 词表 | 收什么 | 不收什么 |
| --- | --- | --- |
| `_TABLE_SCAN_WORDS` | 单元格 / 表格中 / 表中 / 列名 / 哪一列 / 第几行 / 这张卡 | —— |
| `_DOC_NOUN_WORDS` | `报告里` / `报告中` / `这份报告` / 文档 / 资料 | 裸「报告」：财报语境里它是动词（「被报告为当前负债」） |
| `_DOC_PHRASE_WORDS` | 明确的正文指向短语（如「文档中提到」） | 裸动词：`查找` / `描述` / `介绍` |

> ⚠️ 三类词表都不得收裸「内容」。它太泛，实测 500 道赛题里 287 道（57.4%）都含「内容」，而那指的是单元格取值。收进来会把过半的按单元格查找题赶去 RAG，必然答不出。

回归断言见 `scripts/test_intent_routing.py` 第 8~10 段；第 12 段是赛题全量回归（三份题目集共 787 题，断言改判 retrieval 的题数为 0；题目集在仓库外，缺失时跳过并打印，可用 `COMPETITION_DATA_DIR` 指定目录）。

## 会话记忆与上下文治理

### 会话记忆（MongoDB）

- 单次对话以 `session_id` 区分，记录「用户问题 + 助手回答 + 最后使用的数据源」到本地 MongoDB（`localhost:27017`，库 `chongqing_ai`，集合 `sessions`）。
- `session_id` 由前端生成并存 `localStorage`（key `chongqing_ai_session_id`）。刷新页面自动恢复当前会话，历史会话侧栏可切换，「新对话」按钮新建。
- 记忆读写失败静默降级，不影响主查询链路；每轮结果最多持久化 `MEMORY_MAX_ROWS`（200）行。
- 未携带 `session_id` 或本轮结果为错误时不写入，路由层会打 `logger.info`，以便与「Mongo 写入失败」区分。

### 长对话上下文治理

闲聊分支的多轮上下文会随会话变长而 token 膨胀，采用「近期窗口截断 + 旧对话滚动摘要」双层治理：

- 近期窗口：只保留最近 `CHAT_HISTORY_MAX_MESSAGES`（默认 20）条历史进 prompt，另有 `CHAT_HISTORY_MAX_TOKENS`（默认 2400）的 token 预算做二次裁剪。
- 滚动摘要：窗口之前的旧对话增量折叠成一段 ≤ `CHAT_SUMMARY_MAX_CHARS`（默认 300）字的摘要，存于会话文档的 `summary` / `summary_upto` 字段；喂给 LLM 时以「摘要 + 近期窗口」替代全量历史。
- 摘要折叠边界以服务端持久化的消息数为准；摘要生成失败返回 `None` 时不推进 `summary_upto`；`save_summary_state` 用 `expected_upto` 做条件更新（CAS）。
- 全程静默降级：MongoDB / LLM 不可用时回退为纯截断，不产生 error 事件。

意图分类本身已截取最近 6 条历史；text2sql / 决策报告 / 重绘链路本就不携带历史，不受此治理影响。

## 输出与展示

### SQL 展示策略（默认隐藏）

数据查询默认直接回答结果、不返回 SQL 语句；仅当用户明确要求输出 SQL（如「输出 SQL」「给我 SQL 语句」「查询语句」）时才经 `sql_token` / `result.sql` 返回。

识别用 LLM（意图分类的 `want_sql` 字段）+ 关键词规则兜底（`app/llm/intent.py` 的 `detect_want_sql`），三个查询接口统一生效。SQL 在图内部（`state.sql`）仍保留，供决策报告 agent 使用。

### 图表模式（严格）

仅当用户显式指定柱状图 / 折线图 / 饼图时才返回对应 `chart_type`（bar / line / pie），否则一律 `table`，不做自动推断出图。仅说「可视化 / 画图」而不指定类型，同样返回 `table`。

### 输出格式（列表 / JSON / 单答案）

当用户明确要求以列表 / JSON 形式返回答案（如「返回列表」「只返回名称」「以 JSON 返回」），或问题隐含「哪些 / 哪类 / 哪几种…」时，后端把 `QueryResponse.explanation` 渲染为纯列表字符串（如 `['Latte', 'Americano with Milk']`，无匹配为 `[]`；JSON 格式为 `["Latte", ...]`），同时保留明细表格。

若问题是单答案（「最高 / 最低 / 最多 / 唯一 / 哪个…」），`detect_answer_format` 判定为 `single`，后端只把单个名称写入 `explanation`（不带括号引号、不追加分析说明）。

- 识别：纯关键词规则 `app/llm/intent.py` 的 `detect_answer_format`，结果写入 `state.answer_format`；
- 落点：`analysis` 分支从 `AnalysisResult.items[].name` 取名称，`query` 分支取结果首列，经 `render_list_answer` 渲染；
- 此外 `QueryResponse.analysis` 附带分析说明（query 分支用 `summarize_query_result`，analysis 分支用 `AnalysisResult.explanation`，均静默降级），前端分「回答」与「分析说明」两个区块展示；
- 其余情况仍输出自然语言分析。

## 决策报告与编排

### 编排图与节点

后端用 LangGraph `StateGraph` 编排（`app/agents/graph.py`），共 10 个节点：

| 节点 | 作用 |
| --- | --- |
| `classify` | 意图分类，写 `intent` / `chart_hint` / `want_sql` / `answer_format` |
| `prepare_query` | 解析数据源，生成 schema 文本（query 与 analysis 的共同前置） |
| `text2sql` | 流式生成 SQL，按需发 `sql_token` |
| `execute` | 执行 SQL，渲染结果与回答 |
| `report` | 决策报告 agent |
| `redraw` | 复用本会话最近一次结果出图，跳过 SQL |
| `chat` | 流式闲聊 |
| `retrieve` | 文档检索 + 流式回答 |
| `schema` | 表结构 / 列名 / 字段咨询 |
| `analysis` | 统计建模 agent |

拓扑要点：

- `START → classify`，之后按 `_CLASSIFY_BRANCHES` 分流；`query` 与 `analysis` 都先进 `prepare_query`，再按 `_route_after_prepare` 分叉到 `text2sql` 或 `analysis`；
- `text2sql → execute → report → END`（`report` 是否产出内容由 `REPORT_ENABLED` 决定）；
- 其余分支直接 `END`；
- `_CLASSIFY_BRANCHES` 的键是意图名、值是节点名，`_route` 必须返回键。改这两个函数后务必跑 `scripts/test_intent_routing.py` 第 11 段。

### 报告工具与可选增强

报告 agent 由 `create_agent` 驱动，对查询结果做多步下钻推理，产出结构化 `report` 事件（标题 / 核心结论 / 关键发现 / 建议 / 置信度）。

基础工具（本地）：

- `get_schema`：读取当前数据源表结构；
- `generate_sql`：生成 SQL；
- `execute_sql`：执行前过 `validate_sql` 只读校验。

可选工具（视配置追加）：

- `web_search`：联网搜索（DuckDuckGo / Tavily），失败静默降级；
- `knowledge_search`：本地知识库检索。

DeepSeek「思考模式」不支持 `response_format` / 强制 `tool_choice`，报告结构化输出用人工 JSON 解析（参考 `app/agents/report.py`）。

### 数据外发与配置卫生

- 联网搜索默认关闭（`WEB_SEARCH_PROVIDER=none`）。一旦设为 `duckduckgo` / `tavily`，决策报告 agent 在推理过程中会把用户提问按原样发往第三方搜索服务。这是数据外发行为，请自行评估是否可接受。
- `knowledge_search` 默认读取 `backend/data/knowledge`（`settings.knowledge_base_dir`）。目录不存在时视为空知识库、不报错；支持 `.md` / `.txt`。语料不外发，但会写入 / 重建本地 Milvus `knowledge_chunks` 索引。
- 用户提问本身会发往 DeepSeek API（`DEEPSEEK_BASE_URL`），embedding 会发往 `EMBEDDING_BASE_URL`，均为链路必需。
- 若密钥曾经外泄（截图、日志、共享终端等），到 DeepSeek / 阿里云控制台轮换密钥后再更新 `.env`。

## 数据分析（统计建模）

### 算子清单

对「相关性（正相关 / 负相关）、环比 / 同比、年均增长率（CAGR）、趋势 / 时序、预测、异常、聚类」等 SQL 难以单条表达的复杂统计问题，意图分类会命中 `analysis` 分支，改用 `create_agent` + 预定义统计算子（`app/agents/stat_tools.py`）在 pandas / numpy 侧计算，而非生成单条 SQL。

所有算子均为只读：只读库、只对内存 DataFrame 聚合，`df_store` 请求级隔离、不暴露任意 SQL。

| 算子 | 作用 |
| --- | --- |
| `get_schema` | 返回当前数据源表结构 |
| `load_table` | 读表到内存；`filters` 可一次给多个条件（AND），`filter_op` 支持 eq / ne / gt / ge / lt / le / contains / not_contains |
| `group_aggregate` | 分组聚合（count / sum / avg / min / max，可按时间降采样） |
| `group_correlate` | 按组求两列的皮尔逊相关系数 |
| `group_mom_growth` | 按组取最近 n 期，判断是否连续环比增长（严格逐期递增） |
| `group_mom_growth_rate` | 按组算环比增长率并排名（`rank_by`：`last` 末期环比 / `mean` 各期均值） |
| `group_cagr` | 按组算区间内的年均增长率：`CAGR = (末值/首值)^(1/期数) - 1`；`start` / `end` 给区间、`date_freq="year"`，`method` 可选 `cagr` / `aagr`；返回 `growth_rate` / `growth_rate_pct` / `periods_count` / `start_value` / `end_value` / `ranking` / `rank` |
| `group_share` | 按组求占比 + 是否高于平均占比（1/组数） |
| `group_vs_overall_avg` | 按组求指标并与全体组平均值比较（比的是水平值，不是占比） |
| `intersect_groups` | 把若干「每组一行」的结果按分组列合并，求同时满足全部条件的组 |
| `linear_regression` | 一元线性回归（y ~ x），返回斜率 / 截距 / R² / r |
| `forecast_time_series` | 时序外推预测（linear / average / growth_rate，可选按组） |
| `detect_anomalies` | 异常检测（z-score / IQR） |
| `cluster_kmeans` | K-Means 聚类（KMeans++ 初始化，写回簇标签） |

`group_cagr` 与 `group_mom_growth_rate` 的分工是「年化 vs 环比」：问「2015-2020 年间年均增长率」必须用 `group_cagr`，拿环比算会把 5 年的总增速当成一年的。

首值 / 末值非正或期数 < 2 时，该组 `growth_rate` 为 `None` + `note`，绝不返回 `inf`。

关键词兜底表 `app/llm/intent.py` 的 `_ANALYSIS_WORDS`（仅当 LLM 分类不可用时生效）收录「年均增长率 / 复合增长率 / 年平均增长率 / 平均年增长率 / CAGR」完整短语，不收裸「增长率」，否则一般性提及也会被拉进 `analysis`。

### 使用约定

- `load_table` 的 `filters` 一次给多条件：形如 `[{"col":"category","op":"eq","value":"Historical"}, {"col":"powertrain","op":"eq","value":"BEV"}]`。宽表定位目标数据要同时固定四五列，一列一轮就是四五轮工具调用。返回 `filters_applied` 逐条回传筛完剩余行数，筛空时一眼看出是哪个条件筛没的。
- `group_*` 返回 `df_id` 与「每组一行」的指标表，供 `intersect_groups` 做确定性求交。多步检索题的最后一跳是集合求交，让模型自己在几十个组之间心算很容易错。
- 同名指标列的后缀规则：两个表合并时首个保留原名，后者加 `_<df_id>` 后缀，并在返回值里回传 `columns` 让模型看到真实列名。不回传就会照旧名去筛，条件静默失效。
- `intersect_groups` 支持列对列比较：条件的 `value` 可写成 `{"col": "另一列", "factor": 0.5}`（可加 `offset`），表示「本列 > 另一列 × 0.5」。「预测期增长率高于历史均值」「增量超过历史总增量 50%」这类同一主体两段区间互比只能这样表达。
- `group_cagr` 的指标表额外带 `start_value` / `end_value`：「末年保有量超过 X」可直接用 `{"col": "end_value", "op": ">", "value": 100000}` 求交，不必另跑算子。
- ⚠️ 新增算子必须同步登记在 `build_stat_tools` 末尾的返回列表里。`@tool` 只是包装，不登记模型连「有这个工具」都看不到。

### 已知陷阱与回退

- 日期列一律经 `_to_datetime` 解析，不要直接 `pd.to_datetime`：整数列会被按 epoch 纳秒解释（年份 `[2015, 2016]` → 1970 年），所有年份塌缩成同一个期，按年聚合 / 环比 / 年均增长率只会报「期数不足（<2）」，但返回结构完全正常，故障形态与「数据本来就不够」一模一样。`_to_datetime` 对整值且落在 `[1000, 9999]` 的数值列显式按 `%Y` 解析。
- `_infer_types` 在 pandas 3.x 上对文本列是 no-op（pandas 3 文本列已是 `StringDtype`），别指望它把 `"2015"` 转成数字。
- 轮次（= 耗时）靠三处压下来：① `load_table` 支持多条件 AND；② `group_cagr` 让年均增长率不必用环比凑；③ `build_analysis_system_prompt` 把 `schema_text` 直接注入 system 并声明「无需再调 `get_schema`」，要求一次性规划、同一轮发起全部独立工具调用（`ToolNode` 会并行执行同一轮的多个 `tool_calls`）。
- `analysis` 是增强路径：任何失败（含 `GraphRecursionError`）都回退到既有 text2sql 链路（`_analysis_node` 的 `except`），不伪造结果、不阻断主链路。
- `AGENT_RECURSION_LIMIT`（默认 40）是跑飞兜底，不是性能上限；langgraph 1.1.2 的默认值是 10000（等于不设限）。
- 算子数值正确性由 `scripts/test_analysis.py` 离线自检。

## 表结构与列名咨询

「第 1 列的列名是什么？」「哪一列被称为谓词？」「名为 X 的列是第几列？」「这个表有哪些字段？」「有几列 / 多少行？」这类元数据咨询命中 `schema` 意图分支。

### 识别规则

- `app/llm/intent.py` 的 `detect_schema_question` 纯规则识别：第 N 列 / 哪一列被称为 X / 名为 X 的列 / 列名 / 字段 / 表结构 / 列数 / 行数。
- 在 `classify_intent` 里优先于 chat / query 与 doc 的 retrieval 分流，锁定为 `schema`。
- 规则已收紧「第 N 列」问句后缀，优先「哪一列…」疑问句，避免把赛题前置语境「第一行和第一列标记数据编码的起点」里的「第一列」误判为第 N 列。

### 主路径：模型驱动的 schema 总览

- `app/services/schema_service.py` 的 `build_schema_overview` 生成按列序编号、含样本行的表结构总览（多表有界截断）。
- 交 `app/services/chat_service.py` 的 `schema_consult_stream`（低温、反编造）流式回答，让模型自行把中文列名（谓词→predicate、主题→subject、日期→date）映射到真实英文字段并给出列序。
- 总览只含真实列名，杜绝凭空编造。

### 兜底：确定性反射

LLM 不可用时回退到 `app/services/schema_service.py` 的 `answer_schema_question` / `answer_schema_question_multi`：

- 用 `inspect(engine).get_columns(table)` 读列序、`SELECT COUNT(*)` 读行数；
- 直接拼中文答案（如「第 1 列的列名是 date。」）；
- `NO_API_KEY` 等 LLM 失败时生效。

### 原始列名保留

| 场景 | 行为 | 代码落点 |
| --- | --- | --- |
| 文档表入库 | 保留原始名（中文 / 空格 / 数字开头），仅替换引号、分号与控制字符 | `document_service.import_document` |
| 原始表头持久化 | 清洗前的表头存入 `doc_blocks.columns`（JSON） | `document_service.import_document` |
| schema 咨询 | 对 `doc_` 表返回原始名，使「哪一列的名称为 0」「第 1 列的列名」命中真实表头 | `schema_service._columns` |
| RAG 分块 | 分块文本使用原始名 | `document_service` |
| 旧库还原 | 已被清洗的表（数字表头 `0/1` → `t_0/t_1`）经 `doc_blocks.columns` 还原 | `schema_service` |

text2sql 的 schema 文本用 `identifier_preparer.quote` 按方言加引号包裹真实列名，生成 SQL 必须原样照抄。

### 块级定位

文档数据源只暴露文档级条目 `doc:<doc_id>`，选中后由程序自动读取该文档提取出的全部表格。块 id 寻址的格式、映射与合法性白名单见「数据源 → 文档数据源」。

`schema` 节点在此之上先定位块、再作答，按三级收窄：

1. 块级数据源（单表）；
2. 题干块 id（`engine.extract_block_id`）；
3. RAG 语义命中（仅 `column_by_name` 问句）。

位置问句（「第 N 列」）在整篇文档且无块 id 时保留全局总览兜底。此类题干信息不足导致的本质歧义需靠块 id 寻址解决，不能收窄。

### 自检

```bash
cd backend && python scripts/test_schema_qa.py
```

无 LLM 依赖，覆盖命中 / 误判防护与内存 SQLite 上的确定性断言。

## 运行配置与资源上限

以下均可在项目根目录 `.env` 用同名大写键覆盖（默认值见 `app/config.py`）。

### 超时与重试

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_TIMEOUT_SECONDS` | 60 | 单次大模型调用超时；SDK 默认 600s × 2 次重试 |
| `LLM_MAX_RETRIES` | 1 | 大模型调用重试次数 |
| `MILVUS_TIMEOUT_SECONDS` | 30 | Milvus 检索 / 写入超时 |
| `EMBEDDING_TIMEOUT_SECONDS` | 30 | embedding 接口超时 |
| `SSE_HEARTBEAT_SECONDS` | 10 | 图静默超过该秒数即下发 `: keepalive` |
| `AGENT_RECURSION_LIMIT` | 40 | 分析 / 报告 agent 的步数上限（跑飞兜底，非时间预算） |

后端没有「一次提问」的整体超时。上表都是组件级超时或步数上限，一条链路可能串起多次调用，慢查询会一直跑到出结果为止（答案质量优先）。

前端的 `SSE_IDLE_TIMEOUT_MS`（600s，两次 SSE 事件之间的最大间隔）由后端心跳兜住，退化为「后端进程整体失联」的判据；用户可随时点「停止」。

### 资源上限

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | 200 | 单个上传文件大小上限（Excel / CSV / 文档共用） |
| `MAX_QUESTION_CHARS` | 2000 | 单条提问长度上限 |
| `MAX_HISTORY_MESSAGES` | 200 | 请求历史保留的最近条数 |
| `MAX_MESSAGE_CHARS` | 20000 | 单条历史消息长度上限 |
| `MAX_RESULT_ROWS` | 5000 | 查询结果行数上限，超出截断并标注 `truncated` |
| `MEMORY_MAX_ROWS` | 200 | 会话记忆里每轮结果持久化的行数上限 |

这些是资源保护，不是权限限制，不改变「任何能访问该端口的用户均可修改数据源」的现状。

### 取值区预算

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `SCHEMA_FULL_DUMP_ROWS` | 50 | 行数不超过它的表给全部行 |
| `SCHEMA_SAMPLE_ROWS` | 20 | 超过上一条的表只给前 N 行样本 |
| `SCHEMA_MAX_DISTINCT_VALUES` | 30 | 文本列去重取值上限；更多的列整列不给 |
| `SCHEMA_MAX_CHARS` | 60000 | 取值区总字符预算，用尽即停止并注明省略张数 |
| `SCHEMA_MAX_TABLES` | 5 | 表数超过它就不附取值区 |

语义与取舍见「数据源 → 文档表格定位与取值区」。

### 决策报告开关

`REPORT_ENABLED`（默认 `true`）。报告链路在同一次 SSE 响应内串行于主结果之后，且报告 agent 会再跑一轮 SQL 生成 / 执行，单次查询因此多付出若干次 LLM 往返。压延迟或成本时可设为 `false`（前端 `report` 事件的处理保持不变，只是不再收到）。

### 启动期与关停

- `lifespan` 内先经 `asyncio.to_thread(ensure_docs_schema)` 初始化 docs.db 基础表（`doc_meta` / `doc_blocks`，幂等）。此前只有导入文档时才建表，「docs.db 文件已存在但里面没有表」的环境会让对账以 `no such table: doc_meta` 抛出。
- 再做一次孤儿文档向量对账（`document_service.reconcile_orphan_vectors`）：清理「`doc_meta` 记录已删、Milvus 向量没删掉」的残留。连不上 Milvus 时内部跳过，失败只记日志。
- 每次启动经 `seed_runtime_datasources` 从 MongoDB 播种已保存的外部连接。
- 关停走 `try/finally`：dispose 全部缓存引擎、关闭 Mongo 客户端、释放 vector store。
- 本地知识库语料以文件级清单（相对路径 + `mtime_ns` + 大小）为缓存失效键；清单检查、缓存查找与 Milvus 重建在同一把锁下完成，重建先于缓存发布。

### 日志

关键降级路径统一走 `logger.warning(..., exc_info=True)`，不使用静默 `except: pass`：查询结果分析说明失败、数据源解析失败、schema 确定性兜底中的单表失败、文档删除后 Milvus 清理失败、连接列表读取失败、摘要生成失败等。SQL 执行失败会记录完整语句与栈。

## 错误处理

### 错误码

模型不可用时不设规则兜底，而是返回带 `error_code` 的友好提示：

| error_code | 含义 |
| --- | --- |
| `NO_API_KEY` | 未配置 `DEEPSEEK_API_KEY` |
| `LLM_UNAVAILABLE` | 大模型服务不可用（初始化失败 / 网络 / 超时） |
| `SQL_ERROR` | 生成的 SQL 无法执行或多次校验未通过 |
| `DATASOURCE_UNAVAILABLE` | 数据源不可用：外部连接失败，或请求里的 `data_source_id` 已失效 / 未知 |
| `DATASOURCE_CONNECT_FAILED` | 新增 / 编辑连接时测试连接失败 |
| `NO_PRIOR_RESULT` | 重绘图表前当前会话尚无可用查询结果 |
| `SYSTEM_ERROR` | 其它未预期异常 |

前端「修复连接」入口只认 `DATASOURCE_UNAVAILABLE` 与 `DATASOURCE_CONNECT_FAILED`。路由层把 `ValueError`（未知 / 失效数据源）与 `SQLAlchemyError` 都映射为前者，映射成 `SYSTEM_ERROR` 会让补救入口失灵。

### 降级策略

- 查询链路不设规则兜底：模型不可用或多次校验失败时返回友好错误，不伪造结果。
- 意图分类例外：`intent.py` 带关键词规则兜底，LLM 不可用时仍可分流。
- `analysis` 回退 text2sql：分析节点任何失败都不阻断主链路。
- 可选依赖缺失静默降级：`python-docx` / `pypdf` / `tavily-python` 缺失时，.docx / .pdf 报友好 `AppError`。向量检索是硬依赖，未配置 embedding 或 Milvus 不可用时检索直接报错。
- 会话记忆静默降级：MongoDB 读写失败不抛 500、不产出 error 事件。
- 报告节点静默跳过：报告失败不影响主结果。

## 离线自检

### 运行方式与环境前提

`scripts/test_*.py` 均为独立脚本：无 pytest 依赖，直接 `python scripts/xxx.py` 运行，退出码非 0 表示失败。

除两处环境前提外，都不依赖 LLM / MongoDB：

- `test_analysis.py` 需 `data/docs.db` 存在；
- `test_milvus_timeout_kwarg.py` 用替身客户端、不写入任何真实集合，但构造 store 时 `langchain_milvus` 内部会走 pymilvus ORM 的 `connections.connect()`（替身覆盖不到），故需本机 Milvus 可达。Milvus 未启动时它会以 `Fail connecting to server on localhost:19530` 失败，属环境问题而非回归。

### 脚本清单

| 脚本 | 覆盖 |
| --- | --- |
| `test_sql_guard.py` | SQL 只读守门：DML CTE 被拒、`UNION` / `INTERSECT` / `EXCEPT` 放行、多语句被拒 |
| `test_identifier_quote.py` | `_clean_identifier` + `identifier_preparer.quote` 的表名注入防护 |
| `test_datasource.py` | 标识符清洗与 `build_schema_text` 的按方言加引号 |
| `test_datasource_scope.py` | 数据源隔离：只暴露本数据源的表 |
| `test_intent_routing.py` | 意图分流、分支闭合性、赛题全量回归 |
| `test_doc_locator.py` | 文档表格定位：L2 上界、按内容去重、白名单、无命中回退 |
| `test_schema_samples.py` | 取值区：快照一致、预算、去重值、分支表 |
| `test_text2sql_stream.py` | 流式 text2sql：校验重试、重试 prompt 回带上次 SQL |
| `test_answer_format.py` | `detect_answer_format` / `render_list_answer` / `parse_analysis` |
| `test_schema_qa.py` | 表结构咨询命中与误判防护、确定性兜底 |
| `test_memory_trim.py` | 会话记忆每轮结果的行数截断 |
| `test_context_governance.py` | 摘要水位、折叠边界、知识库文件清单失效 |
| `test_milvus_timeout_kwarg.py` | Milvus 首次写入超时关键字冲突的规避（需本机 Milvus） |
| `test_chunking.py` | `chunk_text` 结构感知分块 |
| `test_sse_heartbeat.py` | SSE 保活、事件搬运、副作用次序、提前关闭时取消任务 |
| `test_agent_progress.py` | agent 流式进度：文案、并行调用、上限、形状不符时静默 |
| `test_analysis.py` | 统计算子数值、`filters` 多条件、`group_cagr`、跨列求交 |

另有 `batch_answer.py`（独立批量答题回填，仅依赖 `requests`）。

### 失败判读

- SQL / 标识符 / 数据源隔离：`test_sql_guard`、`test_identifier_quote`、`test_datasource`、`test_datasource_scope`。
- 意图分流与文档定位：`test_intent_routing` 第 12 段需外部赛题目录（`COMPETITION_DATA_DIR`），缺失时跳过并打印；`test_doc_locator` 用替身函数，不连 Milvus / embedding。
- schema / 取值区：`test_schema_samples` 用内存 SQLite + 替身，不连 Milvus / embedding / LLM。
- 流式 / SSE / agent 进度：`test_sse_heartbeat` 与 `test_agent_progress` 均用替身图 / 替身 agent，不连图、LLM、Mongo。
- 会话记忆与上下文治理：`test_memory_trim`、`test_context_governance`。
- 统计算子：`test_analysis` 覆盖聚合 / 相关 / 环比 / 占比 / 环比增长率 / 高于整体平均 / 多条件交集，以及回归 / 预测 / 异常 / 聚类；含「上期为 0 / 零基数 / 期数不足时增长率必须是 `None` 而非 `inf`」与「整数年份列未被当成 epoch 纳秒」的回归。
- Milvus 与分块：`test_milvus_timeout_kwarg` 需本机 Milvus；`test_chunking` 无外部依赖。
