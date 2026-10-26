# 环境依赖版本清单

> 本文档列出项目运行所需的环境与第三方依赖版本约束，供安装部署与「环境配置说明」参考。
> 截至 2026-08-22 依赖已在本机安装：后端使用 conda 环境 `ChongqingAIApplication`（Python 3.13），前端 `node_modules` 已就绪。以下为版本约束（约束语义：`>=` 最低版本、`^`/`~` npm 兼容范围）。

## 一、运行时环境

| 组件 | 版本要求 | 本机实测 |
| --- | --- | --- |
| Python | 3.10+ | 3.13.9 |
| Node.js | 18+（推荐 20+） | v24.14.0 |
| npm | 9+ | 11.9.0 |
| 操作系统 | Windows / macOS / Linux | Windows 11 |

## 二、后端依赖（backend/requirements.txt）

| 包名 | 版本约束 | 用途 |
| --- | --- | --- |
| fastapi | >=0.115 | Web 框架（REST 接口） |
| python-multipart | >=0.0.9 | 文件上传（`POST /api/datasources/upload`） |
| uvicorn[standard] | >=0.30 | ASGI 服务器 |
| sqlalchemy | >=2.0.30 | ORM / 数据层（统一 SQLite/MySQL/PostgreSQL） |
| pandas | >=2.2 | 查询执行与结果处理（Excel/CSV 上传） |
| numpy | >=1.26 | pandas 底层数值运算 |
| openpyxl | >=3.1 | Excel（.xlsx）读取 |
| pydantic | >=2.7 | 数据校验 / 契约模型 |
| pydantic-settings | >=2.3 | 读取环境变量与 .env |
| langchain | ==1.2.12 | LangChain 顶层包（`create_agent` 工具调用智能体） |
| langgraph | ==1.1.2 | LangGraph 编排框架（`StateGraph`） |
| langgraph-checkpoint | ==4.0.1 | LangGraph 检查点（配合 langgraph 1.1 固定版本） |
| langgraph-prebuilt | ==1.0.8 | LangGraph 预构建组件（`create_agent` 依赖） |
| langgraph-sdk | ==0.3.9 | LangGraph SDK（配合固定版本） |
| langchain-openai | >=0.2 | LangChain 的 OpenAI 兼容 LLM 客户端（接入 DeepSeek） |
| sqlglot | >=26.0 | SQL 语法校验（多方言） |
| python-dotenv | >=1.0 | .env 加载 |
| pymysql | >=1.1 | MySQL 驱动（外部数据源） |
| psycopg2-binary | >=2.9.9 | PostgreSQL 驱动（外部数据源） |
| motor | >=3.6.0 | 异步 MongoDB 驱动（会话记忆） |
| python-docx | >=1.1.0 | 可选：.docx 文档解析 |
| pypdf | >=4.0.0 | 可选：.pdf 文档解析 |
| tavily-python | >=0.5.0 | 可选：Tavily 联网搜索（需配置 key；默认走免 key DuckDuckGo） |
| pymilvus | ==3.0.1 | Milvus 向量库客户端（向量检索） |
| langchain-milvus | ==0.4.0 | LangChain 的 Milvus vector store 封装（文档问答 / 知识库检索） |

> **版本固定说明**：`langgraph-checkpoint` / `langgraph-prebuilt` / `langgraph-sdk` 三项必须固定为上述版本——否则 pip 会解析到 checkpoint 4.2.0 / prebuilt 1.0.13，与 `langgraph 1.1.2` 运行时不一致，导致 `create_agent` 导入失败（`ImportError: cannot import name 'ExecutionInfo'`）。`langchain 1.2.12` 声明的 `langchain-core<2.0.0,>=1.2.10` 被现有 1.5.5 满足，未触发降级。

安装命令（已安装于 conda 环境 `ChongqingAIApplication`；从零安装）：

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate   |  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## 三、前端依赖（frontend/package.json）

### dependencies（运行时）

| 包名 | 版本约束 | 用途 |
| --- | --- | --- |
| vue | ^3.5.13 | 前端框架 |
| element-plus | ^2.9.1 | UI 组件库 |
| @element-plus/icons-vue | ^2.3.2 | Element Plus 图标 |
| echarts | ^5.6.0 | 图表渲染引擎 |
| vue-echarts | ^7.0.3 | ECharts 的 Vue 3 封装 |
| axios | ^1.7.9 | HTTP 客户端 |
| marked | ^15.0.6 | Markdown 解析（答案渲染） |
| dompurify | ^3.2.4 | HTML 清洗（防 XSS） |

