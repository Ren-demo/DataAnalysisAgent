"""数据源标识符清洗 + schema 文本引号 离线自检：不依赖 LLM / 外部服务。

覆盖 ``app/services/datasource_service.py`` 的 ``_clean_identifier`` / ``_sanitize_columns``
与 ``app/database/engine.py`` 的 ``build_schema_text``，验证：
- 中文列名/表名被保留（不再 collapse 成 col / uploaded_data）；
- 数字开头列名不再加 t_ 前缀；
- 重复列名仍按大小写不敏感去重加 _N；
- 含中文/空格/数字开头的真实标识符在 schema 文本里被方言引号包裹。

运行：``cd backend && python scripts/test_datasource.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import create_engine, text

from app.database.engine import build_schema_text
from app.services.datasource_service import _clean_identifier, _sanitize_columns


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def main() -> None:
    # 1) _clean_identifier：保留原始名，仅去控制字符与首尾空白
    _check("中文列名保留", _clean_identifier("月份", "col") == "月份")
    _check("中文表名保留", _clean_identifier("销售数据", "uploaded_data") == "销售数据")
    _check("数字开头不加 t_", _clean_identifier("0", "col") == "0")
    _check("纯数字列名保留", _clean_identifier("2024", "col") == "2024")
    _check("去首尾空白、保留内部空格", _clean_identifier("  Column A ", "col") == "Column A")
    _check("空名回退", _clean_identifier("", "col") == "col")
    _check("纯空白回退", _clean_identifier("   ", "col") == "col")
    _check("控制字符替换为 _", _clean_identifier("a\nb\tc", "col") == "a_b_c")

    # 2) _sanitize_columns：去重仍生效，且作用在保留后的原始名上
    _check(
        "中文重复列去重",
        _sanitize_columns(["月份", "月份", "销售额"]) == ["月份", "月份_1", "销售额"],
    )
    _check("数字表头保留", _sanitize_columns(["0", "1"]) == ["0", "1"])
    _check(
        "大小写不敏感去重",
        _sanitize_columns(["ID", "id"]) == ["ID", "id_1"],
    )

    # 3) build_schema_text：真实标识符按方言加引号
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE "销售表" ("月份" TEXT, "0" TEXT, "Column A" TEXT)'))
    schema = build_schema_text(engine)
    _check("表名加引号", 'CREATE TABLE "销售表"' in schema)
    _check("中文列名加引号", '"月份" TEXT' in schema)
    _check("数字列名加引号", '"0" TEXT' in schema)
    _check("含空格列名加引号", '"Column A" TEXT' in schema)

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
