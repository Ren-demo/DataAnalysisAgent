# 智能数据分析系统 —— 后端

FastAPI 后端：自然语言 → 意图分类 → LangGraph 编排（SQL 多方言流式 → 执行 → 图表类型 + `create_agent` 决策报告 / 统计建模分析 / 表结构确定性回答）/ 闲聊。

## 环境要求

- Python 3.10+
- 可访问 DeepSeek API（需配置 `DEEPSEEK_API_KEY`，本项目已配置）

## 快速开始

```bash
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境（Windows）
.venv\Scripts\activate
# （macOS / Linux）
# source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量（本项目已配置 DEEPSEEK_API_KEY，如需修改编辑 backend/.env）

# 5. 确保 MongoDB 运行于 localhost:27017（会话记忆依赖，缺失时记忆功能静默跳过）

# 6. 启动服务
uvicorn app.main:app --reload --port 8000
```

> 本机也可使用 conda 环境 `ChongqingAIApplication`（Python 3.13，依赖已装好）：`conda activate ChongqingAIApplication` 后直接启动，跳过 1–3 步。

启动后：
- 接口文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>
- 表结构：<http://localhost:8000/api/schema>

## Docker

生产镜像由项目根目录的 `docker-compose.yml` 统一编排（不在 backend 目录单独启动）：

```bash
cd ..                 # 项目根目录
cp .env.example .env  # 填 API Key、改 MongoDB 密码
docker compose up -d --build
```

镜像用多阶段构建：按 `requirements.txt` 安装依赖（含 `python-docx` / `pypdf` / `tavily-python` 三个可选依赖）；以非 root 用户运行，数据目录 `/app/data` 挂命名卷 `chongqing-ai_backend_data`。容器内通过服务名访问 `mongodb:27017` 与 `standalone:19530`。详细说明见根 README「六、Docker 部署」。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务信息（name / docs / health） |
| GET | `/api/health` | 健康检查 |
| GET | `/api/schema` | 数据源、表结构与关联关系（含 relationships） |
| GET | `/api/datasources` | 列出已上传 + 外部数据源 + 文档数据源（每个文档**只暴露一个文档级条目** `doc:<id>`） |
| POST | `/api/datasources/upload` | 上传 Excel/CSV（导入 SQLite 表）或文档 .md/.txt/.pdf/.docx（导入为 `doc:` 数据源） |
| DELETE | `/api/datasources/{data_source_id}` | 删除数据源（upload / doc / 外部连接，按 id 前缀分流） |
| GET | `/api/connections` | 列出已保存的外部数据库连接（不含密码） |
| POST | `/api/connections/test` | 测试连接（不保存），返回实时表结构或友好错误 |
| POST | `/api/connections` | 新增连接（先测通再保存到 MongoDB），返回新连接与表结构 |
| PUT | `/api/connections/{ds_id}` | 编辑已有连接（password 留空保留旧密码） |
| DELETE | `/api/connections/{ds_id}` | 删除连接 |
| GET | `/api/sessions` | 列出历史会话（按更新时间降序） |
| GET | `/api/sessions/{session_id}` | 获取指定会话详情（含消息列表） |
| DELETE | `/api/sessions/{session_id}` | 永久删除指定会话（含其全部消息） |
| POST | `/api/query` | 自然语言查询（非流式，默认不返回 SQL，仅提问明确要求时返回） |
| POST | `/api/query/stream` | 流式查询（SSE：`result` / `error`；提问明确要求时含 `sql_token`） |
| POST | `/api/chat/stream` | 统一流式对话（SSE：`intent` / `answer_token` / `progress` / `result` / `report` / `error`；提问明确要求时含 `sql_token`；静默期下发 `: keepalive` 注释行保活） |

### POST /api/query

```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "统计各分类的数量", "data_source_id": "upload:<表名>"}'
```

`/api/query` 与 `/api/query/stream` 是**旁路接口**（前端主入口走 `/api/chat/stream`），三者共用同一份 schema 文本组装逻辑（`build_source_schema_text`）：doc 源同样先做块定位收窄、再附取值区。早前这两个接口直接调 `build_scoped_schema_text`，对 doc 源会把该文档全部几百张表的 DDL 原样喂给模型，故障形态与主链路一致（模型挑错表 / 猜出不存在的过滤字面量 → 0 行）。

### POST /api/chat/stream

前端主入口，先做意图分类（`intent` 事件），再分流到数据查询（默认直接给结果并附简短自然语言分析，仅当提问明确要求输出 SQL 时才发 `sql_token`）或闲聊回答（`answer_token`）；数据查询成功后末尾附 `report` 决策报告事件（前端经 `ReportCard` 卡片展示标题 / 核心结论 / 关键发现 / 建议 / 置信度）：

`intent` 事件透传**真实意图**（`query` / `chat` / `retrieval` / `schema` / `analysis`），供前端在等待期间显示对应文案（「正在生成 SQL…」/「正在思考…」/「正在检索文档…」/「正在读取表结构…」/「正在做数据分析…」）；`redraw` 与 `query` 同属 SQL/表格家族一并上报为 `query`。注意这只是**等待文案**的标签，渲染家族仍由 `result` 事件的 `QueryResponse.kind`（只发 `query` / `chat`）决定，两者互不影响。`analysis` 必须原样透传：该分支一次 SQL 都不生成（走 pandas 统计算子），压成 `query` 会让整段分析全程显示「正在生成 SQL…」。

`progress` 事件（`{"type": "progress", "scope": "analysis" | "report", "text": "正在调用 group_cagr…"}`）由分析 / 决策报告 agent 的**每一轮工具调用**产出，供前端在等待期间滚动展示当前步骤 —— 这两个 agent 会连跑十几轮 LLM 往返，没有任何事件时界面全程静止。`scope` 决定前端显示位置：`analysis` 显示在 loading 块内（此时 `loading` 仍为真），`report` 单独开位（`result` 事件已把 `loading` 置 false，报告 agent 仍在跑）。最多播报 `_MAX_PROGRESS`（40）条，超限不再发。

