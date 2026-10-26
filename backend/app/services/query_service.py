"""查询服务：自然语言 → SQL → 执行 → 结构化结果与图表类型。

不设规则兜底：任何异常都被捕获并转为带 error_code 的友好 QueryResponse，不抛 500。
"""
import datetime
import decimal
import logging
import math
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database.engine import (
    build_source_schema_text,
    get_dialect,
    get_engine,
)
from app.errors import AppError
from app.llm.text2sql import generate_sql
from app.schemas import QueryResponse
from app.services.chart_service import suggest_chart_type

logger = logging.getLogger(__name__)


def _to_json_safe(value: Any) -> Any:
    """把 DataFrame 单元格值统一转换为 JSON 安全类型。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, np.generic):  # numpy 标量 → Python 原生
        value = value.item()

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()

    return str(value)


def _build_explanation(row_count: int, include_sql: bool = False, truncated: bool = False) -> str:
    """规则生成结果说明（默认不提及 SQL，仅在要求展示 SQL 时说明）。"""
    if row_count == 0:
        return "未查到匹配数据，请调整提问。"
    tail = "" if not truncated else f"（结果超过 {settings.max_result_rows} 行，仅显示前 {row_count} 行，请缩小范围）"
    if include_sql:
        return f"已生成并执行 SQL，共返回 {row_count} 条结果。{tail}".rstrip()
    return f"共返回 {row_count} 条结果。{tail}".rstrip()


def _read_frame_limited(sql: str, engine: Engine, limit: int) -> tuple[pd.DataFrame, bool]:
    """执行查询并**限制读入行数**，返回 ``(df, 是否被截断)``。

    早前 ``pd.read_sql_query(sql, engine)`` 没有任何上限：一条大表全量查询会把整个
    结果读进内存，再经 SSE 单帧、Mongo 会话记忆与前端表格层层放大。
    用 ``chunksize`` 只取第一块即可判定是否超限，不必先把全表 materialize 出来。
    """
    chunks = pd.read_sql_query(sql, engine, chunksize=limit + 1)
    try:
        df = next(chunks, None)
    finally:
        chunks.close()  # 只取首块，及时释放游标
    if df is None:
        return pd.DataFrame(), False
    if len(df) > limit:
        return df.iloc[:limit], True
    return df, False


def _error_response(question: str, error: str, error_code: str) -> QueryResponse:
    """构造失败响应。"""
    return QueryResponse(
        question=question,
        sql=None,
        columns=[],
        rows=[],
        row_count=0,
        chart_type=None,
        explanation=None,
        error=error,
        error_code=error_code,
    )


def execute_sql(
    question: str,
    sql: str,
    engine: Engine,
    chart_hint: str | None = None,
    include_sql: bool = False,
) -> QueryResponse:
    """执行 SQL 并构建结构化结果（不含 SQL 生成，供流式与非流式共用）。

    ``include_sql`` 控制结果是否携带 SQL 语句：默认 False（仅当用户明确要求输出 SQL 时置 True）。
    """
    try:
        df, truncated = _read_frame_limited(sql, engine, settings.max_result_rows)
    except Exception:
        # 任何 DB 异常都归一成 SQL_ERROR（用户口径一致），但**必须留日志**：
        # 连接断开、权限不足、方言不兼容都会走到这里，无日志时全都长得一样。
        logger.warning("SQL 执行失败：%s", sql, exc_info=True)
        return _error_response(
            question,
            "生成的查询语句无法执行，请换一种方式描述问题。",
            "SQL_ERROR",
        )

    columns = [str(c) for c in df.columns]
    rows = [
        [_to_json_safe(v) for v in row]
        for row in df.itertuples(index=False, name=None)
    ]
    row_count = len(rows)
    chart_type = suggest_chart_type(columns, rows, question, chart_hint)
    explanation = _build_explanation(row_count, include_sql, truncated)

    return QueryResponse(
        question=question,
        sql=sql if include_sql else None,
        columns=columns,
        rows=rows,
        row_count=row_count,
        chart_type=chart_type,
        explanation=explanation,
        truncated=truncated,
        error=None,
        error_code=None,
    )


def run_query(
    question: str,
    data_source_id: str = "",
    chart_hint: str | None = None,
    include_sql: bool = False,
) -> QueryResponse:
    """执行一次自然语言查询，返回结构化结果与图表类型（默认不携带 SQL）。"""
    try:
        engine = get_engine(data_source_id)
        dialect = get_dialect(data_source_id)
        # 与图内 _prepare_query_node 共用同一口径：仅暴露本数据源的表（避免串表），
        # 且 doc 源先做块定位收窄、再把取值附进 schema 文本。早前这里直接调
        # build_scoped_schema_text，对 doc 源会把该文档全部 239 张表的 DDL 原样喂给模型
        # —— 既没有块定位，也没有任何取值，「猜字面量 → 0 行」的故障形态与主链路一致。
        # 纯同步上下文，直接调用即可（图内那条才需要 asyncio.to_thread）。
        schema_text = build_source_schema_text(engine, data_source_id, question)
        sql = generate_sql(question, schema_text, dialect)
        return execute_sql(question, sql, engine, chart_hint, include_sql=include_sql)
    except AppError as e:
        return _error_response(question, e.message, e.error_code)
    except SQLAlchemyError:
        # 外部数据源连接失败（如配置的 MySQL/PostgreSQL 不可达）
        return _error_response(
            question,
            "数据源连接失败，请检查数据源配置或网络后重试。",
            "DATASOURCE_UNAVAILABLE",
        )
    except ValueError as e:
        # 「未知/失效数据源」：码要与 /api/chat/stream 一致（DATASOURCE_UNAVAILABLE），
        # 前端「修复连接」入口只认这个码；给 SYSTEM_ERROR 会让补救按钮失灵。
        return _error_response(question, str(e), "DATASOURCE_UNAVAILABLE")
    except Exception:
        logger.exception("非流式查询失败。")
        return _error_response(question, "系统出现异常，请稍后重试。", "SYSTEM_ERROR")
