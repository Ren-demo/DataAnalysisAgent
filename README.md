# 智能数据分析系统

> 基于 2026年第二届重庆市AI大模型创新应用大赛
> 九天·梧桐赛道 赛题 1「智能数据分析系统」· 难度：初级
> AI 驱动的对话式数据分析工具：用自然语言提问，自动生成查询语句、执行分析、输出结构化结果与可视化图表。

## 一、项目简介

传统数据分析工具存在「门槛高、效率低」的痛点——用户需要掌握 SQL、操作繁琐、多数据源适配差、结果呈现零散。本项目基于大语言模型（LLM）构建一个**对话式数据分析工具**：用户用日常语言提问（如「统计各分类的数量」），系统自动完成「自然语言 → 意图分类 → SQL → 执行查询 → 结构化结果 / 可视化图表」全流程，显著降低数据使用门槛。

本项目已实现**基础查询、多数据源、可视化、多表关联查询（JOIN）、非结构化文档问答与决策报告**等能力，支持上传文件 / 外部数据库 / 文档多种数据源，端到端可运行。

## 二、功能特性

| 能力 | 说明 |
| --- | --- |
| 自然语言 → SQL | 通过 DeepSeek 大模型（多方言 few-shot）生成标准查询语句，并用 sqlglot 只读校验 |
| 查询执行 | 自动执行 SQL，返回结构化结果（列名 + 行数据 + 行数）；SQL 默认不展示，仅用户明确要求输出时展示 |
| 意图分类 | query/chat/redraw/retrieval/analysis/schema 六类分流：查询 / 闲聊·元数据咨询 / 图表重绘 / 文档问答 / 统计建模分析 / 表结构列名咨询 |
| 智能可视化 | 严格图表模式：仅当用户显式指定柱状/折线/饼图时才出图，否则 table |
| 友好错误处理 | 模型不可用（未配置 Key / 网络异常）时返回带 `error_code` 的友好提示，不伪造结果 |
| 多数据源适配 | SQLite 上传库 + Excel/CSV 上传 + 非结构化文档 + MySQL/PostgreSQL（静态 `datasources.json` + 前端运行时输入连接存 MongoDB `connections`） |
| 多表关联查询 | 显式外键 + 命名推断自动识别关联关系，注入 schema 提示供 JOIN |
| 非结构化文档问答 | .md/.txt/.pdf/.docx 解析，表格入 SQLite、文本向量化入 Milvus 检索回答 |
| 会话记忆 | MongoDB 记录「问题 + 回答 + 数据源」，历史会话侧栏，刷新保持当前对话 |
| 决策报告 | LangGraph + `create_agent` 多步下钻产出结构化报告；可选 web_search 联网搜索 + knowledge_search 本地知识库检索 |
| 自动数据建模（异常检测 / 数据挖掘 / 时序预测） | 线性回归、时序预测（线性/移动平均/复合增长率外推）、异常检测（z-score/IQR）、K-Means 聚类，numpy 手写零新依赖 |

## 三、技术栈总览

