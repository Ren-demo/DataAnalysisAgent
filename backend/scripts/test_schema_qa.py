"""schema 咨询（表结构/列名）确定性回答离线自检：不依赖 LLM。

覆盖 ``app/llm/intent.py`` 的 ``detect_schema_question`` 与
``app/services/schema_service.py`` 的 ``answer_schema_question``，验证：
第 N 列、哪一列叫 X、列名/字段/表结构、列数、行数，以及误判防护。

运行：``cd backend && python scripts/test_schema_qa.py``
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import create_engine, text

from app.database.engine import _resolve_doc_table
from app.llm.intent import SchemaQuestion, detect_schema_question
from app.services.schema_service import answer_schema_question


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _build_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE doc_x_0 ("date" TEXT, "OT" REAL)'))
        conn.execute(text("INSERT INTO doc_x_0 VALUES ('1979-12-31', 1071.1), ('1980-12-31', 1188.2)"))
    return engine


def main() -> None:
    # 1) detect_schema_question：命中
    _check("第1列（阿拉伯）", detect_schema_question("表格中第1列的列名是什么？") == SchemaQuestion(kind="nth_column", n=1))
    _check("第2列", detect_schema_question("表中第2列的列名是什么？") == SchemaQuestion(kind="nth_column", n=2))
    _check("第一列（中文）", detect_schema_question("表中第一列的列名是什么？") == SchemaQuestion(kind="nth_column", n=1))
    _check("第十一列（中文）", detect_schema_question("表中第十一列的列名是什么？") == SchemaQuestion(kind="nth_column", n=11))
    _check("哪一列被称为X（引号）", detect_schema_question('在表格中，哪一列被称为"subject"？') == SchemaQuestion(kind="column_by_name", name="subject"))
    _check("哪一列的名称为X", detect_schema_question('在该表中，哪一列的名称为"subject"？') == SchemaQuestion(kind="column_by_name", name="subject"))
    # 前置语境「第一行和第一列标记数据编码的起点」不应干扰真实问句（回归赛题误判）
    _check("前置语境不误判（哪一列被称为谓词）", detect_schema_question('第一行和第一列标记数据编码的起点。表格中哪一列被称为“谓词”？') == SchemaQuestion(kind="column_by_name", name="谓词"))
    _check("前置语境不干扰第N列", detect_schema_question('数据从第一行和第一列开始编码。表中第2列的列名是什么？') == SchemaQuestion(kind="nth_column", n=2))
    _check("前置语境不干扰行数", detect_schema_question('第一行和第一列标记数据编码的起点。表格中有多少行？') == SchemaQuestion(kind="row_count"))
    _check("反向句式 名为X的列", detect_schema_question('表格中名为“国籍”的列是第几列？') == SchemaQuestion(kind="column_by_name", name="国籍"))
    _check("列名", detect_schema_question("这个表有哪些列名？").kind == "list_columns")
    _check("表结构", detect_schema_question("这个表的表结构是什么？").kind == "list_columns")
    _check("有几列", detect_schema_question("这个表有几列？") == SchemaQuestion(kind="column_count"))
    _check("有多少行", detect_schema_question("这个表有多少行？") == SchemaQuestion(kind="row_count"))

    # 2) detect_schema_question：误判防护（不命中，返回 None）
    _check("统计各列的总和（不误判）", detect_schema_question("统计各列的总和") is None)
    _check("销量第一的产品（不误判）", detect_schema_question("销量第一的产品是哪个") is None)
    _check("各分类的数量（不误判）", detect_schema_question("各分类的数量是多少") is None)

    # 3) answer_schema_question：确定性回答（真实列序 date, OT）
    engine = _build_engine()
    _check("第1列 → date", answer_schema_question(SchemaQuestion(kind="nth_column", n=1), engine, "doc_x_0") == "第 1 列的列名是 date。")
    _check("第2列 → OT", answer_schema_question(SchemaQuestion(kind="nth_column", n=2), engine, "doc_x_0") == "第 2 列的列名是 OT。")
    _check("越界列", answer_schema_question(SchemaQuestion(kind="nth_column", n=3), engine, "doc_x_0") == "该表只有 2 列。")
    _check("列名定位 date", answer_schema_question(SchemaQuestion(kind="column_by_name", name="date"), engine, "doc_x_0") == "date 是第 1 列。")
    _check("列名定位缺失", answer_schema_question(SchemaQuestion(kind="column_by_name", name="subject"), engine, "doc_x_0") == "该表没有名为 subject 的列。")
    _check("列出列", answer_schema_question(SchemaQuestion(kind="list_columns"), engine, "doc_x_0") == "该表共有 2 列：date、OT。")
    _check("列数", answer_schema_question(SchemaQuestion(kind="column_count"), engine, "doc_x_0") == "该表共有 2 列。")
    _check("行数", answer_schema_question(SchemaQuestion(kind="row_count"), engine, "doc_x_0") == "该表共有 2 行。")

    # 4) _resolve_doc_table：块 id 寻址 + 文档隔离（doc:<doc_id>:<段> → 真实表名）
    #    真实表名形如 doc_<doc_id>_<idx>，块 id 是 12~16 位十六进制，二者不冲突。
    #    表名合法性只认「本文档提取出的表」，不得跨文档、不得寻址 docs.db 内部表。
    _check("块id寻址 None→None（整篇文档）", _resolve_doc_table(engine, "x", None) is None)
    with patch("app.services.document_service.get_doc_tables", return_value=["doc_x_0"]), \
         patch("app.services.document_service.get_doc_table_by_block", return_value=None):
        _check("本文档真实表名原样返回", _resolve_doc_table(engine, "x", "doc_x_0") == "doc_x_0")
        _check("跨文档表名被拒 doc_y_0", _resolve_doc_table(engine, "x", "doc_y_0") is None)
        _check("内部表 doc_meta 被拒", _resolve_doc_table(engine, "x", "doc_meta") is None)
        _check("内部表 doc_blocks 被拒", _resolve_doc_table(engine, "x", "doc_blocks") is None)
        _check("未知块 id → None", _resolve_doc_table(engine, "x", "deadbeefdeadbeef") is None)
    with patch("app.services.document_service.get_doc_tables", return_value=["doc_x_0"]), \
         patch("app.services.document_service.get_doc_table_by_block", return_value="doc_x_0") as m:
        _check("块id寻址 块id→表名", _resolve_doc_table(engine, "x", "03b8923724cf4b9") == "doc_x_0")
        m.assert_called_once_with("x", "03b8923724cf4b9")

    # 5) 数字表头（0/1 → t_0/t_1）：schema 咨询用原始名作答（赛题块 826b037b074c425）
    _check("识别 哪一列叫0", detect_schema_question('数据默认没有行名称，而第一列是完整表格的第一列。在表格中哪一列的名称为"0"？') == SchemaQuestion(kind="column_by_name", name="0"))
    engine3 = create_engine("sqlite://")
    with engine3.begin() as conn:
        conn.execute(text('CREATE TABLE doc_x_0 ("t_0" TEXT, "t_1" TEXT)'))
        conn.execute(text("INSERT INTO doc_x_0 VALUES ('subjtitle', 'boryspil')"))
    with patch("app.services.document_service.get_doc_table_original_columns", return_value=["0", "1"]):
        _check("原始名 哪一列叫0 → 第1列", answer_schema_question(SchemaQuestion(kind="column_by_name", name="0"), engine3, "doc_x_0") == "0 是第 1 列。")
        _check("原始名 第1列 → 0", answer_schema_question(SchemaQuestion(kind="nth_column", n=1), engine3, "doc_x_0") == "第 1 列的列名是 0。")
        _check("原始名 列出列", answer_schema_question(SchemaQuestion(kind="list_columns"), engine3, "doc_x_0") == "该表共有 2 列：0、1。")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
