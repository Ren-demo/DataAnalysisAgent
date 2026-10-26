"""标识符引用与 SQL 注入防护离线自检：不依赖 LLM / 外部服务。

覆盖 ``app/services/datasource_service.py`` 的 ``_clean_identifier`` / ``_sanitize_table_name``
与各方言 ``identifier_preparer.quote`` 的组合效果，验证：

1. 上传文件名中的引号/分号被清洗掉，无法建成含引号的物理表名；
2. 对**已存在**于库中的含引号表名，正确引用后不再可注入（对照组演示旧裸拼写法会泄露数据）。

运行：``cd backend && python scripts/test_identifier_quote.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import create_engine, text

from app.services.datasource_service import _clean_identifier, _sanitize_table_name

# 实测可用的注入文件名：其 stem 通过 UNION 读出同库任意其它表
_EVIL_FILENAME = 'evil" UNION SELECT secret FROM private_data UNION SELECT ".csv'
_EVIL_TABLE = 'evil" UNION SELECT secret FROM private_data UNION SELECT "'


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def main() -> None:
    # 1) 清洗：引号 / 反引号 / 分号 / 控制字符一律替换，其余原始名尽量保留
    _check("清洗去掉双引号", '"' not in _clean_identifier(_EVIL_TABLE, "t"))
    _check("清洗去掉反引号", "`" not in _clean_identifier("a`b", "t"))
    _check("清洗去掉分号", ";" not in _clean_identifier("a;b", "t"))
    _check("清洗去掉控制字符", "\n" not in _clean_identifier("a\nb", "t"))
    _check("中文/空格/数字开头保留", _clean_identifier("销售 2024", "t") == "销售 2024")
    _check("空名回退", _clean_identifier("   ", "fallback") == "fallback")
    _check("文件名清洗后无引号", '"' not in _sanitize_table_name(_EVIL_FILENAME))

    # 2) 已落库的含引号表名：正确引用后不再可注入
    engine = create_engine("sqlite://")
    quoted = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        # 受害者表：本不该被 upload:<evil> 数据源读到
        conn.execute(text("CREATE TABLE private_data (secret TEXT)"))
        conn.execute(text("INSERT INTO private_data VALUES ('TOP_SECRET')"))
        # UNION 注入的第一段必须指向真实存在的表，攻击者可先上传一张同名表
        conn.execute(text("CREATE TABLE evil (x TEXT)"))
        conn.execute(text("INSERT INTO evil VALUES ('x')"))
        # 攻击者表：表名本身就是注入载荷（历史上由未清洗的文件名建成）
        conn.execute(text(f"CREATE TABLE {quoted(_EVIL_TABLE)} (own TEXT)"))
        conn.execute(text(f"INSERT INTO {quoted(_EVIL_TABLE)} VALUES ('own_data')"))

    # 对照组：旧写法（裸 f-string 加引号，载荷与 row limit 拼接方式同 load_table）确实会泄露
    with engine.connect() as conn:
        leaked = conn.execute(
            text(f'SELECT * FROM "{_EVIL_TABLE}" LIMIT :limit'),  # noqa: S608 — 故意复现旧写法
            {"limit": 20000},
        ).fetchall()
    _check(
        "对照组：旧裸拼写法会泄露他表数据",
        any("TOP_SECRET" in str(v) for row in leaked for v in row),
        f"实际返回 {leaked}",
    )

    # 修复后：同样的表名，经 quote 正确引用，只读到自己的数据
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT * FROM {quoted(_EVIL_TABLE)} LIMIT :limit"), {"limit": 20000}
        ).fetchall()
    flat = [str(v) for row in rows for v in row]
    _check("修复后：不再泄露他表数据", not any("TOP_SECRET" in v for v in flat), f"实际返回 {flat}")
    _check("修复后：仍能读到本表数据", any("own_data" in v for v in flat), f"实际返回 {flat}")

    # 3) 方言正确的引用字符（旧写法硬编码双引号；MySQL 把双引号当字符串字面量 → 读表必失败）
    from sqlalchemy.dialects import mysql

    mysql_quote = mysql.dialect().identifier_preparer.quote
    _check("MySQL 需引用时用反引号", mysql_quote("my table") == "`my table`", mysql_quote("my table"))
    _check("MySQL 含双引号名安全", mysql_quote('a"b') == '`a"b`', mysql_quote('a"b'))
    _check("SQLite 需引用时用双引号", quoted("my table") == '"my table"', quoted("my table"))
    _check("SQLite 内嵌双引号被加倍", quoted('a"b') == '"a""b"', quoted('a"b'))
    # 无需引用时各方言都返回裸名（仍可执行，不算缺陷）
    _check("简单名不加引号", quoted("orders") == "orders" and mysql_quote("orders") == "orders")

    print("\n全部通过。")


if __name__ == "__main__":
    main()