- **架构形态**：前后端分离（前端 SPA 独立构建 / 静态托管；后端提供 REST 接口）
- **前端**：Vue 3 + TypeScript + Vite + Element Plus + ECharts（vue-echarts）+ axios + marked/DOMPurify（Markdown 渲染）+ 亮/暗主题切换
- **后端**：Python 3.10+ / FastAPI + Uvicorn
- **Agent / 编排**：LangChain 1.2 create_agent 工具调用智能体 + LangGraph 1.1 StateGraph 编排（10 节点：classify/prepare_query/text2sql/execute/report/redraw/chat/retrieve/schema/analysis）；agent 边跑边发 `progress` 步骤事件，SSE 静默期下发 : keepalive 注释行保活
- **LLM**：DeepSeek（ deepseek-v4-flash），经 langchain-openai 的 ChatOpenAI 接入；外部 embedding（阿里云 DashScope text-embedding-v4`）
- **数据层**：SQLAlchemy 2.x（统一 SQLite / MySQL / PostgreSQL）+ pandas（Excel/CSV 处理）
- **会话记忆**：Motor（异步 MongoDB 驱动，localhost:27017，集合 sessions / connections）
- **向量检索**：Milvus（本地 docker，经 langchain-milvus）+ 外部 embedding（硬依赖，无 BM25）
- **SQL 校验**：sqlglot（只读守卫）

## 四、目录结构

```
E:/ChongqingAI/
├── README.md                 # 项目总览（本文件）
├── CLAUDE.md                 # 项目开发约定（需求/决策记录）
├── describe.md               # 赛题说明
├── docs/                     # 文档
│   ├── architecture.md       # 系统架构说明
│   ├── dependencies.md       # 环境依赖版本清单
│   └── sample_questions.md   # 样例提问（用于客观评分）
├── backend/                  # 后端（FastAPI）
│   ├── requirements.txt
│   ├── datasources.json      # 外部数据源配置（MySQL/PostgreSQL，当前为空）
│   ├── .env                  # 环境变量（DeepSeek / MongoDB / embedding / Milvus）
│   ├── data/                 # SQLite 数据文件（uploads.db / docs.db）+ knowledge/ 本地知识库
│   └── app/
│       ├── main.py           # 应用入口，挂载路由（prefix=/api）
│       ├── config.py         # 配置（Settings，加载 backend/.env）
│       ├── schemas.py        # Pydantic v2 数据模型（请求/响应）
│       ├── errors.py         # 统一业务异常（error_code + 友好中文提示）
│       ├── database/         # 数据层
│       │   ├── engine.py     # 多数据源路由 + 表结构读取 + 关联关系识别
│       │   ├── mongo.py      # MongoDB 连接（Motor）：sessions 会话记忆 + connections 连接
│       │   ├── milvus.py     # Milvus 向量库（langchain-milvus）
│       │   └── datasource_config.py  # 外部数据源配置（静态 + 运行时合并）
│       ├── llm/              # LLM 相关
│       │   ├── client.py     # get_llm()（ChatOpenAI 客户端，失败抛 AppError）
│       │   ├── embedding.py  # get_embeddings()（外部 embedding，向量检索硬依赖）
│       │   ├── intent.py     # 意图分类（query/chat/redraw/retrieval + chart_hint）
│       │   ├── text2sql.py   # 多方言 generate_sql()/generate_sql_stream()（few-shot + sqlglot + 重试）
│       │   ├── summary.py    # 滚动摘要生成（旧对话压缩）
│       │   └── title.py      # 会话标题生成（LLM 概括首条问题）
│       ├── agents/           # 编排 / 智能体
│       │   ├── graph.py      # LangGraph 编排图（9 节点）
│       │   ├── state.py      # 图状态（OrchestratorState）
│       │   ├── tools.py      # 报告 agent 工具（get_schema/generate_sql/execute_sql + web_search/knowledge_search）
│       │   ├── stat_tools.py # 统计建模算子（分组聚合/相关性/环比/年均增长率 CAGR/占比 + 回归/预测/异常/聚类，多条件过滤）
│       │   ├── analysis.py   # 分析结果模型 + 系统提示（注入 schema、要求一次性规划并行调用）+ 解析
│       │   └── report.py     # ReportResult 模型 + 报告解析
│       ├── services/         # 服务层
│       │   ├── chart_service.py   # 图表类型推断（严格模式）
│       │   ├── query_service.py   # 查询执行（execute_sql / run_query）
│       │   ├── chat_service.py    # 闲聊/元数据咨询 + 文档问答（retrieval_stream）
│       │   ├── context_service.py # 闲聊上下文治理（窗口截断 + 滚动摘要）
│       │   ├── datasource_service.py  # Excel/CSV 上传导入
│       │   ├── connection_service.py  # 外部数据库连接管理
│       │   ├── document_service.py    # 非结构化文档解析 + 向量检索
│       │   ├── chunking.py       # 文本分块
│       │   ├── knowledge_service.py  # 本地知识库检索
│       │   └── memory_service.py      # 会话记忆读写
│       └── routers/          # 路由
│           ├── meta.py       # GET /health、GET /schema
│           ├── chat.py       # POST /query、/query/stream、/chat/stream
│           ├── memory.py     # GET/DELETE /sessions、/sessions/{session_id}
│           ├── datasource.py # GET /datasources、POST /datasources/upload
│           └── connections.py # GET/POST /connections、/connections/test、/connections/{ds_id}
└── frontend/                 # 前端（Vue 3 + TS + Vite）
    ├── src/
    │   ├── main.ts
    │   ├── App.vue
    │   ├── types.ts          # 与后端契约镜像的 TypeScript 类型
    │   ├── api/              # axios + fetch SSE 封装（13 个函数）
    │   ├── composables/useDark.ts  # 主题切换
    │   ├── views/HomeView.vue
    │   ├── components/       # ChatInput / MessageList / SqlResult / ChartCard / ReportCard / MarkdownText / SessionSidebar / WelcomePanel / ConnectionDialog
    │   └── styles/           # 基础样式 + 设计 token
    └── vite.config.ts        # dev 端口 5173，代理 /api → http://localhost:8000