**SSE 保活**：图内节点可能连续静默数分钟（`want_sql` 为假时 text2sql 的 token 不发，schema 反射 → SQL 流式生成 → 执行 → `summarize_query_result` 全程零事件，实测复杂提问的 `intent` → `result` 静默达 **205 秒**；其后的分析 / 报告 agent 更久）。后端静默超过 `SSE_HEARTBEAT_SECONDS`（默认 10s）即下发一行 SSE **注释**（`: keepalive`）——注释行同样让 `reader.read()` 返回并重置前端的**空闲**计时器，而前端 `consumeSSE` 与 `batch_answer.py` 都会跳过它，两条既有消费端无需改动。**不设总时长上限**：答案质量优先，允许跑到自然结束，用户可随时点「停止」（前端 `AbortController`）。

⚠️ 改动 `app/agents/graph.py` 的 `_route` 时注意：它的返回值是 `_CLASSIFY_BRANCHES` 的**键名**（意图名），**不是**节点名。LangGraph 在 `_branch._finish` 里用它查表，查不到就抛 `KeyError`。早前 `retrieval` 分支返回的是节点名 `"retrieve"` 而键是 `"retrieval"`，**整条 RAG（文档问答）链路每次都以 `SYSTEM_ERROR` 收场**，而类型检查与静态阅读都看不出问题。`scripts/test_intent_routing.py` 第 11 段为此加了分支闭合性断言（覆盖全部意图 + 未知识图兜底 + 分支表指向真实节点）。

```bash
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "统计各分类的数量", "data_source_id": "upload:<表名>", "history": []}'
```

## 数据源

