"""数据分析统计算子离线自检：不依赖 LLM，直接对 ``data/docs.db`` 的咖啡表跑算子验证数值。

覆盖 ``app/agents/stat_tools.build_stat_tools`` 的各算子，验证：
读表过滤（card 807 行）→ 分组聚合（8 品类 × 4 月 = 32 行）→
相关性（皮尔逊）→ 环比连续增长 → 占比，与 pandas 独立计算的地面真值一致；
再用内存 SQLite 构造可控数据，验证高级建模算子（线性回归 / 时序预测 / 异常检测 / K-Means 聚类）、
多步检索类算子（环比增长率 / 高于整体平均 / 多条件交集）、
宽表多条件取数（``load_table`` 的 ``filters``）、年均增长率（``group_cagr``）、
以及 ``intersect_groups`` 的**列对列**比较（两段区间互比）。

运行：``cd backend && python scripts/test_analysis.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.agents.stat_tools import build_stat_tools

# 咖啡交易表（赛题数据，文档导入后落库为 doc_3430815b5ad7_0，含 cash_type/coffee_name/money/date 列）
TABLE = "doc_3430815b5ad7_0"
# 银行卡支付交易行数（cash_type=card）
CARD_ROWS = 807
# 咖啡品类数
N_COFFEE = 8
# 分组聚合：8 品类 × 4 个月（2024-03 ~ 2024-06）
GROUPED_ROWS = 32


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _coffee_checks(call) -> None:
    """读表 → 分组聚合 → 相关性 → 占比 → 环比（依赖赛题咖啡表，数值为 pandas 独立算出的地面真值）。"""
    # load_table：银行卡过滤 → 807 行，8 品类
    loaded = call("load_table", table_name=TABLE, filter_col="cash_type", filter_value="card")
    _check("load_table 成功", loaded.get("ok") is True, str(loaded))
    _check("load_table 过滤 card 后行数", loaded.get("row_count") == CARD_ROWS, str(loaded.get("row_count")))
    df_card = loaded["df_id"]

    # group_aggregate：按品类+月聚合 avg/count → 32 行
    agg = call(
        "group_aggregate",
        df_id=df_card,
        group_by=["coffee_name"],
        agg_col="money",
        agg_funcs=["avg", "count"],
        date_col="date",
        date_freq="month",
    )
    _check("group_aggregate 成功", agg.get("ok") is True, str(agg))
    _check("group_aggregate 行数=8×4", agg.get("row_count") == GROUPED_ROWS, str(agg.get("row_count")))
    _check(
        "group_aggregate 列名含 avg_money/count_money",
        {"avg_money", "count_money"} <= set(agg.get("columns", [])),
        str(agg.get("columns")),
    )
    df_agg = agg["df_id"]

    # group_correlate：每品类 avg_money 与 count_money 的皮尔逊相关
    corr = call("group_correlate", df_id=df_agg, group_col="coffee_name", x_col="avg_money", y_col="count_money")
    _check("group_correlate 成功", corr.get("ok") is True, str(corr))
    res = corr.get("results", {})
    _check("group_correlate 覆盖 8 品类", len(res) == N_COFFEE, str(len(res)))
    # Americano with Milk / Latte 为强负相关（占位符列全 TEXT，此处验证算子数值与 pandas 一致）
    _check(
        "Americano with Milk 相关系数≈-0.9386",
        abs(res["Americano with Milk"]["corr"] - (-0.9386)) < 1e-3,
        str(res.get("Americano with Milk")),
    )
    _check(
        "Latte 相关系数≈-0.8042",
        abs(res["Latte"]["corr"] - (-0.8042)) < 1e-3,
        str(res.get("Latte")),
    )

    # group_share：每品类销售额占比 + 高于平均判断
    share = call("group_share", df_id=df_card, group_col="coffee_name", value_col="money")
    _check("group_share 成功", share.get("ok") is True, str(share))
    _check("group_share total≈27406.94", abs(share.get("total", 0.0) - 27406.94) < 1e-2, str(share.get("total")))
    _check("group_share 平均占比=1/8", abs(share.get("average_share", 0.0) - 0.125) < 1e-6, str(share.get("average_share")))
    share_res = share.get("results", {})
    _check("Americano with Milk 占比高于平均", share_res["Americano with Milk"]["above_average"] is True)
    _check("Espresso 占比低于平均", share_res["Espresso"]["above_average"] is False)

    # group_mom_growth：按品类+月聚合 sum，取最近 3 月判断严格递增
    agg_sum = call(
        "group_aggregate",
        df_id=df_card,
        group_by=["coffee_name"],
        agg_col="money",
        agg_funcs=["sum"],
        date_col="date",
        date_freq="month",
    )
    _check("group_aggregate(sum) 成功", agg_sum.get("ok") is True, str(agg_sum))
    mom = call(
        "group_mom_growth",
        df_id=agg_sum["df_id"],
        group_col="coffee_name",
        value_col="sum_money",
        date_col="date",
        n=3,
    )
    _check("group_mom_growth 成功", mom.get("ok") is True, str(mom))
    mom_res = mom.get("results", {})
    _check("Americano with Milk 环比严格递增", mom_res["Americano with Milk"]["is_continuous_growth"] is True)
    _check("Cortado 环比严格递增", mom_res["Cortado"]["is_continuous_growth"] is True)
    _check("Latte 环比非严格递增（末两月持平）", mom_res["Latte"]["is_continuous_growth"] is False)
    _check("Americano 环比非严格递增（末月下降）", mom_res["Americano"]["is_continuous_growth"] is False)


def main() -> None:
    backend_dir = Path(__file__).resolve().parent.parent
    engine = create_engine(f"sqlite:///{backend_dir / 'data' / 'docs.db'}")
    tools = build_stat_tools(engine, "sqlite", "dummy schema")
    tools = {t.name: t for t in tools}

    def call(name: str, **kwargs):
        return tools[name].invoke(kwargs)

    # 1) get_schema：原样返回 schema 文本
    schema = tools["get_schema"].invoke({})
    _check("get_schema 返回 schema 文本", schema == "dummy schema", repr(schema))

    # 2~6) 读表 / 聚合 / 相关 / 占比 / 环比：依赖赛题咖啡表（文档链路导入后才落库）
    if TABLE in inspect(engine).get_table_names():
        _coffee_checks(call)
    else:
        print(
            f"[skip] 未找到 {TABLE}，跳过读表/聚合/相关性/占比/环比断言。"
            f"\n       该表由赛题文档经 /api/datasources/upload 导入后落在 "
            f"{backend_dir / 'data' / 'docs.db'}；导入后重跑本脚本即可恢复覆盖。"
        )

    # ===== 高级建模算子：内存 SQLite 构造可控数据验证数值 =====
    # 用 StaticPool 让所有连接共享同一个内存库，避免 :memory: 每个连接各开一份
    mem = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # 线性回归表：y = 2x + 1，完美线性
    pd.DataFrame({"x": [0, 1, 2, 3, 4], "y": [1, 3, 5, 7, 9]}).to_sql(
        "linear_t", mem, index=False
    )
    # 时序表：两组，A 组 val 每期 +2，B 组每期 +3（均严格线性）
    pd.DataFrame(
        {
            "month": ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05"] * 2,
            "val": [10, 12, 14, 16, 18, 100, 103, 106, 109, 112],
            "grp": ["A"] * 5 + ["B"] * 5,
        }
    ).to_sql("ts_t", mem, index=False)
    # 等比增长表：每期 +10%，供 growth_rate 法验证
    pd.DataFrame({"val": [100, 110, 121, 133.1]}).to_sql("grow_t", mem, index=False)
    # 异常表：含一个明显离群值 1000
    pd.DataFrame({"val": [10, 12, 11, 13, 10, 1000]}).to_sql("anom_t", mem, index=False)
    # 聚类表：两组可分离的点（近 0 与 近 10）
    pd.DataFrame(
        {"c1": [0, 0.5, 1, 10, 10.5, 11], "c2": [0, 0.5, 0, 10, 10.5, 10]}
    ).to_sql("cluster_t", mem, index=False)
    # 日粒度表：同月内 3 天，验证 date_freq="day" 不被静默按月度聚合
    pd.DataFrame(
        {"d": ["2024-03-01", "2024-03-01", "2024-03-02", "2024-04-01"], "v": [1, 2, 3, 4]}
    ).to_sql("day_t", mem, index=False)

    t2 = {t.name: t for t in build_stat_tools(mem, "sqlite", "dummy")}

    def _close(actual, expected, tol=1e-6):
        return all(abs(a - b) < tol for a, b in zip(actual, expected))

    # 7) linear_regression：y = 2x + 1
    df_lr = t2["load_table"].invoke({"table_name": "linear_t"})["df_id"]
    reg = t2["linear_regression"].invoke({"df_id": df_lr, "x_col": "x", "y_col": "y"})
    _check("linear_regression 成功", reg.get("ok") is True, str(reg))
    _check("linear_regression slope=2", abs(reg["slope"] - 2.0) < 1e-9, str(reg))
    _check("linear_regression intercept=1", abs(reg["intercept"] - 1.0) < 1e-9, str(reg))
    _check("linear_regression r2=1", abs(reg["r2"] - 1.0) < 1e-9, str(reg))
    _check("linear_regression r=1", abs(reg["r"] - 1.0) < 1e-9, str(reg))

    # 8) forecast_time_series：linear 外推（A 组每期 +2 → 下两期 20/22）
    df_ts_a = t2["load_table"].invoke(
        {"table_name": "ts_t", "filter_col": "grp", "filter_value": "A"}
    )["df_id"]
    fc = t2["forecast_time_series"].invoke(
        {"df_id": df_ts_a, "date_col": "month", "value_col": "val", "horizon": 2, "method": "linear"}
    )
    _check("forecast(linear) 成功", fc.get("ok") is True, str(fc))
    _check(
        "forecast(linear) 预测≈[20,22]",
        len(fc["predictions"]) == 2 and _close(fc["predictions"], [20.0, 22.0]),
        str(fc),
    )
    _check("forecast(linear) r2=1", abs(fc["r2"] - 1.0) < 1e-9, str(fc))

    # 9) forecast_time_series：分组 + average + growth_rate
    df_ts = t2["load_table"].invoke({"table_name": "ts_t"})["df_id"]
    fcg = t2["forecast_time_series"].invoke(
        {"df_id": df_ts, "date_col": "month", "value_col": "val", "horizon": 2, "method": "linear", "group_col": "grp"}
    )
    _check("forecast 分组覆盖 A/B", set(fcg["results"].keys()) == {"A", "B"}, str(fcg))
    _check(
        "forecast 分组 A 预测≈[20,22]",
        _close(fcg["results"]["A"]["predictions"], [20.0, 22.0]),
        str(fcg["results"].get("A")),
    )
    _check(
        "forecast 分组 B 预测≈[115,118]",
        _close(fcg["results"]["B"]["predictions"], [115.0, 118.0]),
        str(fcg["results"].get("B")),
    )
    fc_avg = t2["forecast_time_series"].invoke(
        {"df_id": df_ts_a, "date_col": "month", "value_col": "val", "horizon": 1, "method": "average"}
    )
    _check(
        "forecast(average) 首期=最近3期均值16",
        abs(fc_avg["predictions"][0] - 16.0) < 1e-6,
        str(fc_avg),
    )
    df_grow = t2["load_table"].invoke({"table_name": "grow_t"})["df_id"]
    fg = t2["forecast_time_series"].invoke(
        {"df_id": df_grow, "date_col": "", "value_col": "val", "horizon": 1, "method": "growth_rate"}
    )
    _check(
        "forecast(growth_rate) 预测≈146.41",
        abs(fg["predictions"][0] - 146.41) < 1e-6,
        str(fg),
    )

    # 10) detect_anomalies：iqr 命中离群值，zscore 正常数据无异常
    df_anom = t2["load_table"].invoke({"table_name": "anom_t"})["df_id"]
    det_iqr = t2["detect_anomalies"].invoke(
        {"df_id": df_anom, "value_col": "val", "method": "iqr"}
    )
    _check("detect_anomalies(iqr) 成功", det_iqr.get("ok") is True, str(det_iqr))
    _check(
        "detect_anomalies(iqr) 命中离群值 1000",
        det_iqr["n_anomalies"] == 1 and abs(det_iqr["anomalies"][0] - 1000.0) < 1e-6,
        str(det_iqr),
    )
    det_z = t2["detect_anomalies"].invoke(
        {"df_id": df_lr, "value_col": "y", "method": "zscore"}
    )
    _check("detect_anomalies(zscore) 正常数据无异常", det_z["n_anomalies"] == 0, str(det_z))

    # 11) cluster_kmeans：k=2 聚出两组 [3,3]
    df_cl = t2["load_table"].invoke({"table_name": "cluster_t"})["df_id"]
    cl = t2["cluster_kmeans"].invoke(
        {"df_id": df_cl, "value_cols": ["c1", "c2"], "k": 2}
    )
    _check("cluster_kmeans 成功", cl.get("ok") is True, str(cl))
    _check("cluster_kmeans k=2", cl["k"] == 2, str(cl))
    _check(
        "cluster_kmeans 簇大小 [3,3]",
        sorted(cl["cluster_sizes"].values()) == [3, 3],
        str(cl.get("cluster_sizes")),
    )
    _check("cluster_kmeans 注册新 df_id", "df_id" in cl and cl["df_id"] is not None, str(cl))

    # 12) group_aggregate：date_freq 各粒度（回归 day 被静默按 month 聚合）
    df_day = t2["load_table"].invoke({"table_name": "day_t"})["df_id"]
    agg_day = t2["group_aggregate"].invoke(
        {"df_id": df_day, "group_by": [], "agg_col": "v", "agg_funcs": ["sum"],
         "date_col": "d", "date_freq": "day"}
    )
    _check("date_freq=day 成功", agg_day.get("ok") is True, str(agg_day))
    # 3 个不同日期（2024-03-01 / 03-02 / 04-01）→ 3 行；按 month 聚合只会得到 2 行
    _check("date_freq=day 返回日粒度 3 行", agg_day.get("row_count") == 3, str(agg_day.get("row_count")))
    _check(
        "date_freq=day 分组键为日期而非月份",
        all("-0" in str(r[0]) and len(str(r[0])) == 10 for r in agg_day.get("rows", [])),
        str(agg_day.get("rows")),
    )

    agg_month = t2["group_aggregate"].invoke(
        {"df_id": df_day, "group_by": [], "agg_col": "v", "agg_funcs": ["sum"],
         "date_col": "d", "date_freq": "month"}
    )
    _check("同日两行在 month 粒度下合并（2 行）", agg_month.get("row_count") == 2, str(agg_month.get("row_count")))

    agg_bad = t2["group_aggregate"].invoke(
        {"df_id": df_day, "group_by": [], "agg_col": "v", "agg_funcs": ["sum"],
         "date_col": "d", "date_freq": "fortnight"}
    )
    _check("非法 date_freq 显式报错而非静默兜底", agg_bad.get("ok") is False, str(agg_bad))
    _check("非法 date_freq 错误信息给出可用值", "fortnight" in str(agg_bad.get("error", "")), str(agg_bad))

    # ===== 多步检索类算子：环比增长率 / 高于整体平均 / 多条件交集 =====
    # 环比增长率表：A 每期 +10%，B 每期 +25%，C 只有一期，D 上期为 0（除零）
    pd.DataFrame(
        {
            "grp": ["A"] * 4 + ["B"] * 4 + ["C"] + ["D"] * 2,
            "m": ["2024-01", "2024-02", "2024-03", "2024-04"] * 2 + ["2024-01"] + ["2024-01", "2024-02"],
            "v": [100, 110, 121, 133.1, 100, 125, 156.25, 195.3125, 50, 0, 5],
        }
    ).to_sql("mom_t", mem, index=False)

    # 13) group_mom_growth_rate：增长**多少**、谁最快、排第几
    df_mom = t2["load_table"].invoke({"table_name": "mom_t"})["df_id"]
    mom_rate = t2["group_mom_growth_rate"].invoke(
        {"df_id": df_mom, "group_col": "grp", "value_col": "v", "date_col": "m", "date_freq": "month"}
    )
    _check("group_mom_growth_rate 成功", mom_rate.get("ok") is True, str(mom_rate))
    mr = mom_rate.get("results", {})
    _check("A 环比增长率≈0.10", abs(mr["A"]["growth_rate"] - 0.10) < 1e-9, str(mr.get("A")))
    _check("B 环比增长率≈0.25", abs(mr["B"]["growth_rate"] - 0.25) < 1e-9, str(mr.get("B")))
    _check("A 增长率百分比≈10.0", abs(mr["A"]["growth_rate_pct"] - 10.0) < 1e-6, str(mr.get("A")))
    _check("排名 B 在 A 之前（25% > 10%）", mom_rate.get("ranking") == ["B", "A"], str(mom_rate.get("ranking")))
    _check("A rank=2", mr["A"].get("rank") == 2, str(mr.get("A")))
    # 期数不足 / 上期为 0 时必须是 None，**绝不能是 inf**：inf 会以「增长无穷大」稳居第一，
    # 给出一个看起来正常的错误答案（pct_change 的真实行为）
    _check("C 期数不足 → growth_rate 为 None", mr["C"]["growth_rate"] is None, str(mr.get("C")))
    _check("D 上期为 0 → growth_rate 为 None（不是 inf）", mr["D"]["growth_rate"] is None, str(mr.get("D")))
    _check(
        "无任何 inf/nan 泄漏到结果里",
        not any(
            isinstance(v, float) and (v != v or v in (float("inf"), float("-inf")))
            for g in mr.values()
            for v in g.values()
            if not isinstance(v, list)
        ),
        str(mr),
    )
    _check("rank_by=mean 也可用", t2["group_mom_growth_rate"].invoke(
        {"df_id": df_mom, "group_col": "grp", "value_col": "v", "date_col": "m", "rank_by": "mean"}
    ).get("ok") is True)
    _check("非法 rank_by 显式报错", t2["group_mom_growth_rate"].invoke(
        {"df_id": df_mom, "group_col": "grp", "value_col": "v", "date_col": "m", "rank_by": "max"}
    ).get("ok") is False)

    # 14) group_vs_overall_avg：**水平值** vs 全体组平均（区别于 group_share 的占比）
    #     组均值 A=15 B=35 C=100 → 全体组均值 50；A/B 低于平均，C 高于平均
    pd.DataFrame(
        {"cat": ["A", "A", "B", "B", "C"], "amt": [10, 20, 30, 40, 100]}
    ).to_sql("avg_t", mem, index=False)
    df_avg = t2["load_table"].invoke({"table_name": "avg_t"})["df_id"]
    vs = t2["group_vs_overall_avg"].invoke(
        {"df_id": df_avg, "group_col": "cat", "value_col": "amt", "agg": "avg"}
    )
    _check("group_vs_overall_avg 成功", vs.get("ok") is True, str(vs))
    _check("overall_avg=50（(15+35+100)/3）", abs(vs["overall_avg"] - 50.0) < 1e-9, str(vs.get("overall_avg")))
    vr = vs.get("results", {})
    _check("C 客单价高于整体平均", vr["C"]["above_average"] is True, str(vr.get("C")))
    _check("A 客单价低于整体平均", vr["A"]["above_average"] is False, str(vr.get("A")))
    _check("A value=15", abs(vr["A"]["value"] - 15.0) < 1e-9, str(vr.get("A")))
    _check("C diff=50（100-50）", abs(vr["C"]["diff"] - 50.0) < 1e-9, str(vr.get("C")))
    _check("C diff_pct=100.0", abs(vr["C"]["diff_pct"] - 100.0) < 1e-6, str(vr.get("C")))

    # 15) intersect_groups：多条件求交（多步检索「同时满足 A、B、C」的落点）
    pd.DataFrame(
        {
            "cat": ["A", "A", "B", "B", "C", "C"],
            "name": ["Latte", "Latte", "Espresso", "Espresso", "Mocha", "Mocha"],
            "amt": [10, 20, 30, 40, 100, 5],
            "qty": [1, 1, 5, 5, 2, 2],
        }
    ).to_sql("mix_t", mem, index=False)
    df_mix = t2["load_table"].invoke({"table_name": "mix_t"})["df_id"]
    g_amt = t2["group_aggregate"].invoke(
        {"df_id": df_mix, "group_by": ["cat"], "agg_col": "amt", "agg_funcs": ["sum"]}
    )
    g_qty = t2["group_aggregate"].invoke(
        {"df_id": df_mix, "group_by": ["cat"], "agg_col": "qty", "agg_funcs": ["sum"]}
    )
    g_name = t2["group_aggregate"].invoke(
        {"df_id": df_mix, "group_by": ["cat", "name"], "agg_col": "amt", "agg_funcs": ["count"]}
    )
    # sum_amt: A=30 B=70 C=105；sum_qty: A=2 B=10 C=4
    ix = t2["intersect_groups"].invoke(
        {
            "df_ids": [g_amt["df_id"], g_qty["df_id"]],
            "group_col": "cat",
            "conditions": [
                {"col": "sum_amt", "op": ">", "value": 50},
                {"col": "sum_qty", "op": "<=", "value": 5},
            ],
        }
    )
    _check("intersect_groups 成功", ix.get("ok") is True, str(ix))
    _check("交集只剩 C", ix.get("matched") == ["C"], str(ix.get("matched")))
    _check("per_condition 逐条件给出命中数",
           [c["count"] for c in ix.get("per_condition", [])] == [2, 2], str(ix.get("per_condition")))
    # 三表合并 + 字符串条件（contains）
    ix2 = t2["intersect_groups"].invoke(
        {
            "df_ids": [g_amt["df_id"], g_name["df_id"]],
            "group_col": "cat",
            "conditions": [
                {"col": "sum_amt", "op": "ge", "value": 50},
                {"col": "name", "op": "contains", "value": "och"},
            ],
        }
    )
    _check("三表合并后 contains 求交 → C", ix2.get("matched") == ["C"], str(ix2))
    _check("合并列名回传（同名指标加后缀）", "count_amt" in ix2.get("columns", []), str(ix2.get("columns")))
    # 条件列不存在 / 比较符非法，都必须显式报错，不能静默放行
    bad_col = t2["intersect_groups"].invoke(
        {"df_ids": [g_amt["df_id"]], "group_col": "cat", "conditions": [{"col": "nope", "op": ">", "value": 1}]}
    )
    _check("条件列不存在 → 报错", bad_col.get("ok") is False, str(bad_col))
    bad_op = t2["intersect_groups"].invoke(
        {"df_ids": [g_amt["df_id"]], "group_col": "cat", "conditions": [{"col": "sum_amt", "op": "~=", "value": 1}]}
    )
    _check("非法比较符 → 报错", bad_op.get("ok") is False, str(bad_op))
    bad_num = t2["intersect_groups"].invoke(
        {"df_ids": [g_amt["df_id"]], "group_col": "cat", "conditions": [{"col": "sum_amt", "op": ">", "value": "很多"}]}
    )
    _check("数值比较遇到非数字 value → 报错", bad_num.get("ok") is False, str(bad_num))

    # 16) load_table 的 filter_op：等值之外还要能按包含/数值比较筛（「剔除含 with Milk 的品类」）
    for op, val, expect in (("contains", "Latte", 2), ("not_contains", "Latte", 4), ("gt", 25, 3), ("le", 20, 3)):
        r = t2["load_table"].invoke(
            {"table_name": "mix_t", "filter_col": "name" if op.endswith("ontains") else "amt",
             "filter_value": val, "filter_op": op}
        )
        _check(f"load_table filter_op={op} → {expect} 行", r.get("ok") is True and r.get("row_count") == expect, str(r))
    bad_filter = t2["load_table"].invoke(
        {"table_name": "mix_t", "filter_col": "amt", "filter_value": "x", "filter_op": "gt"}
    )
    _check("load_table 非法过滤（数值比较给非数字）→ 报错", bad_filter.get("ok") is False, str(bad_filter))

    # ===== 多步检索类问题：宽表多条件取数 + 年均增长率 + 列对列求交 =====
    # 17) load_table 的 filters：一张宽表同时固定多列（AND 语义）
    #     赛题该文档抽出的表就是 region/category/parameter/mode/powertrain/year/unit/value
    #     这种逐维度展开的长表，定位目标数据要一次固定四五列 —— 早前只支持单条件，
    #     一列一轮就是四五轮工具调用，正是「流式长时间无数据」的耗时根因之一。
    pd.DataFrame(
        {
            "region": ["北京", "北京", "北京", "北京", "上海", "上海", "上海", "上海"],
            "category": ["Historical", "Historical", "Projection", "Projection"] * 2,
            "powertrain": ["BEV", "PHEV", "BEV", "PHEV"] * 2,
            "year": [2015, 2016, 2021, 2022] * 2,
            "value": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0],
        }
    ).to_sql("wide_t", mem, index=False)

    legacy = t2["load_table"].invoke(
        {"table_name": "wide_t", "filter_col": "category", "filter_value": "Historical"}
    )
    _check(
        "load_table 单条件旧签名仍可用（4 行）",
        legacy.get("ok") is True and legacy.get("row_count") == 4,
        str(legacy),
    )

    multi = t2["load_table"].invoke(
        {
            "table_name": "wide_t",
            "filters": [
                {"col": "category", "op": "eq", "value": "Historical"},
                {"col": "powertrain", "op": "eq", "value": "BEV"},
            ],
        }
    )
    _check(
        "load_table filters 两条件 AND → 2 行",
        multi.get("ok") is True and multi.get("row_count") == 2,
        str(multi),
    )
    _check(
        "filters_applied 逐条件回传剩余行数（筛空时一眼看出是哪个条件）",
        [c["remaining"] for c in multi.get("filters_applied", [])] == [4, 2],
        str(multi.get("filters_applied")),
    )

    three = t2["load_table"].invoke(
        {
            "table_name": "wide_t",
            "filters": [
                {"col": "category", "op": "eq", "value": "Historical"},
                {"col": "powertrain", "op": "eq", "value": "BEV"},
                {"col": "region", "op": "eq", "value": "北京"},
            ],
        }
    )
    _check(
        "load_table filters 三条件 AND → 1 行",
        three.get("ok") is True and three.get("row_count") == 1,
        str(three),
    )
    _check(
        "三条件命中的是 value=10 那行",
        three.get("ok") is True and float(three["sample_rows"][0][-1]) == 10.0,
        str(three.get("sample_rows")),
    )

    numeric = t2["load_table"].invoke(
        {
            "table_name": "wide_t",
            "filters": [
                {"col": "year", "op": "ge", "value": 2021},
                {"col": "region", "op": "eq", "value": "上海"},
            ],
        }
    )
    _check(
        "filters 里数值比较（year>=2021）+ 等值 → 2 行",
        numeric.get("ok") is True and numeric.get("row_count") == 2,
        str(numeric),
    )

    mixed = t2["load_table"].invoke(
        {
            "table_name": "wide_t",
            "filter_col": "region",
            "filter_value": "北京",
            "filters": [{"col": "category", "op": "eq", "value": "Projection"}],
        }
    )
    _check(
        "单条件与 filters 并存（AND）→ 2 行",
        mixed.get("ok") is True and mixed.get("row_count") == 2,
        str(mixed),
    )

    bad_fcol = t2["load_table"].invoke(
        {"table_name": "wide_t", "filters": [{"col": "nope", "op": "eq", "value": 1}]}
    )
    _check(
        "filters 列不存在 → 报错并列出可用列",
        bad_fcol.get("ok") is False and "region" in str(bad_fcol.get("error", "")),
        str(bad_fcol),
    )
    bad_fop = t2["load_table"].invoke(
        {"table_name": "wide_t", "filters": [{"col": "value", "op": "~=", "value": 1}]}
    )
    _check("filters 非法比较符 → 报错", bad_fop.get("ok") is False, str(bad_fop))
    bad_fshape = t2["load_table"].invoke(
        {"table_name": "wide_t", "filters": ["category=Historical"]}
    )
    _check(
        "filters 元素不是对象 → 报错（不静默丢条件）",
        bad_fshape.get("ok") is False,
        str(bad_fshape),
    )

    # 18) group_cagr：年均增长率。**整数年份列**是最容易踩的点（见 _to_datetime）：
    #     pd.to_datetime 把 [2015,2016] 当 epoch 纳秒 → 年份全塌缩成 1970，
    #     区间过滤一行都筛不到、或报「期数不足」，形态上像「数据不够」而不报错。
    years = [2015, 2016, 2017, 2018, 2019, 2020]
    cagr_rows = []
    for k, y in enumerate(years):
        cagr_rows.append({"region": "R1", "year": y, "value": 100.0 * (1.30 ** k)})  # 年均 +30%
        cagr_rows.append({"region": "R2", "year": y, "value": 100.0 * (1.10 ** k)})  # 年均 +10%
    cagr_rows.append({"region": "R3", "year": 2020, "value": 50.0})                    # 期数不足
    for k, y in enumerate(years):
        cagr_rows.append({"region": "R4", "year": y, "value": 0.0 if k == 0 else 10.0 * k})  # 零基数
    pd.DataFrame(cagr_rows).to_sql("cagr_t", mem, index=False)
    df_cagr = t2["load_table"].invoke({"table_name": "cagr_t"})["df_id"]

    cg = t2["group_cagr"].invoke(
        {
            "df_id": df_cagr,
            "group_col": "region",
            "value_col": "value",
            "date_col": "year",
            "start": "2015",
            "end": "2020",
        }
    )
    _check("group_cagr 成功", cg.get("ok") is True, str(cg))
    cgr = cg.get("results", {})
    _check("R1 年均增长率≈0.30", abs(cgr["R1"]["growth_rate"] - 0.30) < 1e-9, str(cgr.get("R1")))
    _check("R2 年均增长率≈0.10", abs(cgr["R2"]["growth_rate"] - 0.10) < 1e-9, str(cgr.get("R2")))
    _check("R1 增长率百分比≈30.0", abs(cgr["R1"]["growth_rate_pct"] - 30.0) < 1e-6, str(cgr.get("R1")))
    _check("R1 期数=5（6 个年点 → 5 个间隔）", cgr["R1"]["periods_count"] == 5, str(cgr.get("R1")))
    _check(
        "R1 期标签为真实年份（整数年份未被当成 epoch 纳秒）",
        cgr["R1"]["periods"] == ["2015", "2016", "2017", "2018", "2019", "2020"],
        str(cgr["R1"].get("periods")),
    )
    _check(
        "R1 首末值 100 / 371.293",
        abs(cgr["R1"]["start_value"] - 100.0) < 1e-9
        and abs(cgr["R1"]["end_value"] - 371.293) < 1e-6,
        str(cgr.get("R1")),
    )
    _check("排名 R1 在 R2 之前（30% > 10%）", cg.get("ranking") == ["R1", "R2"], str(cg.get("ranking")))
    _check("R1 rank=1", cgr["R1"].get("rank") == 1, str(cgr.get("R1")))
    _check("R3 期数不足 → growth_rate 为 None", cgr["R3"]["growth_rate"] is None, str(cgr.get("R3")))
    _check("R3 附 note 说明原因", bool(cgr["R3"].get("note")), str(cgr.get("R3")))
    _check(
        "R4 零基数 → growth_rate 为 None（不是 inf）",
        cgr["R4"]["growth_rate"] is None,
        str(cgr.get("R4")),
    )
    _check(
        "group_cagr 结果里无 inf/nan 泄漏",
        not any(
            isinstance(v, float) and (v != v or v in (float("inf"), float("-inf")))
            for g in cgr.values()
            for v in g.values()
            if not isinstance(v, list)
        ),
        str(cgr),
    )
    _check(
        "method=aagr 也可用（等比序列上与 cagr 一致）",
        abs(
            t2["group_cagr"].invoke(
                {
                    "df_id": df_cagr, "group_col": "region", "value_col": "value",
                    "date_col": "year", "start": "2015", "end": "2020", "method": "aagr",
                }
            )["results"]["R1"]["growth_rate"]
            - 0.30
        )
        < 1e-9,
    )
    cg_short = t2["group_cagr"].invoke(
        {
            "df_id": df_cagr, "group_col": "region", "value_col": "value",
            "date_col": "year", "start": "2015", "end": "2018",
        }
    )
    _check(
        "end 收窄到 2018 → 期数=3",
        cg_short["results"]["R1"]["periods_count"] == 3,
        str(cg_short["results"].get("R1")),
    )
    cg_empty = t2["group_cagr"].invoke(
        {
            "df_id": df_cagr, "group_col": "region", "value_col": "value",
            "date_col": "year", "start": "2030", "end": "2035",
        }
    )
    _check("区间内无数据 → 显式报错（不返回空结果）", cg_empty.get("ok") is False, str(cg_empty))
    _check(
        "非法 method / date_freq → 报错",
        t2["group_cagr"].invoke(
            {"df_id": df_cagr, "group_col": "region", "value_col": "value",
             "date_col": "year", "method": "x"}
        ).get("ok") is False
        and t2["group_cagr"].invoke(
            {"df_id": df_cagr, "group_col": "region", "value_col": "value",
             "date_col": "year", "date_freq": "fortnight"}
        ).get("ok") is False,
    )

    # 19) intersect_groups 的**列对列**比较：同一主体两段区间互比
    #     （「预测期增长率高于历史均值」「增量超过历史总增量 50%」只能这样表达）
    proj_rows = []
    for y in range(2015, 2026):
        k = y - 2015
        if y <= 2020:
            v1, v2 = 100.0 * (1.30 ** k), 100.0 * (1.10 ** k)          # 历史：R1 +30% / R2 +10%
        else:
            v1, v2 = 400.0 * (1.15 ** (k - 6)), 200.0 * (0.97 ** (k - 6))  # 预测：R1 +15% / R2 -3%
        proj_rows.append({"region": "R1", "year": y, "value": v1})
        proj_rows.append({"region": "R2", "year": y, "value": v2})
    pd.DataFrame(proj_rows).to_sql("proj_t", mem, index=False)
    df_proj = t2["load_table"].invoke({"table_name": "proj_t"})["df_id"]

    def _interval(start: str, end: str) -> dict:
        return t2["group_cagr"].invoke(
            {"df_id": df_proj, "group_col": "region", "value_col": "value",
             "date_col": "year", "start": start, "end": end}
        )

    hist, fut = _interval("2015", "2020"), _interval("2021", "2025")
    _check("两段区间的 group_cagr 均成功", hist.get("ok") is True and fut.get("ok") is True, str(hist) + str(fut))
    _check(
        "历史期 R1=30% / R2=10%",
        abs(hist["results"]["R1"]["growth_rate"] - 0.30) < 1e-9
        and abs(hist["results"]["R2"]["growth_rate"] - 0.10) < 1e-9,
        str(hist.get("results")),
    )
    _check(
        "预测期 R1 年均≈15%（5 个年点 = 4 个间隔，年化后不是 74.9% 的总增幅）",
        abs(fut["results"]["R1"]["growth_rate"] - 0.15) < 1e-9,
        str(fut["results"].get("R1")),
    )
    _check(
        "预测期 R2 年均≈-3%（负增长也要能算）",
        abs(fut["results"]["R2"]["growth_rate"] - (-0.03)) < 1e-9,
        str(fut["results"].get("R2")),
    )

    # 「末年水平超过 X」：直接用 group_cagr 返回的 end_value 求交，不必另跑算子
    lvl = t2["intersect_groups"].invoke(
        {
            "df_ids": [hist["df_id"]],
            "group_col": "region",
            "conditions": [{"col": "end_value", "op": ">", "value": 300}],
        }
    )
    _check("end_value>300 求交 → R1（末年水平超阈值）", lvl.get("matched") == ["R1"], str(lvl))

    fut_id = fut["df_id"]
    pair = t2["intersect_groups"].invoke(
        {
            "df_ids": [hist["df_id"], fut_id],
            "group_col": "region",
            "conditions": [
                {"col": "growth_rate", "op": ">", "value": 0.05},
                {"col": f"growth_rate_{fut_id}", "op": ">",
                 "value": {"col": "growth_rate", "factor": 0.3}},
            ],
        }
    )
    _check("列对列比较成功", pair.get("ok") is True, str(pair))
    _check(
        "合并后第二张表的同名列带 _<df_id> 后缀",
        f"growth_rate_{fut_id}" in pair.get("columns", []),
        str(pair.get("columns")),
    )
    _check(
        "两条件都不可少：R1（15% > 30%×0.3）留下，R2（-3% < 10%×0.3）被筛掉",
        pair.get("matched") == ["R1"]
        and [c["count"] for c in pair.get("per_condition", [])] == [2, 1],
        str(pair),
    )
    bad_factor = t2["intersect_groups"].invoke(
        {
            "df_ids": [hist["df_id"], fut_id],
            "group_col": "region",
            "conditions": [
                {"col": "growth_rate", "op": ">",
                 "value": {"col": f"growth_rate_{fut_id}", "factor": "很多"}}
            ],
        }
    )
    _check("列引用 factor 非数字 → 报错", bad_factor.get("ok") is False, str(bad_factor))
    bad_ref = t2["intersect_groups"].invoke(
        {
            "df_ids": [hist["df_id"]],
            "group_col": "region",
            "conditions": [{"col": "growth_rate", "op": ">", "value": {"col": "nope", "factor": 1}}],
        }
    )
    _check("列引用指向不存在的列 → 报错", bad_ref.get("ok") is False, str(bad_ref))
    bad_pair_op = t2["intersect_groups"].invoke(
        {
            "df_ids": [hist["df_id"], fut_id],
            "group_col": "region",
            "conditions": [
                {"col": "growth_rate", "op": "contains",
                 "value": {"col": f"growth_rate_{fut_id}", "factor": 1}}
            ],
        }
    )
    _check(
        "列对列用 contains → 报错（两列相比没有包含语义）",
        bad_pair_op.get("ok") is False,
        str(bad_pair_op),
    )

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
