"""Pydantic 契约模型：前后端共享的数据结构。"""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class ColumnInfo(BaseModel):
    name: str
    type: str


class TableInfo(BaseModel):
    name: str
    columns: list[ColumnInfo] = Field(default_factory=list)


class RelationshipInfo(BaseModel):
    """表关联关系（显式外键或命名推断），供 /api/schema 暴露与多表 JOIN 参考。"""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    kind: Literal["fk", "inferred"] = "fk"


class DataSourceInfo(BaseModel):
    id: str
    name: str
    type: str
    tables: list[TableInfo] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)
    available: bool = True


class SchemaResponse(BaseModel):
    data_sources: list[DataSourceInfo] = Field(default_factory=list)


class ConnectionCreate(BaseModel):
    """新增/编辑外部数据库连接的表单输入。"""

    name: str
    type: Literal["mysql", "postgresql"]
    host: str
    port: int | None = None  # 缺省时按 type 补 3306/5432
    database: str
    user: str
    password: str = ""


class ConnectionInfo(BaseModel):
    """已保存连接元数据（不含 password，供列表/下拉展示）。"""

    id: str
    name: str
    type: str
    host: str
    port: int
    database: str
    user: str


class ConnectionTestResponse(BaseModel):
    """连接测试结果。"""

    ok: bool = True
    tables: list[TableInfo] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None


class ConnectionCreateResponse(BaseModel):
    """新增/编辑连接的结果（成功含实时表结构，失败含 error/error_code）。"""

    id: str = ""
    name: str = ""
    type: str = ""
    tables: list[TableInfo] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None


class QueryRequest(BaseModel):
    # 长度上限走 settings（超限由 FastAPI 直接返回 422），避免超长提问打爆上下文与内存
    question: str = Field(max_length=settings.max_question_chars)
    data_source_id: str = ""
    # 显式指定的图表类型（bar/line/pie）。早前这两个端点无从指定，chart_hint 恒为 None，
    # 于是「严格图表模式」下它们**永远只能出表格**；/api/chat/stream 则由意图分类自动识别。
    chart_hint: Literal["bar", "line", "pie"] | None = None
    # 显式指定**多种**图表类型（按指标列顺序对应），如 ["bar", "line"]。
    # 与 chart_hint 并存：新客户端优先传这个，旧的单值字段继续可用。
    chart_hints: list[Literal["bar", "line", "pie"]] | None = None


class ChatMessage(BaseModel):
    """多轮对话中的一条消息，用于把历史上下文传给后端。"""

    role: Literal["user", "assistant"]
    content: str

    @field_validator("content", mode="before")
    @classmethod
    def _truncate_content(cls, v):
        """超长正文**截断**而非拒绝：助手答案天然可能很长，硬 422 会让整段对话续不下去。"""
        if isinstance(v, str) and len(v) > settings.max_message_chars:
            return v[: settings.max_message_chars]
        return v


class ChatRequest(BaseModel):
    """统一对话请求：既可能是数据查询，也可能是闲聊，由后端意图判别分流。"""

    # question 是用户手输的单字段，超限直接 422（前端能明确提示）；上限见 settings
    question: str = Field(max_length=settings.max_question_chars)
    data_source_id: str = ""
    session_id: str | None = None
    history: list[ChatMessage] = Field(default_factory=list)

    @field_validator("history", mode="before")
    @classmethod
    def _keep_recent(cls, v):
        """只保留**最近** N 条：前端会把整段会话都发上来，长会话必然超限，
        拒绝会让对话彻底无法继续；保留最近若干条与 context_service 的近期窗口语义一致。
        """
        if isinstance(v, list) and len(v) > settings.max_history_messages:
            return v[-settings.max_history_messages:]
        return v


class SourceRef(BaseModel):
    """RAG 检索的出处引用：文档名 + 片段编号。"""

    title: str
    block_id: str


class ChartSeries(BaseModel):
    """单个数据系列的图表映射：列名 → 图形类型 → 左右轴。

    ``column`` 在宽表下是指标列名，在长表下是系列名（第 2 列的标签）。
    前端按 ``chart_series`` 为每个系列设置 ECharts 的 ``type`` / ``yAxisIndex``。
    """

    column: str
    type: Literal["bar", "line", "pie"] = "bar"
    axis: Literal["left", "right"] = "left"


class QueryResponse(BaseModel):
    question: str
    kind: Literal["query", "chat"] = "query"
    answer: str | None = None
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False  # 结果超过 settings.max_result_rows 被截断（explanation 中亦已说明）
    # 仅会话记忆回放时出现：落库前按 settings.memory_max_rows 截断，这里记原始行数。
    original_row_count: int | None = None
    # 主图表类型：取 chart_series[0].type，保留给旧客户端/旧会话做单图降级。
    chart_type: str | None = None
    # 多系列映射（有序，与指标/系列一一对应）。默认空列表保证旧 Mongo 会话可直接反序列化；
    # 前端没有该字段时按 chart_type 走旧的单类型渲染。
    chart_series: list[ChartSeries] = Field(default_factory=list)
    # 本轮 SQL 实际引用的表（文档源多表定位用：下一轮向量检索无可信命中时沿用它，
    # 避免同一会话里刚用过的表被判「未定位」而回退全量 schema）。旧会话无此字段。
    used_tables: list[str] = Field(default_factory=list)
    explanation: str | None = None
    analysis: str | None = None  # 分析说明（答案之外的自然语言分析，可选）
    error: str | None = None
    error_code: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)


class SessionMessage(BaseModel):
    """会话中的一条消息（用于历史会话恢复）。"""

    role: Literal["user", "assistant"]
    content: str = ""
    kind: Literal["query", "chat"] | None = None
    data_source_id: str | None = None
    sql: str | None = None
    result: QueryResponse | None = None
    report: dict | None = None
    sources: list[SourceRef] | None = None
    created_at: str | None = None


class SessionSummary(BaseModel):
    """会话摘要（历史会话列表项）。"""

    session_id: str
    title: str = ""
    last_data_source_id: str | None = None
    message_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class SessionDetail(BaseModel):
    """会话详情（含全部消息）。"""

    session_id: str
    last_data_source_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    messages: list[SessionMessage] = Field(default_factory=list)