- **上传**：`upload:<表名>`（Excel/CSV 导入到 `data/uploads.db`）。
- **文档**：`doc:<id>`（.md/.txt/.pdf/.docx 解析后表格入 `data/docs.db`、文本结构感知分块后向量化入 Milvus）。每个文档**只暴露一个文档级条目** `doc:<id>`（`document_service.list_documents`），选中后由程序自动解析出该文档提取出的**全部表格**，用于表结构回答或数据查询 —— 单张提取表的条目**不会**出现在前端下拉里。
  多块文档（如被 `=== <块id> ===` 切成数百张小表的赛题 `Table_Query-数据.md`）支持**块 id 寻址** `doc:<doc_id>:<块id>`（如 `doc:<doc_id>:03b8923724cf4b9`）：`app/database/engine.py` 的 `_resolve_doc_table` 把第三段经 `get_doc_table_by_block` 映射到该块对应的真实表，`resolve_table_names` 返回单表。该寻址用于「先定位块、再作答」（见「表结构 / 列名咨询」一节），只在题干显式带块 id 时走，不是下拉项。
  标识符**保留原始名**（中文 / 空格 / 数字开头均保留），仅替换引号、分号与控制字符，入库时由 SQLAlchemy 按方言自动加引号；`doc_blocks.columns` 另外持久化**清洗前的原始表头**，供 schema 咨询与 RAG 分块文本按原始名作答（旧 `data/docs.db` 里已清洗的表同样经此还原）。
  **问答路径分流**（`app/llm/intent.py` 的 `_route_doc_intent`）：问句**指向文档正文**时一律改判为 `retrieval`，走 Milvus 检索 + 流式回答并附出处引用，**不再**因问句里出现「多少 / 统计 / 排名」这类聚合词而被回落到 SQL —— 聚合词只描述答案的**形态**（是个数），不指示答案**在哪里**；早前「命中检索词且不命中聚合词」的否决条件会让「报告里提到的退款金额是多少」去查从文档抽出的表格，问正文却答表格。不含正文指向的问句（如「ANON-…-0001 这张卡 2024-03-01 的消费金额是多少」）仍走 SQL 查文档表格：Milvus 里表格块只索引「列名 + 前 5 行样本」，纯 RAG 找不到具体行，**按行查找必须由 SQL 覆盖全量数据**。该分流只作用于 `query` / `chat`（`schema` 问句在上一步已锁定，`analysis` 不参与），且**就地改判**——只改 `intent`，`chart_hint` / `want_sql` / `answer_format` 原样保留（早前返回新建的 `IntentResult`，把 LLM 那一轮判出来的副产物全丢了）。

  词表分三类（`_DOC_NOUN_WORDS` / `_DOC_PHRASE_WORDS` / `_TABLE_SCAN_WORDS`），判定顺序是「**表格信号一票否决 → 文档名词 → 正文短语**」：

  - **表格内部定位/取值信号一票否决**：「这份文档的表格中第三列的列名是什么」问的是表结构而非正文，`单元格 / 表格中 / 表中 / 列名 / 哪一列 / 第几行 / 这张卡` 任一命中即不转 retrieval。
  - **只收文档名词的明确指代**：`报告里 / 报告中 / 这份报告` 收，**裸「报告」不收** —— 财报语境里它是动词（「被报告为当前负债」「报告的净亏损」），实测赛题 `Domain-specific` 239 题里正是这 2 题被裸「报告」误判进 RAG。
  - **不收裸动词**：`查找 / 找一下 / 描述 / 介绍` 曾经都在词表里，于是「查找 ANON-…-0001 这张卡 2024-03-01 的消费金额」因为一个「查找」被赶去 RAG，而答案在文档抽出的表里。

  ⚠️ **检索词表不得收「内容」**：它太泛，任何提及「内容」二字的问句都会命中。赛题 `Table_Query` 的「表格中内容为 X 的单元格上面单元格的内容是什么？」里的「内容」指的是**单元格取值**，与文档正文无关 —— 实测 500 道题里 287 道（57.4%）都含「内容」，收进来会把过半的按单元格查找题赶去 RAG，而这些题 Milvus 里只有「列名 + 前 5 行样本」必然答不出。信号要锚在**文档名词**或明确的正文指向短语上；「文档/报告/资料」已足以承载正文指向，「这份文档的内容是什么」照样命中。

  回归断言见 `scripts/test_intent_routing.py` 第 8~10 段；第 12 段是**赛题全量回归**（三份题目集共 787 题，断言改判 retrieval 的题数为 0，题目集在仓库外、缺失时跳过并打印）。

  **schema 文本的单一入口（`build_source_schema_text`）**：三条查询链路（`/api/query`、`/api/query/stream`、`/chat/stream` 的 `_prepare_query_node`）**统一**经 `app/database/engine.py` 的 `build_source_schema_text(engine, data_source_id, question)` 取 schema 文本：先做多表收窄，再按表数有界附加**取值区**。早前这段逻辑只写在 `_prepare_query_node` 里，于是 `/api/query` 与 `/api/query/stream` 对 doc 源会把该文档**全部 239 张表的 DDL** 原样喂给模型（既无收窄也更无取值）。`_chat_node` 刻意**不**走这个入口 —— 闲聊不生成 SQL，用不到取值区，而取值区对小表是整表 dump，每句闲聊都付这个代价不合理。

  **多表收窄（`engine.locate_doc_tables`）**：文档源下 `doc:<id>` 选中后会解析出该文档提取出的**全部**表格（赛题 `Table_Query` 500 张、`Multi-step` 48 张），直接把整份 schema 交给模型等于让它盲选。`app/database/engine.py` 的 `locate_doc_tables` 按「题干块 id（精确）→ 向量语义召回（粗）」两级收窄到 N 张候选表。它住在 `engine.py` 而非 `graph.py`，是因为 `graph.py` 反向依赖 `query_service.execute_sql`，`query_service` 再 import `graph` 会成环；`graph._locate_doc_tables` 保留为 async 薄壳（`asyncio.to_thread` 包装）供 `_schema_node` 使用，**三条链路共用同一份实现**，避免逻辑漂移：

  - 召回走 `document_service.retrieve_doc_scored`，它按 L2 距离阈值过滤（`doc_table_max_distance`，最优命中都超阈值就判「没定位到」），只保留不差于「最优 + `doc_table_score_margin`」的命中；
  - 命中块经 `get_doc_table_by_block` 映射到真实表名，并**按内容去重** —— 同一张表在文档里常被复制成多个块（赛题咖啡表 4 份、能源表 5 份，内容逐字相同），不去重就会把同构表的多个副本一起交给模型，它分别聚合再把结果相加，**数值直接翻倍**且形态完全正常；
  - 只认本数据源的表（`table_names` 白名单）；
  - **定位不到就回退全量 schema + `logger.info`**，维持既有行为不恶化（收窄方向可能反转风险：现在是「模型可能选错」，收窄后变成「定位错则确定错」，故失败时宁可退回不收窄）。

  ⚠️ **阈值是「上界」**：Milvus 集合的 metric 是 **L2 距离，越小越相似**（与余弦相似度方向相反）。默认 `0.95` / `0.10` 由 2026-09 用 `text-embedding-v4` + 赛题 `Multi-step` 数据实测标定（真命中 0.87 / 语义无关 0.98~1.03 / 完全无关 1.43），**换 `EMBEDDING_MODEL` 必须重标**。自检见 `scripts/test_doc_locator.py`。

  **取值区（`build_schema_text(..., with_samples=True)`）**：定位对了表**不等于**答得出题 —— 若 schema 文本只有 DDL、零取值，模型没见过任何字面量，只能照中文问题猜一个不存在的值。文档抽出的表大多正是「行标签 + 数值」的**透视表**，语义全在某一列的**取值**里：

  ```
  -- 表 doc_a85671194933_10 共 9 行，全部数据：
  -- col | "amount ( in thousands )"
  -- 2009 | $ 32452
  ...
  -- less : amount representing interest | 73512     ← 问题要的就是这一行
  ```

  故 `with_samples=True` 时在每张表的 `CREATE TABLE` 之后追加**以 `--` 注释行**书写的取值区（用注释行是为了留在模型已熟悉的 DDL 块内，不引入散文体，也不影响任何 SQL 解析）：

  - 行数 ≤ `SCHEMA_FULL_DUMP_ROWS`（默认 50）→ 给**全部行**；
  - 否则 → 前 `SCHEMA_SAMPLE_ROWS`（默认 20）行，另加**文本列的完整去重取值**（限 `SCHEMA_MAX_DISTINCT_VALUES`，默认 30 个），覆盖「答案行落在样本窗口之外」；去重值**超上限就整列不给** —— 截断的清单会让模型以为「就这些」，比不给更糟；
  - 总预算 `SCHEMA_MAX_CHARS`（默认 60000），用尽即停止追加并在末尾注明「另有 N 张表的取值区因超出字符预算已省略」（**留痕，不静默截断**）；单表读值失败只跳过该表，不毁掉整份 schema；
  - 表数超过 `SCHEMA_MAX_TABLES`（默认 5）就整份不附取值。**回退全量（doc 源 239 张表）时也刻意不带取值** —— 实测每张表都带样本会多出约 7.9 万字符（DDL 本身才 3.9 万），prompt 直接翻三倍，而块 id / 向量定位已覆盖绝大多数情况。

  text2sql 的 system prompt 配套要求：**WHERE / HAVING 的取值必须来自表结构里给出的真实取值，原样照抄，不得杜撰**；表结构里没有所需取值时不要猜，先写不带该条件的查询把全表拉出来。自检见 `scripts/test_schema_samples.py`。

  **表格块只索引「列名 + 前 5 行样本」**（`document_service.import_document`），而 docs.db 里存的是**全量数据**。故 RAG 命中表格块时，`graph._chunk_header` 会给片段抬头加「【表格摘录：以下仅为该表的部分样本行，全表共 N 行，非全部数据】」，`chat_service._RETRIEVAL_SYSTEM_PROMPT` 配套要求**不得**据此回答计数/合计/平均值/占比等需要全量的统计 —— 不交代的话模型会拿 5 行样本算出并报出一个形态完好的**假统计量**，比「答不出」更糟，因为它看起来像答案。需要全量计算的问句应由用户在数据源上直接提问，走 SQL / 统计算子。
