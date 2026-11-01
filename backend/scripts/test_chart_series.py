"""多图表类型（柱+线组合、双轴）纯规则自检：不依赖 LLM / 数据库。

覆盖 ``app/llm/intent.py`` 的多类型识别与 ``app/services/chart_service.py``
的系列映射，重点回归：

1. 「分别绘制柱状图和折线图」两种类型必须都保留 —— 早前单值 chart_hint +
   规则兜底命中即 break，第二种类型被静默丢弃，最终返回 table。
2. 类型与指标**按列顺序一一对应**：第 1 个类型→第 1 个指标，指标多于类型时
   多出的指标跟随最后一个类型。
3. bar 与 line 同时存在时 line 挂右轴（营业额 vs 客单数量级差一个数量级）。
4. 严格模式不变：未显式指定类型 → ("table", [])。
5. 旧构造 ``IntentResult(chart_hint="bar")`` 与旧响应（无 chart_series）保持兼容。

运行：``cd backend && python scripts/test_chart_series.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.llm.intent import (
    IntentResult,
    _detect_chart_hints,
    _normalize_chart_hints,
)
from app.schemas import QueryResponse
from app.services.chart_service import suggest_chart_series, suggest_chart_type


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _specs(series):
    return [(s.column, s.type, s.axis) for s in series]


def main() -> None:
    # 1) 归一化：数组 / 分隔串 / 中文混排 / 非法值
    _check("数组归一化", _normalize_chart_hints(["bar", "line"]) == ["bar", "line"])
    _check("逗号分隔串", _normalize_chart_hints("bar,line") == ["bar", "line"])
    _check(
        "中文分隔串",
        _normalize_chart_hints("柱状图和折线图") == ["bar", "line"],
    )
    _check(
        "非法值丢弃 + 大写/重复去重",
        _normalize_chart_hints(["BAR", "柱状图", "line", "bad"]) == ["bar", "line"],
    )
    _check("None 归一化为空", _normalize_chart_hints(None) == [])

    # 2) 按句中出现顺序收集类型
    q = "统计各个月的营业额和客单数，分别绘制柱状图和折线图分析"
    _check("双类型按出现顺序", _detect_chart_hints(q) == ["bar", "line"], str(_detect_chart_hints(q)))
    _check(
        "顺序反转句",
        _detect_chart_hints("用折线图看营业额，用柱状图看订单数") == ["line", "bar"],
        str(_detect_chart_hints("用折线图看营业额，用柱状图看订单数")),
    )
    _check("柱状图+条形图同类去重", _detect_chart_hints("柱状图和条形图") == ["bar"])
    _check("仅说画图 → 无类型", _detect_chart_hints("把结果画个图") == [])

    # 3) IntentResult 新旧构造兼容
    old = IntentResult(intent="query", chart_hint="bar", want_sql=True)
    _check("旧构造 chart_hint 自动补 chart_hints", old.chart_hints == ["bar"], str(old))
    new = IntentResult(intent="query", chart_hints=["bar", "line"])
    _check("新构造 chart_hint 取首项", new.chart_hint == "bar" and new.chart_hints == ["bar", "line"])
    conflict = IntentResult(intent="query", chart_hint="bar", chart_hints=["line"])
    _check("冲突以 chart_hints 为准", conflict.chart_hint == "line", str(conflict))

    # 4) 宽表主场景：营业额 bar 左轴、客单数 line 右轴
    cols = ["月份", "营业额", "客单数"]
    rows = [["2024-03", 7050.2, 206], ["2024-04", 6720.56, 196]]
    chart_type, series = suggest_chart_series(cols, rows, ["bar", "line"])
    _check("主场景主类型", chart_type == "bar", chart_type)
    _check(
        "主场景系列映射",
        _specs(series) == [("营业额", "bar", "left"), ("客单数", "line", "right")],
        str(_specs(series)),
    )

    # 5) 指标多于类型：多出的指标跟随最后一个类型
    cols3 = ["月份", "营业额", "客单数", "杯数"]
    rows3 = [["2024-03", 7050.2, 206, 210], ["2024-04", 6720.56, 196, 198]]
    chart_type, series = suggest_chart_series(cols3, rows3, ["bar", "line"])
    _check(
        "指标多于类型时跟随最后一个类型",
        _specs(series)
        == [
            ("营业额", "bar", "left"),
            ("客单数", "line", "right"),
            ("杯数", "line", "right"),
        ],
        str(_specs(series)),
    )

    # 6) 类型多于指标：忽略多余类型
    _, series = suggest_chart_series(["月份", "营业额"], rows, ["bar", "line"])
    _check("类型多于指标时忽略多余类型", _specs(series) == [("营业额", "bar", "left")], str(_specs(series)))

    # 7) 单一类型：所有系列同类型、同一左轴（不出空右轴）
    _, series = suggest_chart_series(cols, rows, ["line"])
    _check(
        "单类型折线全左轴",
        _specs(series) == [("营业额", "line", "left"), ("客单数", "line", "left")],
        str(_specs(series)),
    )

    # 8) 长表（第 2 列是系列名）：按系列名首次出现顺序映射
    long_cols = ["月份", "指标", "数值"]
    long_rows = [
        ["2024-03", "营业额", 7050.2],
        ["2024-03", "客单数", 206],
        ["2024-04", "营业额", 6720.56],
        ["2024-04", "客单数", 196],
    ]
    chart_type, series = suggest_chart_series(long_cols, long_rows, ["bar", "line"])
    _check("长表主类型", chart_type == "bar", chart_type)
    _check(
        "长表按系列名映射",
        _specs(series) == [("营业额", "bar", "left"), ("客单数", "line", "right")],
        str(_specs(series)),
    )

    # 9) 空结果 / 列数不足 → table（严格模式 + 不渲染空图卡片）
    _check("空 rows → table", suggest_chart_series(cols, [], ["bar"]) == ("table", []))
    _check("单列 → table", suggest_chart_series(["月份"], [["2024-03"]], ["bar"]) == ("table", []))
    _check("无类型 → table", suggest_chart_series(cols, rows, []) == ("table", []))

    # 10) 饼图：单独保持饼图；混用时丢弃 pie、其余继续组合
    chart_type, series = suggest_chart_series(["品类", "金额"], [["A", 1], ["B", 2]], ["pie"])
    _check("饼图单独", chart_type == "pie" and _specs(series) == [("金额", "pie", "left")], str(_specs(series)))
    chart_type, series = suggest_chart_series(cols, rows, ["pie", "bar"])
    _check(
        "饼图混用丢弃 pie",
        chart_type == "bar" and all(s.type == "bar" for s in series),
        str(_specs(series)),
    )

    # 11) 旧接口与旧响应兼容
    _check("suggest_chart_type 单值兼容", suggest_chart_type(cols, rows, q, "bar") == "bar")
    _check("suggest_chart_type 无类型 → table", suggest_chart_type(cols, rows, q) == "table")
    _check("旧响应无 chart_series", QueryResponse(question="q").chart_series == [])

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