### devDependencies（构建/开发）

| 包名 | 版本约束 | 用途 |
| --- | --- | --- |
| vite | ^6.0.7 | 构建工具 / 开发服务器 |
| @vitejs/plugin-vue | ^5.2.1 | Vite 的 Vue 插件 |
| typescript | ~5.6.3 | 类型检查 |
| vue-tsc | ^2.2.0 | Vue SFC 类型检查（`npm run build` 用） |

安装命令（`node_modules` 已就绪；从零安装）：

```bash
cd frontend
npm install
npm run build   # vue-tsc 类型检查 + vite 打包
```

## 四、环境变量（backend/.env）

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是 | （空） | DeepSeek API Key；未配置时查询接口返回 `NO_API_KEY` 友好错误（本项目已配置） |
| `DEEPSEEK_MODEL` | 否 | `deepseek-v4-pro` | 模型 ID（低配 `deepseek-v4-flash`） |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `MONGO_URI` | 否 | `mongodb://localhost:27017` | MongoDB 连接串（会话记忆）；若开启认证需带凭据，如 `mongodb://<user>:<pass>@localhost:27017/?authSource=admin` |
| `MONGO_DB` | 否 | `chongqing_ai` | MongoDB 库名（会话记忆） |
| `EMBEDDING_BASE_URL` | 是 | （空） | 外部 embedding 端点（本项目已配 DashScope `https://dashscope.aliyuncs.com/compatible-mode/v1`） |
| `EMBEDDING_API_KEY` | 是 | （空） | 外部 embedding API Key |
| `EMBEDDING_MODEL` | 是 | （空） | embedding 模型（本项目已配 `text-embedding-v4`） |
| `MILVUS_URI` | 否 | `http://localhost:19530` | Milvus 地址（向量检索，默认本地 gRPC） |
| `MILVUS_DOC_COLLECTION` | 否 | `doc_chunks` | 文档分块向量集合名 |
| `MILVUS_KNOWLEDGE_COLLECTION` | 否 | `knowledge_chunks` | 知识库向量集合名 |
| `CHAT_HISTORY_MAX_MESSAGES` | 否 | `20` | 闲聊近期窗口保留历史条数 |
| `CHAT_HISTORY_MAX_TOKENS` | 否 | `2400` | 闲聊近期窗口 token 预算 |
| `CHAT_SUMMARY_MAX_CHARS` | 否 | `300` | 滚动摘要长度上限（字数） |
| `DOC_CHUNK_SIZE` | 否 | `500` | 文档分块长度（字符） |
| `DOC_CHUNK_OVERLAP` | 否 | `100` | 文档分块重叠长度 |
| `DOC_TOP_K` | 否 | `5` | 检索返回 top-k 片段数 |
| `WEB_SEARCH_PROVIDER` | 否 | `duckduckgo` | 联网搜索提供商（none/duckduckgo/tavily） |
| `TAVILY_API_KEY` | 否 | （空） | Tavily 联网搜索 key（provider=tavily 时需配） |
| `WEB_SEARCH_MAX_RESULTS` | 否 | `5` | 联网搜索最大结果数 |
| `KNOWLEDGE_BASE_ENABLED` | 否 | `true` | 是否启用本地知识库检索 |

## 五、数据依赖

- 会话记忆存储于 MongoDB（`localhost:27017`，库 `chongqing_ai`，集合 `sessions`），需本机运行 `mongod`；缺失时记忆功能静默跳过，不影响主查询链路。
- 上传数据源导入 `backend/data/uploads.db`（Excel/CSV 上传后自动生成）。
- 非结构化文档导入 `backend/data/docs.db`（表格入 SQLite、文本分块向量化入 Milvus，`doc:<id>` 数据源）。
- 决策报告本地知识库目录 `backend/data/knowledge/`（放 .md/.txt，供 knowledge_search 检索，可选）。
- 向量检索（文档问答 + 知识库检索）依赖本地 Milvus（`http://localhost:19530`）与外部 embedding（DashScope），二者为**硬依赖**，未配置/不可用时检索会报错。
- 外部数据源连接信息配置于 `backend/datasources.json`（当前为空列表）。