- **外部（静态）**：MySQL / PostgreSQL，经 `backend/datasources.json` 配置（当前为空列表）。
- **外部（运行时）**：MySQL / PostgreSQL，前端「连接数据库」手动输入主机 IP / 端口 / 库名 / 用户名 / 密码，保存到 MongoDB `connections` 集合并跨会话复用；连接成功即返回实时表结构（表 + 字段），提问时后端先 `inspect` 表字段再生成 SQL。接口见 `/api/connections`。

## 会话记忆（MongoDB）

- 单次对话以 `session_id` 区分，记录「用户问题 + 助手回答 + 最后使用的数据源」到本地 MongoDB（`localhost:27017`，库 `chongqing_ai`，集合 `sessions`）。
- `session_id` 由前端 `crypto.randomUUID()` 生成并存 `localStorage`；刷新页面自动恢复当前会话，历史会话侧栏可切换，「新对话」按钮新建。
- 接口：`GET /api/sessions`（历史会话列表）、`GET /api/sessions/{session_id}`（会话详情）、`DELETE /api/sessions/{session_id}`（删除会话）。
- 记忆读写失败静默降级，不影响主查询链路。

## 长对话记忆治理（闲聊链路）

闲聊分支的多轮上下文会随会话变长而 token 膨胀，为此采用「近期窗口截断 + 旧对话滚动摘要」双层治理：

- **近期窗口**：只保留最近 `CHAT_HISTORY_MAX_MESSAGES`（默认 20）条历史进 prompt，另有 `CHAT_HISTORY_MAX_TOKENS`（默认 2400）的 token 预算做二次裁剪。
- **滚动摘要**：窗口之前的旧对话增量折叠成一段 ≤ `CHAT_SUMMARY_MAX_CHARS`（默认 300）字的摘要，存于会话文档的 `summary` / `summary_upto` 字段；喂给 LLM 时以「摘要 + 近期窗口」替代全量历史，保留长期记忆。
- 三者均可在 `backend/.env` 用同名大写键覆盖（`CHAT_HISTORY_MAX_MESSAGES` / `CHAT_HISTORY_MAX_TOKENS` / `CHAT_SUMMARY_MAX_CHARS`）。
- 全程静默降级：MongoDB / LLM 不可用时回退为纯截断，不产生 error 事件。

> 说明：意图分类本身已截取最近 6 条历史；text2sql / 决策报告 / 重绘链路本就不携带历史，不受此治理影响。

## 图表模式（严格）

仅当用户显式指定柱状图 / 折线图 / 饼图时才返回对应 `chart_type`（bar / line / pie），否则一律 `table`，不做自动推断出图。

## SQL 展示策略（默认隐藏）

数据查询默认**直接回答结果、不返回 SQL 语句**；仅当用户明确要求输出 SQL（如「输出 SQL」「给我 SQL 语句」「查询语句」）时才经 `sql_token` / `result.sql` 返回。识别用 LLM（意图分类的 `want_sql` 字段）+ 关键词规则兜底（`app/llm/intent.py` 的 `detect_want_sql`），三个查询接口（`/api/query`、`/api/query/stream`、`/api/chat/stream`）统一生效；SQL 在图内部（`state.sql`）仍保留，供决策报告 agent 使用。

## 决策报告（Agent 编排）

后端用 LangGraph `StateGraph` 编排，查询分支末尾由 `create_agent` 工具调用智能体对查询结果做多步下钻推理，产出结构化 `report` 事件（标题 / 核心结论 / 关键发现 / 建议 / 置信度）。工具为本地 `get_schema` / `generate_sql` / `execute_sql`（执行前过 `validate_sql` 只读校验），并视配置追加可选工具 `web_search`（联网搜索，DuckDuckGo / Tavily，失败静默降级）与 `knowledge_search`（本地知识库检索，依赖本地知识库 + Milvus 向量检索）。

### ⚠️ 数据外发与配置卫生

- **联网搜索默认关闭**（`WEB_SEARCH_PROVIDER=none`）。一旦设为 `duckduckgo` / `tavily`，决策报告 agent 在推理过程中**会把用户提问按原样发往第三方搜索服务** —— 这是数据外发行为，请自行评估是否可接受。`knowledge_search` 只读本地目录 `data/knowledge`，不外发数据。
- 用户提问本身会发往 DeepSeek API（`DEEPSEEK_BASE_URL`），embedding 会发往 `EMBEDDING_BASE_URL`，均为链路必需。
- `backend/.env` 含真实密钥，**不要提交到版本库**；仓库只提交占位模板 `backend/.env.example`（复制为 `.env` 后填写）。本仓库当前**不是 git 仓库**，一旦 `git init`，请先 `git status --porcelain` 确认 `.env` 未被跟踪。
- 若密钥曾经外泄（截图、日志、共享终端等），到 DeepSeek / 阿里云控制台**轮换密钥**后再更新 `.env`。

## 数据分析（统计建模）

对「相关性（正相关/负相关）、环比/同比、**年均增长率（CAGR）**、趋势/时序、预测、异常、聚类」等 SQL 难以单条表达的复杂统计问题，意图分类会命中 `analysis` 分支，改用 `create_agent` + **预定义统计算子**（`app/agents/stat_tools.py`）在 pandas/numpy 侧计算，而非生成单条 SQL。除统计聚合外，还覆盖高级建模（线性回归 / 时序预测 / 异常检测 / K-Means 聚类，numpy 手写零新依赖）。关键词兜底表（`app/llm/intent.py` 的 `_ANALYSIS_WORDS`，仅在 LLM 分类不可用时生效）同步收录了「年均增长率 / 复合增长率 / 年平均增长率 / 平均年增长率 / CAGR」完整短语 —— 只收完整短语、不收裸「增长率」。

算子均为只读（只读库、只对内存 DataFrame 聚合，`df_store` 请求级隔离、不暴露任意 SQL）：

