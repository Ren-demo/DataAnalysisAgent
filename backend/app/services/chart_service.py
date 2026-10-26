"""图表类型推断：仅在用户明确指定图表类型时返回对应类型，否则一律 table（不出图）。

严格模式：出图唯一条件是 chart_hint 有效（bar / line / pie）；普通数据查询默认返回
表格结果，不再对聚合结果自动推断图表。多系列渲染由前端 ChartCard 依据列数决定。
"""


def suggest_chart_type(
    columns: list,
    rows: list,
    question: str,
    chart_hint: str | None = None,
) -> str:
    """返回 bar / line / pie / table 之一；仅 chart_hint 有效时出图，否则 table。"""
    if chart_hint in ("bar", "line", "pie"):
        return chart_hint
    return "table"
