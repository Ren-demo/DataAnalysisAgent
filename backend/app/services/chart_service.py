"""图表类型推断（严格模式）：只有用户显式指定图表类型时才出图，否则一律 table。

自「分别绘制柱状图和折线图」起，支持**多种类型组合**：``chart_hints`` 按指标列
顺序排列（如 ``["bar", "line"]``），逐列映射成 ``ChartSeries``；bar 与 line 同时
存在时，line 系列挂右轴（两种指标量级通常差一个数量级，共用一轴会把小的压平）。

严格模式不变：``chart_hints`` 为空 → ``chart_type="table"`` 且不返回任何系列，
不做基于数据形态的启发式推断。多系列渲染由前端 ChartCard 依据 ``chart_series``
（缺失时退回 ``chart_type``）决定。
"""
import math
import re

from app.schemas import ChartSeries

_VALID_TYPES = ("bar", "line", "pie")
_HINT_SEP_RE = re.compile(r"[,，、;；/|+\s和与及]+")


def _coerce_hints(chart_hints) -> list[str]:
    """归一化为去重保序的 bar/line/pie 列表（兼容单个字符串/分隔串）。"""
    if chart_hints is None:
        return []
    raw = (
        list(chart_hints)
        if isinstance(chart_hints, (list, tuple, set))
        else [chart_hints]
    )
    out: list[str] = []
    for item in raw:
        for part in _HINT_SEP_RE.split(str(item)):
            t = part.strip().lower()
            if t in _VALID_TYPES and t not in out:
                out.append(t)
    return out


def _is_numeric_column(values: list) -> bool:
    """一组值能否整体当作数值解析（宽表的指标列可以，长表的系列名标签不行）。"""
    usable = [v for v in values if v is not None and v != ""]
    if not usable:
        return False
    for v in usable:
        try:
            if math.isnan(float(v)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _is_long_format(columns: list, rows: list) -> bool:
    """是否为「长表」形态：第 1 列=分类轴，第 2 列=系列名，第 3 列=数值。

    判据必须与前端 ``frontend/src/components/ChartCard.vue`` 的 ``isLongFormat``
    **完全一致**（三条硬特征缺一不可），两端改动需同步：

      1. 第 1 列（分类）有重复 —— 宽表经 GROUP BY 后每个分类恰好一行；
      2. 第 2 列是系列名标签，基数 ≥ 2 且整体不能当数值解析；
      3. (分类, 系列) 组合唯一 —— 每个格子只有一行。
    """
    if len(columns) < 3 or not rows or any(len(r) < 3 for r in rows):
        return False
    if len({str(r[0]) for r in rows}) >= len(rows):
        return False
    series = {str(r[1]) for r in rows}
    if len(series) < 2:
        return False
    if _is_numeric_column([r[1] for r in rows]):
        return False
    return len({f"{r[0]}\x00{r[1]}" for r in rows}) == len(rows)


def suggest_chart_series(
    columns: list,
    rows: list,
    chart_hints: list[str] | str | None = None,
) -> tuple[str, list[ChartSeries]]:
    """返回 ``(主 chart_type, chart_series)``；未显式指定类型时 ``("table", [])``。

    映射规则（用户已确认）：
      - 第 i 个类型对应第 i 个指标；指标多于类型时，多出的指标跟随最后一个类型；
      - 类型多于指标时忽略多余类型；
      - bar 与 line 同时存在时 line 系列挂右轴，其余挂左轴；
      - 饼图独占：与其它类型混用时丢弃 pie（饼图无法与直角坐标系系列共存）。
    """
    hints = _coerce_hints(chart_hints)
    if not hints or not rows or len(columns) < 2:
        return "table", []

    # 饼图独占：混用时丢弃 pie，保留其余类型继续组合
    if "pie" in hints and len(hints) > 1:
        hints = [h for h in hints if h != "pie"]
    if not hints:
        return "table", []

    if _is_long_format(columns, rows):
        # 长表只有一个数值指标：第 2 列的标签是系列名，不能当成多个指标
        names = list(dict.fromkeys(str(r[1]) for r in rows))
    else:
        names = [str(c) for c in columns[1:]]
    if not names:
        return "table", []

    if hints == ["pie"]:
        return "pie", [ChartSeries(column=names[0], type="pie", axis="left")]

    # 第 i 个类型 → 第 i 个指标；多出的指标跟随最后一个类型
    types = [hints[i] if i < len(hints) else hints[-1] for i in range(len(names))]
    dual_axis = "bar" in types and "line" in types
    specs = [
        ChartSeries(
            column=name,
            type=typ,
            axis="right" if dual_axis and typ == "line" else "left",
        )
        for name, typ in zip(names, types)
    ]
    return types[0], specs


def suggest_chart_type(
    columns: list,
    rows: list,
    question: str,
    chart_hint: str | None = None,
) -> str:
    """兼容旧调用：只返回主图表类型；新代码请用 :func:`suggest_chart_series`。"""
    chart_type, _ = suggest_chart_series(
        columns, rows, [chart_hint] if chart_hint else []
    )
    return chart_type