```

## 五、快速开始

### 1. 后端

```bash
cd E:/ChongqingAI/backend

# 创建并激活虚拟环境（Windows PowerShell）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（编辑 backend/.env）

# 确保 MongoDB 运行于 localhost:27017（会话记忆依赖，缺失时记忆功能静默跳过）
# 向量检索需本地 Milvus（docker，http://localhost:19530）与外部 embedding（DashScope），二者为硬依赖
# 启动服务
uvicorn app.main:app --reload --port 8000
```

### 2. 前端

```bash
cd E:/ChongqingAI/frontend

npm install
npm run dev    # 访问 http://localhost:5173
```

### 3. 环境变量

在后端根目录的 `backend/.env` 中配置（本项目已配置）：

```ini
# LLM
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
DEEPSEEK_MODEL=deepseek-v4-pro          # 低配可改为 deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 会话记忆（MongoDB）
MONGO_URI=mongodb://localhost:27017
MONGO_DB=chongqing_ai

# 向量检索（硬依赖：外部 embedding + 本地 Milvus）
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=sk-xxxxxxxxxxxxxxxx
EMBEDDING_MODEL=text-embedding-v4
MILVUS_URI=http://localhost:19530
```

- **配置了 `DEEPSEEK_API_KEY`**：走大模型 few-shot 生成 SQL（质量优先）。
- **未配置 `DEEPSEEK_API_KEY`**：查询接口会返回一条用户友好的系统错误提示（`error_code=NO_API_KEY`），**不会**伪造查询结果，请先配置后再使用。

### 4. 验证

```bash
curl http://localhost:8000/api/health        # {"status":"ok"}
curl http://localhost:8000/api/schema        # 返回数据源与表结构
curl http://localhost:8000/api/datasources   # 列出已上传 + 外部 + 文档数据源
curl http://localhost:8000/api/connections   # 列出已保存的外部数据库连接
curl http://localhost:8000/api/sessions      # 列出历史会话
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question":"统计各分类的数量","data_source_id":"upload:<表名>"}'
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question":"统计各分类的数量","data_source_id":"upload:<表名>","history":[]}'  # 前端主入口
```

## 六、Docker 部署

生产部署不需要 nginx：前端由 Node 直接伺服 `frontend/dist`，并把 `/api/**` 反向代理到后端容器，浏览器始终同源访问，前端代码无需改 API 地址，也不产生 CORS。

### 1. 架构

```text
浏览器
  └─ :80  frontend（node:22-alpine）
           ├─ 静态资源 dist/
           └─ /api/** ──> backend:8000（FastAPI）
                            ├─> mongodb:27017
                            └─> standalone:19530（Milvus）
```

- 只有 frontend 映射宿主机端口（默认 80）；后端与中间件只在 `app-net` 内部可达。
- 不使用 `vite preview`（开发预览服务器）。生产静态服务是零第三方依赖的 `frontend/server.mjs`，同时处理 SSE 透传、gzip、缓存头、SPA 回退与客户端断开清理。
- `backend/.env` 仅用于本地开发；容器里的配置全部由 compose 从根目录 `.env` 注入。

### 2. 前置条件

- Docker Engine 24+、Docker Compose v2
- 内存 ≥ 4 GB（建议 8 GB；Milvus standalone + MongoDB + Python 依赖较吃内存）
- 磁盘 ≥ 20 GB（后端镜像与 Milvus 数据）
- 服务器能出网访问 DeepSeek 与 DashScope embedding
- 默认按构建机架构构建；ARM 服务器需用 `docker buildx --platform linux/arm64`

### 3. 启动

```bash
# 1) 上传/克隆项目到服务器
git clone <repo> && cd ChongqingAI

# 2) 准备环境变量
cp .env.example .env
vi .env      # 填 DEEPSEEK_API_KEY、EMBEDDING_API_KEY，改 MONGO_ROOT_PASSWORD

# 3) 构建并启动（首次 5~10 分钟）
docker compose up -d --build

# 4) 查看状态（等 milvus-standalone 变为 healthy）
docker compose ps
docker compose logs -f backend
```

访问 `http://服务器IP`（`FRONTEND_PORT` 非 80 时带端口）。

### 4. 验证

```bash
curl http://服务器IP/healthz        # ok（前端容器自身）
curl http://服务器IP/api/health     # {"status":"ok"}（反代到后端）
```

浏览器走一遍：上传 CSV → 提问 → 结果表/图表 → 决策报告；再测一次文档上传问答（依赖 embedding 与 Milvus）。

### 5. 运维

```bash
docker compose ps
docker compose logs -f --tail=200 backend
docker compose restart backend
docker compose down           # 停止并删除容器（保留数据卷与 ./data）
docker compose up -d --build  # 代码更新后重建
```

### 6. 数据持久化与备份

| 数据 | 位置 |
| --- | --- |
| SQLite（uploads.db / docs.db）、上传文件、知识库语料 | 命名卷 `chongqing-ai_backend_data`（容器内 `/app/data`） |
| MongoDB | `./data/mongodb` |
| Milvus（etcd / MinIO / 数据） | `./data/milvus/...` |

```bash
# 备份后端数据卷
docker run --rm -v chongqing-ai_backend_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/backend-data-$(date +%F).tgz -C /data .
```

> 知识库语料放在容器的 `/app/data/knowledge`；本地已有语料时可用 `docker cp` 拷入。

### 7. 安全与网络

- 安全组/防火墙只放行 `80`（启用 HTTPS 时再加 443）。
- 中间件的宿主机端口在 compose 里默认是注释状态；需要 Attu / MinIO 控制台时再打开，并限制来源 IP。
- `MONGO_ROOT_PASSWORD` 必须修改；密码建议只用字母数字（它会被拼进 `mongodb://` URI，含 `@ : / # ? $` 时需要 URL 编码）。
- `.env` 不要提交，镜像构建时也已通过 `.dockerignore` 排除。
- 无 nginx 时的 HTTPS：可用云厂商负载均衡/证书，或在 Node 前面加一层 Caddy/Traefik；当前 compose 默认 HTTP。

### 8. 常见问题

| 现象 | 处理 |
| --- | --- |
| `backend` 一直 unhealthy | `docker compose logs backend`；确认 `.env` 中 `MONGO_ROOT_PASSWORD` 与 Mongo 初始化时一致 |
| 文档问答报 embedding / Milvus 错误 | 确认服务器能访问 DashScope；`docker compose ps` 看 Milvus 是否 healthy |
| 首次启动慢 | Milvus 冷启动需要 1~2 分钟，compose 已把 `start_period` 设为 90s |
| 80 端口被占用 | 改 `.env` 的 `FRONTEND_PORT=3000` |
| 需要看 Swagger `/docs` | 临时给 `backend` 加 `ports: ["8000:8000"]`，用完删除 |
| MongoDB 认证失败（Authentication failed） | 若 `./data/mongodb` 已用旧密码初始化，容器不会重新创建 root 用户：备份后清空该目录再启动，或改用原密码 |
| 容器无法写入 `./data`（RHEL / CentOS） | 启用 SELinux 时给 bind mount 加 `:Z`，例如 `./data/mongodb:/data/db:Z` |
| 镜像拉取慢/失败 | 配置 Docker 镜像加速；或本地 `docker save` 后 `docker load` 到服务器 |
| ARM 服务器构建失败 | 使用 `docker buildx build --platform linux/arm64`，或换用支持多架构的镜像源 |

## 七、功能清单

* 前后端分离项目骨架（FastAPI + Vue 3）
* 会话记忆（MongoDB，记录问题/回答/数据源；历史会话侧栏；刷新保持当前对话）
* 表结构自动读取（`GET /api/schema`）
* 自然语言 → SQL（DeepSeek 多方言 few-shot + sqlglot 校验 + 失败重试）
* 意图分类（query/chat/redraw/retrieval 四类分流）
* 统一错误处理（模型不可用时返回友好提示，不设规则兜底）
* 查询执行与结构化结果（POST /api/query）
* 图表类型推断（严格模式：bar / line / pie / table）
* 数据源上传（Excel/CSV → SQLite，POST /api/datasources/upload）
* 多数据源路由（SQLite + 上传 + MySQL/PostgreSQL + 文档数据源）
* 外部数据库连接管理（/api/connections，前端运行时输入并持久化到 MongoDB）
* 多表关联查询（显式外键 + 命名推断，注入 JOIN 提示）
* 非结构化文档问答（.md/.txt/.pdf/.docx 解析 + Milvus 向量检索）
* 决策报告（LangGraph + `create_agent` 下钻推理 + 可选联网/知识检索）
* 自动数据建模（线性回归 / 时序预测 / 异常检测 / K-Means 聚类，numpy 手写）
* SSE 流式对话输出（POST /api/query/stream、POST /api/chat/stream）
* 前端查询面板 + 结果表格 + ECharts 图表渲染 + 决策报告卡片 + 错误通知 + Markdown 回答 + 会话侧栏 + 主题切换

## 八、相关文档

- [系统架构说明](./docs/architecture.md)
- [环境依赖版本清单](./docs/dependencies.md)
- [样例提问（客观评分用）](./docs/sample_questions.md)
