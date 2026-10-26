
"""应用配置：通过 pydantic-settings 读取环境变量与 backend/.env。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend 根目录（本文件位于 backend/app/config.py，向上两级）
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """全局配置。"""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DeepSeek 大模型
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_base_url: str = "https://api.deepseek.com"
    # 单次调用超时与重试：SDK 默认 600s × 2 次重试，单次最长可挂 30 分钟
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 1

    # MongoDB 会话记忆
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "chongqing_ai"

    # 长对话记忆治理（闲聊链路）：近期窗口截断 + 旧对话滚动摘要
    chat_history_max_messages: int = 20    # 近期窗口保留的历史条数（同时是摘要边界）
    chat_history_max_tokens: int = 2400    # 近期窗口的 token 预算
    chat_summary_max_chars: int = 300      # 滚动摘要长度上限（提示词约束，约等于字数）

    # 非结构化文档数据源（文档问答 / RAG-lite）
    doc_chunk_size: int = 1500      # 文本分块长度（字符）
    doc_chunk_overlap: int = 100   # 分块重叠长度
    doc_top_k: int = 5             # 检索返回的 top-k 片段数

    # 文档表格定位（把「文档里的几十上百张表」收窄成问句真正指向的那几张）
    # Milvus 集合 metric 是 **L2 距离：越小越相似**，故下面都是**上界**。
    # 默认值由 2026-09 用 text-embedding-v4 + 赛题 Multi-step 数据实测标定：
    # 真命中 0.87 / 语义无关 0.98~1.03 / 完全无关 1.43。**换 EMBEDDING_MODEL 必须重标**，
    # 否则要么把无关表当候选（阈值过松），要么一张都定位不到（阈值过紧）。
    doc_table_max_distance: float = 0.95   # 超过即判「没定位到」，交由调用方回退全量 schema
    doc_table_score_margin: float = 0.10   # 只保留「不差于最优 margin」的命中

    # schema 文本附带的**取值区**（供 text2sql 写出正确的过滤字面量）
    # 背景：文档抽出的表常是「行标签 + 数值」的透视表，语义全在某一列的**取值**里
    # （如 col = 'less : amount representing interest' → 73512）。早前 schema 文本
    # **只有 DDL、零取值**，模型没见过任何字面量，只能按问题字面猜一个不存在的值，
    # 必然 0 行。取值区就是把这个信息补上。以下都是**资源保护**（衡量 prompt 体积），
    # 不是权限限制，嵌入 settings 便于按需放宽。
    schema_sample_rows: int = 20           # 大表只给前 N 行样本
    schema_full_dump_rows: int = 50        # 行数 ≤ N 的小表直接给全部行
    schema_max_distinct_values: int = 30   # 文本列去重取值上限（去重值更多的列不给）
    schema_max_chars: int = 60000          # 取值区总字符预算，超出即停止追加
    schema_max_tables: int = 5             # 表数超过它就不附取值（回退全量属此列）

    # 决策报告增强：联网搜索 + 本地知识库检索（均可选，静默降级）
    # 默认 "none"：联网搜索会把用户提问原样发往第三方搜索服务，属于数据外发，
    # 需显式开启（.env 里设 WEB_SEARCH_PROVIDER=duckduckgo / tavily）。
    web_search_provider: str = "none"  # "none" | "duckduckgo" | "tavily"
    tavily_api_key: str = ""
    web_search_max_results: int = 5
    knowledge_base_enabled: bool = True
    # 是否在数据查询后追加决策报告。默认开启（赛题可选能力）。
    # 报告链路在同一次 SSE 响应内**串行**于主结果之后，且报告 agent 会再跑一轮
    # SQL 生成/执行，单次查询因此多付出若干次 LLM 往返；压延迟或成本时可关掉。
    report_enabled: bool = True

    # 资源上限（防止超大上传/超长请求体打爆内存与上下文）
    max_upload_mb: int = 20          # 单个上传文件大小上限（Excel/CSV/文档共用）
    max_question_chars: int = 2000   # 单条提问长度上限
    max_history_messages: int = 200  # 请求历史保留的最近条数（超出丢弃最早的，不报错）
    max_message_chars: int = 20000   # 单条历史消息长度上限（助手答案可能较长，故放宽）
    max_result_rows: int = 5000      # 查询结果行数上限（超出截断，见 query_service）
    memory_max_rows: int = 200       # 会话记忆里每轮结果**持久化**的行数上限（Mongo 单文档 16MB 保护）

    # SSE 心跳：图内节点（分析 / 决策报告 agent）会连续跑十几轮 LLM 往返且中途
    # **一个事件都不下发**，前端空闲超时（600s）会把这类健康的长任务单方面掐断。
    # 静默超过该秒数即下发一行 SSE 注释（``: keepalive``）——注释行同样能让
    # ``reader.read()`` 返回、重置前端空闲计时器，而既有消费端（前端 consumeSSE、
    # batch_answer.py）都会跳过它，无需改动。取值远小于前端空闲阈值即可。
    sse_heartbeat_seconds: int = 10

    # 分析 / 决策报告子 agent 的**步数**上限（super-step，约等于 2× 工具轮次）。
    # 这是**兜底**而非时间预算：心跳保活之后，模型一旦陷入「参数错→重试→参数错」的
    # 死循环会**无限期**烧 token，且用户永远停在「正在调用 X…」。默认 40 步对本类
    # 问题（3 轮左右）有 6 倍余量，正常链路绝不会提前结束。
    # ⚠ langgraph 1.1.2 的默认值是 10000（等于不设限），所以这里是在**收紧**。
    # 撞上后 analysis 静默回退 text2sql、report 静默跳过（与既有降级姿态一致）。
    agent_recursion_limit: int = 40

    # 数据目录（上传数据源 SQLite 库所在位置）
    data_dir: Path = BACKEND_DIR / "data"

    # Milvus 向量库（本地 docker 部署；向量检索必需）
    milvus_uri: str = "http://localhost:19530"
    milvus_doc_collection: str = "doc_chunks"
    milvus_knowledge_collection: str = "knowledge_chunks"
    milvus_timeout_seconds: int = 30  # 单次检索/写入超时（Milvus 不可达时不要无限等）

    # 外部 embedding API（OpenAI 兼容端点，阿里云 DashScope；向量检索必需）
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_timeout_seconds: int = 30

    @property
    def uploads_dir(self) -> Path:
        """上传文件暂存目录。"""
        return self.data_dir / "uploads"

    @property
    def uploads_db_path(self) -> Path:
        """上传数据源（Excel/CSV）导入的 SQLite 库路径。"""
        return self.data_dir / "uploads.db"

    @property
    def uploads_db_url(self) -> str:
        """上传数据源库的连接串（SQLite）。"""
        return f"sqlite:///{self.uploads_db_path.as_posix()}"

    @property
    def docs_dir(self) -> Path:
        """上传文档（.md/.txt/.pdf/.docx）暂存目录。"""
        return self.data_dir / "docs"

    @property
    def docs_db_path(self) -> Path:
        """文档解析后导入的 SQLite 库路径。"""
        return self.data_dir / "docs.db"

    @property
    def docs_db_url(self) -> str:
        """文档库的连接串（SQLite）。"""
        return f"sqlite:///{self.docs_db_path.as_posix()}"

    @property
    def knowledge_base_dir(self) -> Path:
        """本地知识库目录（决策报告检索）。"""
        return self.data_dir / "knowledge"


settings = Settings()