| 算子 | 作用 |
| --- | --- |
| `get_schema` | 返回当前数据源表结构 |
| `load_table` | 读表到内存（可选过滤，`filter_op` 支持 eq/ne/gt/ge/lt/le/contains/not_contains，如 `cash_type=card`、名称含 `Latte`）；**`filters` 可一次给多个条件（AND）**，形如 `[{"col":"category","op":"eq","value":"Historical"}, {"col":"powertrain","op":"eq","value":"BEV"}]` —— 宽表（region/category/parameter/mode/powertrain/year 逐维度展开的长表）定位目标数据要同时固定四五列，一列一轮就是四五轮工具调用。返回 `filters_applied` 逐条回传筛完剩余行数，筛空时一眼看出是哪个条件筛没的。文本列自动推断数值/日期类型 |
| `group_aggregate` | 分组聚合（count/sum/avg/min/max，可按时间降采样按月/季/年） |
| `group_correlate` | 按组求两列的皮尔逊相关系数（判断正/负相关） |
| `group_mom_growth` | 按组取最近 n 期，判断**是否连续**环比增长（严格逐期递增，布尔） |
| `group_mom_growth_rate` | 按组算环比增长**率**并排名（「增长最快/排第几/增长了多少」）；`rank_by` 可选 `last`（末期环比）或 `mean`（各期环比均值） |
| `group_cagr` | 按组算区间内的**年均增长率**（`CAGR = (末值/首值)^(1/期数) - 1`，`start`/`end` 给区间、`date_freq="year"`）；`method` 可选 `cagr`（复合，默认）或 `aagr`（各期增长率算术平均）。返回 `growth_rate`/`growth_rate_pct`/`periods_count`/`start_value`/`end_value`/`ranking`/`rank`。**与 `group_mom_growth_rate` 的分工是「年化 vs 环比」**：问「2015-2020 年间年均增长率」必须用它，拿环比算会把 5 年的总增速当成一年的。首值/末值非正或期数 <2 时该组 `growth_rate` 为 `None` + `note`，**绝不返回 `inf`** |
| `group_share` | 按组求**占比** + 是否高于平均占比（1/组数） |
| `group_vs_overall_avg` | 按组求指标并与**全体组平均值**比较（客单价高于全品类平均、保有量高于平均值）——与 `group_share` 不同，这个比的是**水平值**不是占比 |
| `intersect_groups` | 把若干「每组一行」的算子结果按分组列合并，求**同时满足全部条件**的组（多步检索「哪些…同时满足 A、B、C」的落点）。条件的 `value` 还可以写成**列引用** `{"col": "另一列", "factor": 0.5}`（可加 `offset`），表示「本列 > 另一列 × 0.5」—— 「预测期增长率高于历史均值」「增量超过历史总增量 50%」这类**同一主体两段区间互比**只能这样表达 |
| `linear_regression` | 一元线性回归（y ~ x），返回斜率/截距/拟合优度 R²/相关系数 r |
| `forecast_time_series` | 时序外推预测（linear 线性外推 / average 滚动移动平均 / growth_rate 复合增长率，可选按组） |
| `detect_anomalies` | 异常检测（z-score 标准分数法 / IQR 箱线法） |
| `cluster_kmeans` | K-Means 聚类（KMeans++ 初始化，写回簇标签供后续聚合） |

各 `group_*` 算子除返回嵌套 `results` 外，还会注册一张**每组一行**的指标表并返回其 `df_id`，供 `intersect_groups` 做确定性求交 —— 多步检索题的最后一跳是集合求交，让模型自己在几十个组之间心算很容易错。若两个表有同名指标列，合并时**首个保留原名、后者加 `_<df_id>` 后缀**，并在返回值里回传 `columns` 让模型看到真实列名（不回传就会照旧名去筛，条件静默失效）。`group_cagr` 的指标表额外带 `start_value`/`end_value`，「末年保有量超过 X」可直接用 `{"col": "end_value", "op": ">", "value": 100000}` 求交，不必另跑算子。

⚠️ **新增算子必须同步登记在 `build_stat_tools` 末尾的返回列表里** —— `@tool` 只是包装，不登记模型连「有这个工具」都看不到。

⚠️ **日期列一律经 `_to_datetime` 解析，不要直接 `pd.to_datetime`**：整数列会被按 **epoch 纳秒**解释（年份 `[2015, 2016]` → `1970-01-01 + 2015ns`，`.dt.year` 一律得到 **1970**），所有年份塌缩成同一个期，按年聚合 / 环比 / 年均增长率只会报「期数不足（<2）」，形态上像「数据不够」而不报错。`_infer_types` 恰好会把「全是数字的年份文本列」转成 int64，文档表格（导入时列全为 TEXT）首当其冲。`_to_datetime` 对整值且落在 `[1000, 9999]` 的数值列显式按 `%Y` 解析，其余仍走默认推断。

`analysis` 分支是**增强路径**：任何失败都会回退到既有 text2sql 查询链路（`app/agents/graph.py` 的 `_analysis_node`），不伪造结果、不阻断。提示词由 `app/agents/analysis.py` 的 `build_analysis_system_prompt(schema_text)` 拼出 —— 规则 + **内联的表结构**（省掉「先调 get_schema」的整轮往返），其中最关键的一条是「**先一次性规划、把能并行的工具调用放在同一轮**」（同一轮的多个 `tool_calls` 会被 `ToolNode` 并行执行）。早前的提示词是逐步引导（先用 A、再用 B、再用 C），等于教模型每轮只发一个调用，一道多条件题因此要跑十几轮 LLM 往返（每轮 30~60s）—— 这正是「流式响应长时间无数据」的耗时根因。算子数值正确性由 `scripts/test_analysis.py` 离线自检（`cd backend && python scripts/test_analysis.py`）。

## 输出格式（遵循用户指定）

