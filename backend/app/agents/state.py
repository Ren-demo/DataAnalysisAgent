"""编排图状态定义。"""
from typing import TypedDict

from sqlalchemy import Engine

from app.agents.report import ReportResult
from app.llm.intent import SchemaQuestion
from app.schemas import ChatMessage, QueryResponse


class OrchestratorState(TypedDict, total=False):
    """LangGraph 编排图的状态字段（按需写入，故 total=False）。"""

    question: str
    data_source_id: str
    history: list[ChatMessage]
    intent: str                 # "query" | "chat" | "redraw" | "retrieval" | "analysis" | "schema"
    data_source_type: str | None  # "upload" | "doc" | "mysql" | "postgresql"
    chart_hint: str | None      # "bar" | "line" | "pie" | None
    want_sql: bool              # 用户是否明确要求输出/展示 SQL 语句
    answer_format: str | None   # "list" | "json" | "single" | None，用户要求/隐含的输出格式
    session_id: str | None      # 会话 id（重绘图表复用记忆数据用）
    schema_question: SchemaQuestion | None  # 表结构/列名咨询（确定性回答）
    table_names: list[str]      # 已解析的目标表名
    schema_text: str | None
    dialect: str
    engine: Engine
    sql: str | None
    result: QueryResponse | None
    report: ReportResult | None
