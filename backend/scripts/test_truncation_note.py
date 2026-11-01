"""截断口径自检：结果被截断时，``rows`` / ``row_count`` / ``explanation`` 必须一致。

线上事故的第二种「数字对不上」：查询实际返回 896 行（未超过 ``max_result_rows``），
但会话记忆落库时截到了 200 行，正文却仍写着「已返回全量 896 行」。同类隐患还有
``_execute_node`` 用 LLM 分析**整段覆盖** explanation —— 把「结果超过 N 行」这条
规则提示冲掉，于是正文说全量、rows 只有前 N 行。

本脚本验证 ``query_service`` 这一端的三条不变式（内存 SQLite，不依赖 LLM / 外部库）：

1. 超过 ``max_result_rows`` → ``truncated=True``，explanation 带截断说明；
2. 未超过 → ``truncated=False``，explanation 不带任何截断措辞；
3. ``_execute_node`` 的拼接方式（分析文案 + ``truncation_note``）不会丢截断提示。

运行：``cd backend && python scripts/test_truncation_note.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import asyncio

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.agents.tools import MAX_ROWS, build_tools
from app.config import settings
from app.services.query_service import _build_explanation, execute_sql, truncation_note


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _engine(rows: int):
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    pd.DataFrame({"id": range(rows), "v": range(rows)}).to_sql("t", eng, index=False)
    return eng


def main() -> None:
    limit = settings.max_result_rows
    _check("max_result_rows 为正", limit > 0, str(limit))

    # 1) 恰好等于上限：不算截断
    exact = execute_sql("q", "SELECT * FROM t", _engine(limit))
    _check("恰好等于上限不截断", exact.truncated is False, str(exact.truncated))
    _check("未截断时 explanation 无截断措辞", "仅显示前" not in (exact.explanation or ""), str(exact.explanation))
    _check("未截断时 row_count == len(rows)", exact.row_count == len(exact.rows) == limit)

    # 2) 超过上限：truncated=True，行数与说明同步
    over = execute_sql("q", "SELECT * FROM t", _engine(limit + 100))
    _check("超限置 truncated=True", over.truncated is True)
    _check("超限后 row_count == len(rows)", over.row_count == len(over.rows) == limit, str(over.row_count))
    _check(
        "explanation 说明截断与真实上限",
        f"超过 {limit} 行" in (over.explanation or "") and f"仅显示前 {limit} 行" in (over.explanation or ""),
        str(over.explanation),
    )

    # 3) _execute_node 的拼接方式：LLM 分析 + 截断提示，提示不能被分析文案吞掉
    composed = "3 月营业额最高…" + truncation_note(over.row_count, over.truncated)
    _check("拼接后保留分析文案", composed.startswith("3 月营业额最高…"), composed)
    _check("拼接后仍带截断提示", f"仅显示前 {limit} 行" in composed, composed)
    _check("未截断时拼接不引入噪声", "3 月营业额最高…" + truncation_note(over.row_count, False) == "3 月营业额最高…")

    # 4) 规则文案与截断提示同源（避免两处措辞漂移）
    _check(
        "_build_explanation 与 truncation_note 措辞一致",
        truncation_note(limit, True) in _build_explanation(limit, False, True),
        _build_explanation(limit, False, True),
    )
    _check("0 行时不提截断", _build_explanation(0, False, False) == "未查到匹配数据，请调整提问。")

    # 5) 报告 agent 的 execute_sql 工具：row_count 必须是真实总行数，
    #    早前直接返回 rows[:MAX_ROWS] 的行数，报告里「结果行数」最大只有 50。
    tool = {t.name: t for t in build_tools(_engine(MAX_ROWS + 30), "sqlite", "schema")}["execute_sql"]
    out = asyncio.run(tool.ainvoke({"sql": "SELECT * FROM t"}))
    _check("工具返回 ok", out.get("ok") is True, str(out))
    _check("工具 row_count 是真实总行数", out["row_count"] == MAX_ROWS + 30, str(out.get("row_count")))
    _check("工具 returned_rows 是实际返回行数", out["returned_rows"] == MAX_ROWS, str(out.get("returned_rows")))
    _check("工具置 truncated=True", out["truncated"] is True, str(out.get("truncated")))

    small = {t.name: t for t in build_tools(_engine(3), "sqlite", "schema")}["execute_sql"]
    out = asyncio.run(small.ainvoke({"sql": "SELECT * FROM t"}))
    _check("未超限时工具不标截断", out["truncated"] is False and out["row_count"] == 3, str(out))

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