当用户明确要求以列表/JSON 形式返回答案（如「返回列表」「只返回名称」「以 JSON 返回」），或问题隐含「哪些/哪类/哪几种…」（如「哪些咖啡品类同时满足…」）时，后端会把答案文本（`QueryResponse.explanation`）渲染为**纯列表字符串**（如 `['Latte', 'Americano with Milk']`，无匹配为 `[]`；JSON 格式为 `["Latte", ...]`），同时仍保留明细表格。若问题是单答案（「最高/最低/最多/唯一/哪个…」），`detect_answer_format` 判定为 `single`，后端只把单个名称写入 `explanation`（不带括号引号）。识别用纯关键词规则 `app/llm/intent.py` 的 `detect_answer_format`（`answer_format` 写入 `state`）；落点：analysis 分支（`_analysis_node` 从 `AnalysisResult.items[].name` 取名称）与 query 分支（`_execute_node` 取结果首列），经 `render_list_answer` 渲染；其余情况仍输出自然语言分析。此外后端会在 `QueryResponse.analysis` 附带**分析说明**（query 分支用 `summarize_query_result` 生成、analysis 分支用 agent 计算推理 `AnalysisResult.explanation`，均静默降级），前端分「回答」（`explanation`）与「分析说明」（`analysis`）两个区块展示。

## 表结构 / 列名咨询（LLM + 全局 schema 视角）

「第 1 列的列名是什么？」「哪一列被称为谓词？」「名为 X 的列是第几列？」「这个表有哪些字段？」「有几列 / 多少行？」这类**元数据咨询**问题，命中 `schema` 意图分支。主路径用**模型驱动**回答：给模型一份按列序编号、含样本行的表结构总览，让模型自行把中文列名（谓词→predicate、主题→subject、日期→date 等）映射到真实英文字段并给出列序，不硬编码词典；LLM 不可用时回退到 SQLAlchemy 反射的确定性回答。

- 识别：`app/llm/intent.py` 的 `detect_schema_question` 纯规则识别（第 N 列 / 哪一列被称为 X / 名为 X 的列 / 列名 / 字段 / 表结构 / 列数 / 行数），在 `classify_intent` 里优先于 chat/query 与 doc 的 retrieval 分流锁定为 `schema`；已收紧「第 N 列」问句后缀、优先「哪一列…」疑问句，避免把赛题前置语境「第一行和第一列标记数据编码的起点」里的「第一列」误判为第 N 列。
- 回答（主）：`app/services/schema_service.py` 的 `build_schema_overview` 生成全局总览（带序号的列 + 样本行，多表有界截断），交 `app/services/chat_service.py` 的 `schema_consult_stream`（低温、反编造）流式回答。
- 回答（兜底）：`app/services/schema_service.py` 的 `answer_schema_question` / `answer_schema_question_multi` 用 `inspect(engine).get_columns(table)` 读列序、`SELECT COUNT(*)` 读行数，直接拼中文答案（如「第 1 列的列名是 date。」）；`NO_API_KEY` 等 LLM 失败时回退。
- 原始列名保留：文档表列名入库时**保留原始名**（中文 / 空格 / 数字开头均保留，仅替换引号、分号与控制字符），`document_service.import_document` 另外把**清洗前的原始表头**存进 `doc_blocks.columns`（JSON）；`schema_service._columns` 对 `doc_` 表返回原始名，使「哪一列的名称为 0」「第 1 列的列名」这类问句命中真实表头（如 `0 是第 1 列`），`build_schema_overview`（LLM 总览）与 RAG 分块文本同样用原始名。text2sql 的 schema 文本用 `identifier_preparer.quote` 按方言加引号包裹真实列名（生成 SQL 必须原样照抄）。旧 `data/docs.db` 里已被清洗过的表（数字表头 `0/1` → `t_0/t_1`）仍经 `doc_blocks.columns` 还原原始名作答。
- 表格定位：文档数据源只暴露**文档级条目** `doc:<doc_id>`（`app/services/document_service.py` 的 `list_documents`）；选中后由程序自动读取该文档提取出的全部表格（查询/schema 咨询均如此）。针对「多块文档」（如赛题 `Table_Query-数据.md` 被 `=== <块id> ===` 切成数百张独立小表、各块列不同），支持**块 id 寻址** `doc:<doc_id>:<块id>`：`_resolve_doc_table` 把第三段（块 id，12~16 位十六进制）经 `get_doc_table_by_block` 映射到真实表名，`resolve_table_names` 返回单表。`_schema_node` 据此**先定位块、再作答**——按「块级数据源（单表）→ 题干块 id（`_extract_block_id`）→ RAG 语义命中（`retrieve_doc`，仅 `column_by_name` 问句）」三级收窄到目标块，再交 `build_schema_overview`（单表总览）回答；位置问句（「第 N 列」）在整篇文档且无块 id 时保留全局总览兜底（此类题干信息不足导致的本质歧义需靠块 id 寻址解决）。
- 自检：`cd backend && python scripts/test_schema_qa.py`（无 LLM 依赖，覆盖命中/误判防护与内存 SQLite 上的确定性断言）。

## 运行配置与资源上限

以下均可在 `backend/.env` 用同名大写键覆盖（默认值见 `app/config.py`）。

