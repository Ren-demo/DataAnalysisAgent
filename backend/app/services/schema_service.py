"""schema 咨询服务：对表结构/列名/字段类问题做确定性回答（不经过 LLM 自由生成）。

用 SQLAlchemy 反射按真实列序读取列名、或读行数，直接拼接中文答案，
避免闲聊链路因 schema 文本歧义 / 高温度而编造列名（如把「第1列」猜成 subject）。
"""
import logging

from sqlalchemy import Engine, inspect, text

from app.llm.intent import SchemaQuestion

logger = logging.getLogger(__name__)


def _columns(engine: Engine, table_name: str) -> list[str]:
    """返回表的保序列名列表（SQLite cid 顺序 / 各库声明顺序）。

    doc 表若记录了清洗前的原始表头（如数字表头 ``0/1`` 被清洗成 ``t_0/t_1``），
    用原始名作答，使「哪一列叫 0」等问句能命中真实表头；其余表仍返回物理列名。
    """
    cols = [col["name"] for col in inspect(engine).get_columns(table_name)]
    if table_name.startswith("doc_"):
        from app.services.document_service import get_doc_table_original_columns

        orig = get_doc_table_original_columns(table_name)
        if orig and len(orig) == len(cols):
            return orig
    return cols


def _row_count(engine: Engine, table_name: str) -> int:
    """按方言正确引用表名后读取行数。"""
    quoted = engine.dialect.identifier_preparer.quote(table_name)
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar() or 0)


def _sample_rows(engine: Engine, table_name: str, limit: int) -> list[list]:
    """读取表的前 ``limit`` 行（按列序），供 schema 总览让模型理解列语义。"""
    quoted = engine.dialect.identifier_preparer.quote(table_name)
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT * FROM {quoted} LIMIT {limit}")).fetchall()
    return [list(row) for row in rows]


def build_schema_overview(
    engine: Engine,
    table_names: list[str],
    sample_rows: int = 3,
    max_tables: int = 50,
) -> str:
    """构建「全局 schema 总览」文本：逐表列出**带序号的列** + 样本行。

    供 LLM 回答表结构/列名/字段/列序/行数咨询时使用——模型据列序与样本语义即可把
    中文列名（谓词→predicate、主题→subject、日期→date 等）映射到真实英文字段，无需硬编码词典。

    - 单表/块寻址（``doc:<id>:<table>``）→ 全量总览；
    - 多表（如整篇文档提取出的几百张表）→ 仅展示前 ``max_tables`` 张并标注总数，避免超大 prompt。
    """
    ordered = sorted(table_names)  # doc_<id>_<i> 排序后 idx 自然有序
    total = len(ordered)
    shown = ordered[:max_tables]

    lines: list[str] = [f"数据源共 {total} 张表。"]
    if total > len(shown):
        lines.append(f"（仅展示前 {len(shown)} 张表的结构，其余请按具体表名/块定位。）")

    for t in shown:
        try:
            cols = _columns(engine, t)
            col_text = "  ".join(f"{i}. {c}" for i, c in enumerate(cols, start=1))
            lines.append(f"\n表 {t}（共 {len(cols)} 列）：\n  {col_text}")
            try:
                rows = _sample_rows(engine, t, sample_rows)
            except Exception:
                rows = []
            if rows:
                lines.append("  样本行：")
                for row in rows:
                    cells = ["" if v is None else str(v) for v in row]
                    cells = [(c[:60] + "…") if len(c) > 60 else c for c in cells]
                    lines.append("    " + " | ".join(cells))
        except Exception:
            continue

    return "\n".join(lines)


def answer_schema_question(sq: SchemaQuestion, engine: Engine, table_name: str) -> str:
    """按 SchemaQuestion 确定性回答，返回中文答案字符串。读取失败抛异常，由调用方降级。"""
    cols = _columns(engine, table_name)
    n_cols = len(cols)

    if sq.kind == "nth_column":
        n = sq.n or 1
        if 1 <= n <= n_cols:
            return f"第 {n} 列的列名是 {cols[n - 1]}。"
        return f"该表只有 {n_cols} 列。"

    if sq.kind == "column_by_name":
        name = (sq.name or "").strip()
        target = name.lower()
        for i, c in enumerate(cols):
            if c.strip().lower() == target:
                return f"{name} 是第 {i + 1} 列。"
        return f"该表没有名为 {name} 的列。"

    if sq.kind == "list_columns":
        return f"该表共有 {n_cols} 列：{'、'.join(cols)}。"

    if sq.kind == "column_count":
        return f"该表共有 {n_cols} 列。"

    if sq.kind == "row_count":
        return f"该表共有 {_row_count(engine, table_name)} 行。"

    return f"该表共有 {n_cols} 列：{'、'.join(cols)}。"


def answer_schema_question_multi(sq: SchemaQuestion, engine: Engine, table_names: list[str]) -> str:
    """多表文档：逐表确定性回答并汇总（带序号与表名标注），不让用户手动选表。

    表名 ``doc_<doc_id>_<idx>`` 排序后 idx 自然有序；任一表读取失败跳过，
    全部失败时返回友好提示。
    """
    ordered = sorted(table_names)
    lines: list[str] = []
    for i, t in enumerate(ordered, start=1):
        try:
            ans = answer_schema_question(sq, engine, t)
        except Exception:
            logger.warning("读取表 %s 的结构失败，已跳过该表。", t, exc_info=True)
            continue
        lines.append(f"第 {i} 张表（{t}）：{ans}")
    if not lines:
        return "未能读取任何表格的结构信息。"
    return f"该文档共提取出 {len(ordered)} 张表：\n" + "\n".join(lines)
