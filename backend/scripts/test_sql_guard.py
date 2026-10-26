"""SQL 只读守门离线自检：不依赖 LLM，直接断言 ``_validate_sql``。

守门用于 text2sql 生成结果与决策报告 agent 的 ``execute_sql``，必须做到两件事：

- **不放过写操作**，包括藏在 CTE / 子查询里的 DML ——
  早前只查根节点 ``parsed.key``，``WITH del AS (DELETE FROM t RETURNING id) SELECT * FROM del``
  的根节点是 select，能整条通过；SQLite 不支持这种语法故无害，
  但在 PostgreSQL 数据源上会**真的删数据**；
- **不误杀合法只读查询** —— 早前 ``SELECT ... UNION SELECT ...`` 根节点是 union，
  不在白名单里，被拒后整条查询以 ``SQL_ERROR`` 收场。

运行：``cd backend && python scripts/test_sql_guard.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.llm.text2sql import _validate_sql, validate_sql


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _allowed(sql: str, dialect: str = "sqlite") -> bool:
    try:
        _validate_sql(sql, dialect)
        return True
    except ValueError:
        return False


def _rejected(sql: str, dialect: str = "sqlite") -> bool:
    return not _allowed(sql, dialect)


def main() -> None:
    # 1) 写操作型 CTE —— 本守门存在的理由（PostgreSQL 上会真的执行）
    _check(
        "DML CTE：DELETE 被拒（postgres）",
        _rejected("WITH del AS (DELETE FROM orders RETURNING id) SELECT * FROM del", "postgres"),
    )
    _check(
        "DML CTE：INSERT 被拒（postgres）",
        _rejected("WITH ins AS (INSERT INTO t (a) VALUES (1) RETURNING id) SELECT * FROM ins", "postgres"),
    )
    _check(
        "DML CTE：UPDATE 被拒（postgres）",
        _rejected("WITH upd AS (UPDATE t SET a = 1 RETURNING id) SELECT * FROM upd", "postgres"),
    )
    _check(
        "DML 藏在子查询里也被拒",
        _rejected("SELECT * FROM (WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d) x", "postgres"),
    )

    # 2) 集合运算 —— 早前被误杀的合法只读查询
    _check("UNION 被放行", _allowed("SELECT a FROM t UNION SELECT b FROM u"))
    _check("UNION ALL 被放行", _allowed("SELECT a FROM t UNION ALL SELECT b FROM u"))
    _check("INTERSECT 被放行", _allowed("SELECT a FROM t INTERSECT SELECT b FROM u"))
    _check("EXCEPT 被放行", _allowed("SELECT a FROM t EXCEPT SELECT b FROM u"))
    _check("嵌套 UNION 被放行", _allowed("SELECT a FROM (SELECT a FROM t UNION SELECT b FROM u) x"))

    # 3) 常规只读形态
    _check("普通 SELECT 被放行", _allowed("SELECT * FROM orders WHERE amount > 100"))
    _check("CTE 只读被放行", _allowed("WITH s AS (SELECT a FROM t) SELECT * FROM s"))
    _check("JOIN 被放行", _allowed("SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id"))
    _check("聚合+分组被放行", _allowed("SELECT category, COUNT(*) FROM t GROUP BY category"))
    _check("VALUES 被放行", _allowed("VALUES (1), (2)"))

    # 4) 直接写操作
    for sql in (
        "INSERT INTO t (a) VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN a INT",
        "CREATE TABLE t (a INT)",
        "TRUNCATE TABLE t",
    ):
        _check(f"{sql.split()[0]} 被拒", _rejected(sql))

    # 5) 多语句注入：解析出多个根节点
    _check("SELECT 1; DROP TABLE t 被拒", _rejected("SELECT 1; DROP TABLE t"))
    _check("SELECT 1; SELECT 2 被拒（仅允许单条）", _rejected("SELECT 1; SELECT 2"))

    # 6) 解析失败 → ValueError（而非静默放行）
    _check("语法错误被拒", _rejected("SELECT FROM WHERE ???"))

    # 7) 公开别名 validate_sql 与内部实现同体（agent 工具据此复用）
    _check("validate_sql 是 _validate_sql 的别名", validate_sql is _validate_sql)
    _check("validate_sql 放行只读", validate_sql("WITH s AS (SELECT 1) SELECT * FROM s") is None)

    # 8) MySQL 方言同样生效（反引号标识符）
    _check("MySQL DML CTE 被拒", _rejected("WITH d AS (DELETE FROM `t` RETURNING `id`) SELECT * FROM d", "mysql"))
    _check("MySQL 普通查询被放行", _allowed("SELECT `订单号`, SUM(`金额`) FROM `订单` GROUP BY `订单号`", "mysql"))

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