### 超时与重试

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_TIMEOUT_SECONDS` | 60 | 单次大模型调用超时。SDK 默认 600s × 2 次重试，不设时单次调用最长可挂 30 分钟 |
| `LLM_MAX_RETRIES` | 1 | 大模型调用重试次数 |
| `MILVUS_TIMEOUT_SECONDS` | 30 | Milvus 检索 / 写入超时（不可达时不要无限等） |
| `EMBEDDING_TIMEOUT_SECONDS` | 30 | embedding 接口超时 |
| `SSE_HEARTBEAT_SECONDS` | 10 | 图静默超过该秒数即下发一行 SSE 注释（`: keepalive`）保活，避免被前端空闲超时单方面掐断。远小于前端阈值即可 |
| `AGENT_RECURSION_LIMIT` | 40 | 分析 / 决策报告子 agent 的**步数**上限（super-step，约等于 2× 工具轮次）。这是**兜底**而非时间预算：保活之后模型若陷入「参数错→重试→参数错」的死循环会**无限期**烧 token。⚠️ langgraph 1.1.2 的默认值是 **10000**（等于不设限），故此项是在**收紧**；默认 40 步对多步检索类问题（约 3 轮）有 6 倍余量。撞上后 analysis 静默回退 text2sql、report 静默跳过（与既有降级姿态一致） |

**后端没有「一次提问」的整体超时**：上表都是**组件级**超时（单次 LLM 往返、单次 Milvus／embedding 调用）或**步数**上限，一条查询链路可能串起多次调用，因此单次提问的总耗时不受这些数值直接约束，慢查询会一直跑到出结果为止（用户已确认答案质量优先）。前端的 `SSE_IDLE_TIMEOUT_MS`（600s，**两次 SSE 事件之间**的最大间隔）原本是唯一的掐断路径，会把分析 / 报告这类「跑十几轮但中途零事件」的健康长任务误杀 —— 现在由后端心跳保活兜住，该阈值退化为「后端进程整体失联」的兜底；界面的「停止」按钮（`AbortController`）始终可随时中止，且生成器被关闭时会 `task.cancel()` 掉图任务，不留继续烧 LLM 的孤儿任务（自检：`scripts/test_sse_heartbeat.py`）。

### 资源上限

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | 20 | 单个上传文件大小上限（Excel/CSV/文档共用）。只读「上限 + 1」字节即可判定超限，不把超大文件整个读进内存 |
| `MAX_QUESTION_CHARS` | 2000 | 单条提问长度上限 |
| `MAX_HISTORY_MESSAGES` | 200 | 请求历史保留的最近条数（超出丢弃最早的，不报错） |
| `MAX_MESSAGE_CHARS` | 20000 | 单条历史消息长度上限（助手答案可能较长，故放宽） |
| `MAX_RESULT_ROWS` | 5000 | 查询结果行数上限，超出截断并在 `QueryResponse.truncated` 标注、说明文字中提示「请缩小范围」。用 `chunksize` 只取首块判定，不先 materialize 全表 |
| `MEMORY_MAX_ROWS` | 200 | 会话记忆里每轮结果**持久化**的行数上限（Mongo 单文档 16MB 保护） |
| `SCHEMA_FULL_DUMP_ROWS` | 50 | 行数不超过它的表在 schema 文本里直接给**全部行**（取值区） |
| `SCHEMA_SAMPLE_ROWS` | 20 | 超过上一条的表只给前 N 行样本 |
| `SCHEMA_MAX_DISTINCT_VALUES` | 30 | 文本列的去重取值上限；去重值更多的列不给取值清单（截断的清单会误导模型） |
| `SCHEMA_MAX_CHARS` | 60000 | 取值区总字符预算，用尽即停止追加并在结尾注明省略了几张表 |
| `SCHEMA_MAX_TABLES` | 5 | 表数超过它就不附取值区。doc 源**回退全量**（239 张表）正落在这一支 |

> 这些是**资源保护**，不是权限限制 —— 不改变「任何能访问该端口的用户均可修改数据源」的现状。

### 决策报告开关

`REPORT_ENABLED`（默认 `true`）。报告链路在同一次 SSE 响应内**串行**于主结果之后，且报告 agent 会再跑一轮 SQL 生成/执行，单次查询因此多付出若干次 LLM 往返；压延迟或成本时可设为 `false`（前端 `report` 事件的处理保持不变，只是不再收到）。

### 启动期与关停

- `lifespan` 内先经 `asyncio.to_thread(ensure_docs_schema)` **初始化 docs.db 基础表**（`doc_meta` / `doc_blocks`，幂等；`data/` 目录不存在时先建目录）。此前只有导入文档时才会建表，于是「docs.db 文件已存在但里面没有表」的环境会让下面这步对账以 `no such table: doc_meta` 抛出、打出一整页 traceback。
- 再做一次**孤儿文档向量对账**（`document_service.reconcile_orphan_vectors`）：清理「`doc_meta` 记录已删、Milvus 向量没删掉」的残留。会真的连 Milvus（连不上则内部跳过），故同样经 `asyncio.to_thread` 移出事件循环；失败只记日志，不影响启动。未导入过任何文档时会跳过（`doc_meta` 不存在是正常状态，不告警）。
- 关停走 `try/finally`：finally 中 dispose 全部缓存引擎（`dispose_all_engines`）并关闭 Mongo 客户端。
- 本地知识库语料以**文件级清单**（相对路径 + `mtime_ns` + 大小）为缓存失效键，原地编辑知识文件（文件名与大小不变）同样会触发重建；清单检查、缓存查找与索引重建在同一把锁下完成，重建先于缓存发布。

### 日志

关键降级路径统一走 `logger.warning(..., exc_info=True)` 而非静默 `except: pass`：查询结果分析说明失败、数据源解析失败、schema 确定性兜底中的单表失败、文档删除后 Milvus 清理失败、连接列表读取失败、摘要生成失败等；SQL 执行失败会记录完整语句与栈。

## 错误处理

模型不可用时不设规则兜底，而是返回带 `error_code` 的友好提示：

| error_code | 含义 |
| --- | --- |
| `NO_API_KEY` | 未配置 `DEEPSEEK_API_KEY` |
| `LLM_UNAVAILABLE` | 大模型服务不可用（初始化失败 / 网络 / 超时） |
| `SQL_ERROR` | 生成的 SQL 无法执行或多次校验未通过 |
| `DATASOURCE_UNAVAILABLE` | 数据源不可用：外部连接失败，**或**请求里的 `data_source_id` 已失效/未知（如连接在弹窗里被删掉、顶栏仍停在旧 id）。前端「修复连接」入口只认这个码与 `DATASOURCE_CONNECT_FAILED` |
| `DATASOURCE_CONNECT_FAILED` | 新增/编辑连接时测试连接失败（主机/端口/账号/库名不可用） |
| `NO_PRIOR_RESULT` | 重绘图表前当前会话尚无可用查询结果 |
| `SYSTEM_ERROR` | 其它未预期异常 |

## 离线自检

`scripts/test_*.py` 均为**独立脚本**（无 pytest 依赖，直接 `python scripts/xxx.py`，退出码非 0 表示失败）。除两处环境前提外，都不依赖 LLM / MongoDB：`test_analysis.py` 需 `data/docs.db` 存在；`test_milvus_timeout_kwarg.py` 虽用替身客户端、**不写入任何真实集合**，但构造 store 时 `langchain_milvus` 内部会走 pymilvus ORM 的 `connections.connect()`（替身覆盖不到），故**需本机 Milvus 可达**——Milvus 未启动时它会以 `Fail connecting to server on localhost:19530` 失败，属环境问题而非回归。

| 脚本 | 覆盖 |
| --- | --- |
| `test_sql_guard.py` | SQL 只读守门：DML CTE（`WITH del AS (DELETE ...) SELECT * FROM del`）被拒、`UNION`/`INTERSECT`/`EXCEPT` 被放行、多语句被拒 |
| `test_identifier_quote.py` | `_clean_identifier` + `identifier_preparer.quote` 组合下的表名注入防护 |
| `test_datasource.py` | 标识符清洗与 `build_schema_text` 的按方言加引号 |
| `test_datasource_scope.py` | 数据源隔离：`build_scoped_schema_text` 只暴露本数据源的表（不串表） |
| `test_intent_routing.py` | 意图分流回归：`detect_schema_question` 不劫持「统计…有多少条」类数据查询；输出格式判定；文档源 `_route_doc_intent`（指向文档正文 → retrieval，不再被聚合词否决；不含正文指向的表格问句仍走 SQL；裸动词/裸「报告」不误伤；表格信号一票否决；改判时就地保留 `chart_hint`/`want_sql`）；**编排图分支闭合性**（`_route` 的返回值必须都在 `_CLASSIFY_BRANCHES` 里、且在编译出的图中真实存在）；**赛题全量回归**（三份题目集 787 题，断言改判 retrieval 数为 0；题目集在仓库外、缺失时跳过并打印，`COMPETITION_DATA_DIR` 可指定目录） |
| `test_doc_locator.py` | 文档表格定位：`retrieve_doc_scored` 的 L2 上界阈值与 margin 成组保留；`locate_doc_tables` 的块 id 精确命中 / 白名单过滤 / **按内容去重**（同表多副本不能各占一个候选）/ 检索失败与无命中返回空；`_chunk_header` 对表格块的「摘录 + 全表行数」标注。替身函数驱动，不连 Milvus / embedding |
| `test_schema_samples.py` | 取值区：小表全量 dump 且**关键取值确实进了 prompt**（回归靶子）；`with_samples=False` 与改造前**逐字节一致**（纯 DDL 快照）；大表给前 N 行样本 + 文本列全部去重取值（数值列不给）、去重值超上限则整列不给；字符预算用尽即停止并注明省略张数；单表读值失败不影响其它表；表数超 `SCHEMA_MAX_TABLES` 一律不出取值区；`build_source_schema_text` 的分支表（doc 源命中→收窄+取值、未命中→全量**不带**取值、非 doc 源有界→带取值）。内存 SQLite + 替身，不连 Milvus / embedding / LLM |
| `test_text2sql_stream.py` | 流式 text2sql：校验失败重试、重试 prompt 回带上一次 SQL、`_response_to_text` 处理结构化 content |
| `test_answer_format.py` | `detect_answer_format` / `render_list_answer` / `parse_analysis` 的名称提取 |
| `test_schema_qa.py` | 表结构咨询的命中与误判防护 + 确定性兜底（含跨文档寻址被拒） |
| `test_memory_trim.py` | 会话记忆每轮结果的行数截断（Mongo 16MB 保护） |
| `test_context_governance.py` | 上下文治理：摘要失败不推进水位、折叠边界以服务端持久化消息数为准、知识库文件清单失效 |
| `test_milvus_timeout_kwarg.py` | Milvus 写入超时：首次写入不存在的集合必须成功（规避 langchain-milvus 0.4.0 的 `timeout` 关键字冲突）、`drop_old` 重建路径同样成立、构造级 timeout 必须为 `None`。替身客户端、**不写入任何真实集合**，但**需本机 Milvus 可达**（构造 store 时 `langchain_milvus` 走 pymilvus ORM `connections.connect()`，替身覆盖不到） |
| `test_chunking.py` | `chunk_text` 结构感知分块（段落 / 句子 / 超长回退 / overlap） |
| `test_sse_heartbeat.py` | SSE 保活与事件搬运（替身图 + 桩 `record_turn`/`attach_report`）：静默期按心跳下发 `: keepalive` 注释行（且**带空行**）、事件按序透传（`result` 先于 `report`）、副作用次序仍是「result 时 `record_turn` → report 时 `attach_report`」、四类异常映射出的 `error_code` 与改造前一致（前端「修复连接」入口的契约）、`result` 带 error 或无 `session_id` 时不落库、**生成器被提前关闭时图任务确实被 cancel**（否则 agent 在后台继续烧 LLM） |
| `test_agent_progress.py` | 分析 / 报告 agent 的流式进度（替身 agent）：入口文案、`正在调用 X…` / `正在并行调用 X、Y…` / `X 已完成`、最终文本 = 最后一条不带 `tool_calls` 的 AI 消息（结构化 content 块也能拼接）、**分片形状不符时不抛异常也不发事件**、同 id 消息只播报一次、`_MAX_PROGRESS` 上限、`stream_mode="updates"` 与 `recursion_limit` 确实传给 agent |
| `test_analysis.py` | 统计算子数值（聚合 / 相关 / 环比 / 占比 / **环比增长率与排名** / **高于整体平均** / **多条件交集** + 回归 / 预测 / 异常 / 聚类 / `load_table` 的非等值过滤）；**`load_table` 的 `filters` 多条件 AND**（含旧单条件签名向后兼容、非法列/op/形状显式报错）；**`group_cagr` 年均增长率**（已知数值、aagr、区间收窄、排名、`df_id` 可被 `intersect_groups` 消费，以及「**整数年份列**未被当成 epoch 纳秒」的回归）；**`intersect_groups` 的列对列比较**（两段区间互比、`_<df_id>` 后缀、非法引用报错）。含「上期为 0 / 零基数 / 期数不足时增长率必须是 `None` 而非 `inf`」的回归 |

另有 `batch_answer.py`（独立批量答题回填，仅依赖 `requests`）。
