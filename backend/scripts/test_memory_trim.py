"""会话记忆持久化截断离线自检：不依赖 LLM 与 MongoDB，纯函数级验证。

覆盖 ``app.services.memory_service._trim_result``：
每轮结果经 ``$push`` 全量追加到同一个会话文档，一次万行查询就能把文档推过
MongoDB 16MB 上限 —— 此后该会话的每次写入都失败且被静默吞掉（记忆静默丢失）。
故落库前按 ``settings.memory_max_rows`` 截断，并保持
``row_count == len(rows)`` 这一全链路不变式（前端表格与图表据此渲染）。

运行：``cd backend && python scripts/test_memory_trim.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import settings
from app.services.memory_service import _trim_result


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _result(n: int) -> dict:
    return {
        "question": "q",
        "columns": ["a", "b"],
        "rows": [[i, i * 2] for i in range(n)],
        "row_count": n,
        "truncated": False,
    }


def main() -> None:
    limit = settings.memory_max_rows
    _check("memory_max_rows 为正", limit > 0, str(limit))

    # 1) 未超限：原样返回（同一个对象，不做无谓拷贝）
    small = _result(3)
    _check("未超限时原样返回", _trim_result(small) is small)

    # 2) 恰好等于上限：不截断
    exact = _result(limit)
    _check("恰好等于上限不截断", _trim_result(exact) is exact, str(limit))

    # 3) 超限：截到上限，row_count 同步（不变式 row_count == len(rows)）
    big = _result(limit + 500)
    trimmed = _trim_result(big)
    _check("超限截断到上限", len(trimmed["rows"]) == limit, str(len(trimmed["rows"])))
    _check(
        "截断后 row_count == len(rows)",
        trimmed["row_count"] == len(trimmed["rows"]),
        f"{trimmed['row_count']} vs {len(trimmed['rows'])}",
    )
    _check("截断后置 truncated=True", trimmed["truncated"] is True)
    _check("截断保留前 N 行（顺序不变）", trimmed["rows"][0] == [0, 0] and trimmed["rows"][-1] == [limit - 1, (limit - 1) * 2])

    # 4) 不改动入参（原 dict 不被就地修改）
    _check("不就地修改原结果", len(big["rows"]) == limit + 500 and big["truncated"] is False)

    # 5) 其它字段透传
    _check("columns 透传", trimmed["columns"] == ["a", "b"])
    _check("question 透传", trimmed["question"] == "q")

    # 6) 边界：无 rows / rows 为空 / 非 dict 形状的缺失键均不报错
    _check("无 rows 键不报错", _trim_result({"question": "q"}) == {"question": "q"})
    empty = _result(0)
    _check("空 rows 原样返回", _trim_result(empty) is empty)

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
