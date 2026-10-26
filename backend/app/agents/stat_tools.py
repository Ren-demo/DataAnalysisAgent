"""数据分析（统计建模）agent 的工具集：预定义统计算子。

以闭包按请求现建（``build_stat_tools(engine, dialect, schema_text)``），
闭包内维护**请求级内存 DataFrame 注册表** ``df_store``，工具间靠 ``df_id`` 句柄
传递中间结果，避免任何全局可变状态。

算子是只读的：只读库、只对内存 DataFrame 做聚合/计算，不写库、不暴露任意 SQL。
覆盖分组聚合、相关性（皮尔逊）、环比（逐期是否连续增长 + 逐期增长率与排名）、
**年均增长率（CAGR / AAGR）**、占比、水平值 vs 全体组均值、多条件交集，
以及赛题高级「自动数据建模」所需的线性回归、时序预测、异常检测、K-Means 聚类，
让「相关性/环比/同比/年均增长率/时序/预测/异常/聚类」等 SQL 难以表达的问题
改由 pandas/numpy 计算。

各 ``group_*`` 算子除返回嵌套 ``results`` 外，还注册一张**每组一行**的指标表并给出
``df_id``，供 ``intersect_groups`` 做确定性求交 —— 多步检索题「哪些…同时满足 A、B、C」
的落点在那里，靠模型自己逐组心算取交集很容易错。

``load_table`` 支持 ``filters`` **多列同时**过滤（AND）：宽表（按 region/category/
parameter/mode/powertrain 逐维度展开的长表）定位到目标数据往往要一次固定四五列，
一列一轮地筛就是四五轮 LLM 往返。
"""
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.tools import tool
from sqlalchemy import Engine, inspect, text

from app.services.query_service import _to_json_safe

# 单次工具返回的最大行数（聚合结果/样本的展示上限）
MAX_ROWS = 50

# 默认读表行数上限（避免超大表撑爆内存与上下文）
DEFAULT_LIMIT = 20000

_AGG_FUNCS = {"count", "sum", "avg", "min", "max"}
# 用户友好的聚合函数名 → pandas 聚合函数名（avg→mean，count→size 即 COUNT(*)）
_PANDAS_AGG = {"count": "size", "sum": "sum", "avg": "mean", "min": "min", "max": "max"}

# 支持的时间粒度 → pandas period 别名。`day` 用 `D`；
# 早前只在代码里写了 year/quarter/week 三个分支、其余一律落到 month，
# 「按天统计」被静默算成了按月（见 group_aggregate）。
_DATE_FREQ_PERIOD = {
    "day": "D",
    "week": "W",
    "month": "M",
    "quarter": "Q",
    "year": "Y",
}


def _apply_date_freq(dt: "pd.Series", date_freq: str) -> "pd.Series":
    """按粒度把 datetime 列降采样为可分组的值（year 取整数年，其余取 period 字符串）。"""
    if date_freq == "year":
        return dt.dt.year
    return dt.dt.to_period(_DATE_FREQ_PERIOD[date_freq]).astype(str)


def _to_datetime(series: "pd.Series") -> "pd.Series":
    """把一列解析为 datetime，并**正确对待「整数年份」列**。

    ``pd.to_datetime`` 遇到整数会按 **epoch 纳秒**解释：年份列 ``[2015, 2016]`` 会变成
    1970-01-01 00:00:00.000002015，``.dt.year`` 一律得到 **1970** —— 所有年份塌缩成
    同一个期，按年聚合/环比/年均增长率只会报「期数不足（<2）」，形态上却像「数据不够」，
    不报错、不留痕。而 ``_infer_types`` 恰好会把「全是数字的年份文本列」转成 int64，
    文档表格（导入时列全为 TEXT）首当其冲。

    故整数/整值且落在公元年份区间的数值列显式按 ``%Y`` 解析；其余仍走
    ``pd.to_datetime`` 的默认推断（真正的日期串、带时刻的时间戳不受影响）。
    """
    if pd.api.types.is_numeric_dtype(series):
        num = pd.to_numeric(series, errors="coerce")
        whole = num.dropna()
        if len(whole) and bool((whole % 1 == 0).all()) and bool(whole.between(1000, 9999).all()):
            return pd.to_datetime(
                num.round().astype("Int64").astype(str), format="%Y", errors="coerce"
            )
    return pd.to_datetime(series, errors="coerce")


def _infer_types(df: pd.DataFrame) -> pd.DataFrame:
    """对文本列做类型推断：数值文本 → 数值，日期文本 → datetime，其余保持。

    解决文档表格（``document_service`` 导入时列全为 TEXT）导致 SUM/AVG 失效的问题。
    """
    df = df.copy()
    for col in df.columns:
        s = df[col]
        if s.dtype != object:
            continue
        # 数值：绝大多数非空值可转数字才转换
        num = pd.to_numeric(s, errors="coerce")
        if num.notna().mean() > 0.9 and num.notna().any():
            df[col] = num
            continue
        # 日期：绝大多数非空值可解析为时间才转换
        dt = pd.to_datetime(s, errors="coerce")
        if dt.notna().mean() > 0.9 and dt.notna().any():
            df[col] = dt
    return df


def _rows_json(df: pd.DataFrame, n: int) -> dict:
    """把 DataFrame 前 n 行转为列名 + 行数据的 JSON 友好结构。"""
    columns = [str(c) for c in df.columns]
    rows = [
        [_to_json_safe(v) for v in row]
        for row in df.head(n).itertuples(index=False, name=None)
    ]
    return {"columns": columns, "rows": rows}


def _group_metric_frame(groups: dict[str, dict], group_col: str) -> pd.DataFrame:
    """把「``{组名: {指标: 值}}``」形式的算子结果摊平成**每组一行**的 DataFrame。

    各 ``group_*`` 算子本来返回的是这种嵌套 dict，模型得自己在几十个组之间做集合运算
    ——「同时满足 A、B、C 的品类有哪些」这类多步题的落点就在这里，靠模型心算容易错。
    摊平成每组一行后，就能注册成 ``df_id`` 交给 ``intersect_groups`` 做确定性求交。
    """
    rows = [{group_col: k, **v} for k, v in groups.items()]
    return pd.DataFrame(rows)


# 行过滤/条件比较的操作符：符号与别名归一到同一个规范符。
# 两套写法都收，是因为模型时而写 `>`、时而写 `gt` —— 选错一次就是一次白跑的往返，
# 而这些写法在语义上毫无歧义，没必要让调用方猜。
_OP_ALIASES = {
    "==": "==", "eq": "==", "=": "==", "等于": "==",
    "!=": "!=", "ne": "!=", "<>": "!=", "不等于": "!=",
    ">": ">", "gt": ">", "大于": ">",
    ">=": ">=", "ge": ">=", "gte": ">=", "大于等于": ">=",
    "<": "<", "lt": "<", "小于": "<",
    "<=": "<=", "le": "<=", "lte": "<=", "小于等于": "<=",
    "contains": "contains", "包含": "contains",
    "not_contains": "not_contains", "不包含": "not_contains",
    "in": "in", "not_in": "not_in",
}
_OP_HELP = "> >= < <= == != contains not_contains in（也可写 gt/ge/le/lt/eq/ne）"


def _mask_for(series: "pd.Series", op: str, value: Any) -> "pd.Series | None":
    """按操作符对一列求布尔掩码；操作符无法识别或数值比较遇到非数字 value 时返回 None。

    返回 None 由调用方转成工具错误信息 —— 静默当成「全部通过」会让条件被无声丢掉，
    用户拿到的是「满足条件的有 20 个」这种看起来正常、实则没筛过的结果。
    """
    canon = _OP_ALIASES.get(str(op or "").strip().lower())
    if canon is None:
        return None
    if canon in ("==", "!="):
        hit = series.astype(str).eq(str(value))
        return hit if canon == "==" else ~hit
    if canon in ("contains", "not_contains"):
        hit = series.astype(str).str.contains(str(value), case=False, na=False, regex=False)
        return hit if canon == "contains" else ~hit
    if canon in ("in", "not_in"):
        opts = {str(v) for v in value} if isinstance(value, (list, tuple, set)) else {str(value)}
        hit = series.astype(str).isin(opts)
        return hit if canon == "in" else ~hit
    num = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(num):
        return None
    s = pd.to_numeric(series, errors="coerce")
    if canon == ">":
        return s > float(num)
    if canon == ">=":
        return s >= float(num)
    if canon == "<":
        return s < float(num)
    return s <= float(num)


def _mask_for_pair(
    series: "pd.Series", op: str, other: "pd.Series", factor: float, offset: float
) -> "pd.Series | None":
    """列对列比较 ``series <op> other * factor + offset``；算子不可识别时返回 None。

    与 ``_mask_for`` 的分工：那个比的是**常数**（列 > 100000），这个比的是**另一列**
    （预测期增量 > 历史增量 × 0.5）。符号表共用 ``_OP_ALIASES``，不另造一套。
    只收数值语义的比较符 —— contains/in 这类对两列比较没有意义，直接判不可用。
    """
    canon = _OP_ALIASES.get(str(op or "").strip().lower())
    if canon is None or canon in ("contains", "not_contains", "in", "not_in"):
        return None
    try:
        target = pd.to_numeric(other, errors="coerce") * float(factor) + float(offset)
    except (TypeError, ValueError):
        return None
    s = pd.to_numeric(series, errors="coerce")
    if canon == ">":
        return s > target
    if canon == ">=":
        return s >= target
    if canon == "<":
        return s < target
    if canon == "<=":
        return s <= target
    if canon == "==":
        return s == target
    return s != target


def _forecast_series(vals, method: str, horizon: int) -> dict:
    """对单个数值序列做 ``horizon`` 期外推，返回预测值列表与拟合信息。

    - ``linear``：对序列下标做最小二乘线性拟合，外推；返回拟合优度 R²。
    - ``average``：最近 k（≤3）期滚动移动平均，逐期滑动外推。
    - ``growth_rate``：最近若干期环比增长率的均值，按复合增长率外推。
    """
    vals = np.asarray(vals, dtype=float)
    vals = vals[~np.isnan(vals)]
    n = int(len(vals))
    if n == 0:
        return {"n": 0, "predictions": [], "r2": None, "last_value": None}
    last_value = float(vals[-1])
    if n == 1:
        return {
            "n": 1,
            "predictions": [last_value] * horizon,
            "r2": None,
            "last_value": last_value,
            "note": "样本仅一期，按末值平推",
        }
    if method == "growth_rate":
        # 最近若干期环比增长率的均值，复合外推
        k = min(n - 1, 3)
        recent = vals[-(k + 1):]
        rates = [
            (recent[i + 1] - recent[i]) / recent[i]
            for i in range(k)
            if recent[i] != 0
        ]
        avg_rate = float(np.mean(rates)) if rates else 0.0
        predictions: list[float] = []
        cur = last_value
        for _ in range(horizon):
            cur = cur * (1.0 + avg_rate)
            predictions.append(float(cur))
        return {
            "n": n,
            "predictions": predictions,
            "r2": None,
            "last_value": last_value,
            "avg_growth_rate": avg_rate,
        }
    if method == "average":
        # 滚动移动平均，逐期滑动
        k = min(n, 3)
        window = vals[-k:].tolist()
        predictions = []
        for _ in range(horizon):
            nxt = float(np.mean(window))
            predictions.append(nxt)
            window = window[1:] + [nxt]
        return {"n": n, "predictions": predictions, "r2": None, "last_value": last_value}
    # linear（默认）：最小二乘线性外推
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, vals, 1)
    y_pred = slope * x + intercept
    ss_res = float(np.sum((vals - y_pred) ** 2))
    ss_tot = float(np.sum((vals - vals.mean()) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    future_x = np.arange(n, n + horizon, dtype=float)
    predictions = [float(v) for v in (slope * future_x + intercept)]
    return {
        "n": n,
        "predictions": predictions,
        "r2": float(r2),
        "last_value": last_value,
        "slope": float(slope),
    }


def build_stat_tools(engine: Engine, dialect: str, schema_text: str) -> list:
    """按当前数据源现建一组统计算子工具（闭包捕获引擎/表结构/内存注册表）。"""
    df_store: dict[int, pd.DataFrame] = {}
    counter = [0]

    def _register(df: pd.DataFrame) -> int:
        counter[0] += 1
        df_store[counter[0]] = df
        return counter[0]

    def _get(df_id: int) -> pd.DataFrame | None:
        return df_store.get(df_id)

    @tool
    def get_schema() -> str:
        """返回当前数据源的表结构（CREATE TABLE 文本），供理解字段与选择数据表使用。"""
        return schema_text

    @tool
    def load_table(
        table_name: str,
        filter_col: str = "",
        # 收 int/float 而不只是 str：数值比较（gt/lt）时模型很容易直接传数字，
        # 声明成 str 会让它在 pydantic 校验层就被打回，白白多绕一轮
        filter_value: "str | int | float" = "",
        filter_op: str = "eq",
        filters: list | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """读取一张表到内存并返回其句柄 df_id（后续算子用 df_id 引用它）。

        - table_name：表名（见 get_schema）
        - filter_col / filter_value / filter_op：可选，按**单个**列过滤（如
          filter_col="cash_type"、filter_value="card" 表示只看银行卡支付）
        - filters：可选，**同时**按多列过滤，形如
          ``[{"col": "category", "op": "eq", "value": "Historical"},
             {"col": "parameter", "op": "eq", "value": "EV stock"},
             {"col": "powertrain", "op": "eq", "value": "BEV"}]``，
          条件之间是 **AND**。宽表（按 region/category/parameter/mode/powertrain/
          year 逐维度展开的长表）定位到目标数据往往要一次固定四五列，
          一列一轮地筛就是四五轮工具调用 —— 能一次给全就一次给全。
        - filter_op / filters 里各项的 op：过滤方式，默认 eq（等于）；可选
          eq/ne/gt/ge/lt/le/contains/not_contains/in。
          「名称中包含 Latte 的品类」用 contains，「剔除含 with Milk 的品类」用 not_contains，
          「金额高于 30」用 gt —— 早前只有等值过滤，这类题目只能整表读回再让模型自己筛。
        - limit：最多读取行数

        会自动把数值文本列转成数值、日期文本列转成日期，方便后续 sum/avg/环比计算。
        """
        try:
            tables = set(inspect(engine).get_table_names())
        except Exception as e:
            return {"ok": False, "error": f"无法读取表列表：{type(e).__name__}: {e}"}
        if table_name not in tables:
            return {"ok": False, "error": f"表 {table_name} 不存在，可用表见 get_schema。"}
        # 标识符按方言正确引用（SQLite/PG 双引号、MySQL 反引号）并转义内嵌引号，
        # 既避免表名含引号时的 SQL 注入，也修掉 MySQL 上双引号被当作字符串字面量导致的读表失败。
        quoted = engine.dialect.identifier_preparer.quote(table_name)
        try:
            df = pd.read_sql_query(
                text(f"SELECT * FROM {quoted} LIMIT :limit"),
                engine,
                params={"limit": int(limit)},
            )
        except Exception as e:
            return {"ok": False, "error": f"读表失败：{type(e).__name__}: {e}"}

        df = _infer_types(df)
        # 单条件参数与 filters 并存（AND 语义）：旧签名保持可用，
        # filters 是给「一张宽表同时固定四五列」准备的（见 docstring）。
        conds: list[dict] = []
        if filter_col:
            conds.append({"col": filter_col, "op": filter_op or "eq", "value": filter_value})
        for cond in filters or []:
            if not isinstance(cond, dict) or not cond.get("col"):
                return {
                    "ok": False,
                    "error": (
                        "filters 的每一项都必须是形如 "
                        "{'col': 列名, 'op': 比较符, 'value': 取值} 的对象，"
                        f"收到 {cond!r}。"
                    ),
                }
            conds.append({
                "col": str(cond["col"]),
                "op": str(cond.get("op") or "eq"),
                "value": cond.get("value"),
            })

        applied: list[dict] = []
        for cond in conds:
            if cond["col"] not in df.columns:
                return {
                    "ok": False,
                    "error": f"列 {cond['col']} 不存在，可用列：{list(df.columns)}。",
                }
            mask = _mask_for(df[cond["col"]], cond["op"], cond["value"])
            if mask is None:
                return {
                    "ok": False,
                    "error": (
                        f"过滤方式 {cond['op']!r} / 取值 {cond['value']!r} 无法应用"
                        f"（列 {cond['col']}），可用：{_OP_HELP}。"
                    ),
                }
            df = df[mask.fillna(False)]
            # 逐条件记录剩余行数：筛空时一眼看出是哪个条件筛没的
            applied.append({**cond, "remaining": int(len(df))})

        df_id = _register(df)
        sample = _rows_json(df, 5)
        return {
            "ok": True,
            "df_id": df_id,
            "columns": sample["columns"],
            "row_count": int(len(df)),
            "sample_rows": sample["rows"],
            "filters_applied": applied,
        }

    @tool
    def group_aggregate(
        df_id: int,
        group_by: list,
        agg_col: str,
        agg_funcs: list,
        date_col: str = "",
        date_freq: str = "month",
    ) -> dict:
        """对 df_id 对应的表做分组聚合，得到每个分组的统计指标。

        - group_by：分组列名列表（如 ["coffee_name"]）
        - agg_col：要聚合的数值列（如 "money"）
        - agg_funcs：聚合函数列表，取值 count/sum/avg/min/max；其中 count 表示按行计数，即“销量/数量/笔数”
        - date_col：可选，按时间分组时的时间列名（如 "date"）
        - date_freq：时间粒度，day/week/month/quarter/year（默认 month）

        结果会注册为一个新的 df_id 并返回，供 correlate / mom_growth / share 等算子继续使用。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table。"}
        bad = [f for f in agg_funcs if f not in _AGG_FUNCS]
        if bad:
            return {"ok": False, "error": f"不支持的聚合函数 {bad}，可用：{sorted(_AGG_FUNCS)}。"}

        df = df.copy()
        group_keys = [g for g in group_by if g in df.columns]

        # 时间粒度：把日期列降采样为 period/年份，作为分组键
        if date_col and date_col in df.columns:
            # 无法识别的粒度必须显式报错：早前 else 分支吞掉一切其它值（含 "day"），
            # 日粒度查询被静默按**月度**聚合 —— 数值错误但形态合理，极难察觉。
            if date_freq not in _DATE_FREQ_PERIOD:
                return {
                    "ok": False,
                    "error": f"不支持的时间粒度 {date_freq!r}，可用：{sorted(_DATE_FREQ_PERIOD)}。",
                }
            try:
                dt = _to_datetime(df[date_col])
            except Exception:
                dt = None
            if dt is not None:
                df[date_col] = _apply_date_freq(dt, date_freq)
                group_keys.append(date_col)

        if agg_col not in df.columns:
            return {"ok": False, "error": f"聚合列 {agg_col} 不存在，可用列：{list(df.columns)}。"}
        df[agg_col] = pd.to_numeric(df[agg_col], errors="coerce")

        agg_specs: dict[str, Any] = {}
        for f in agg_funcs:
            agg_specs[f"{f}_{agg_col}"] = (agg_col, _PANDAS_AGG[f])

        try:
            grouped = df.groupby(group_keys, dropna=False).agg(**agg_specs).reset_index()
        except Exception as e:
            return {"ok": False, "error": f"分组聚合失败：{type(e).__name__}: {e}"}

        new_id = _register(grouped)
        summary = _rows_json(grouped, MAX_ROWS)
        return {
            "ok": True,
            "df_id": new_id,
            "columns": summary["columns"],
            "row_count": int(len(grouped)),
            "rows": summary["rows"],
        }

    @tool
    def group_correlate(df_id: int, group_col: str, x_col: str, y_col: str) -> dict:
        """对 df_id 对应的表按 group_col 分组，计算每组内 x_col 与 y_col 的皮尔逊相关系数。

        典型用法：先 group_aggregate 得到“每品类每月”的 avg(金额) 与 count(销量)，
        再对每个品类求二者相关性，判断正相关（corr > 0）还是负相关（corr < 0）。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        for c in (group_col, x_col, y_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        df = df.copy()
        df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
        df[y_col] = pd.to_numeric(df[y_col], errors="coerce")

        results: dict[str, Any] = {}
        for g, sub in df.groupby(group_col, dropna=False):
            xs = pd.to_numeric(sub[x_col], errors="coerce")
            ys = pd.to_numeric(sub[y_col], errors="coerce")
            mask = xs.notna() & ys.notna()
            xs, ys = xs[mask], ys[mask]
            n = int(len(xs))
            if n < 2:
                results[str(g)] = {"corr": None, "n": n, "note": "样本不足，无法计算相关性"}
            else:
                c = float(xs.corr(ys))
                results[str(g)] = {"corr": c, "n": n}
        # 每组一行的 df_id：供 intersect_groups 做多条件求交（见 _group_metric_frame）
        return {
            "ok": True,
            "group_col": group_col,
            "results": results,
            "df_id": _register(_group_metric_frame(results, group_col)),
        }

    @tool
    def group_mom_growth(
        df_id: int, group_col: str, value_col: str, date_col: str, n: int = 3
    ) -> dict:
        """对 df_id 对应的表按 group_col 分组，每组按 date_col 排序取最近 n 期 value_col，
        判断是否「连续环比增长」（严格逐期递增）。

        返回每组的 recent_values（最近 n 期取值）与 is_continuous_growth（是否连续增长）。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        for c in (group_col, value_col, date_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        df = df.copy()
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")

        results: dict[str, Any] = {}
        for g, sub in df.groupby(group_col, dropna=False):
            # 按时间列排序再看最近 n 期；年份列（含整数年份）由 _to_datetime 正确处理
            order = _to_datetime(sub[date_col]).sort_values().index
            sub = sub.loc[order]
            vals = [float(v) for v in sub[value_col].dropna().tail(n)]
            growth = len(vals) >= 2 and all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))
            results[str(g)] = {
                "recent_values": vals,
                "is_continuous_growth": bool(growth),
            }
        # 每组一行的 df_id：供 intersect_groups 做多条件求交（见 _group_metric_frame）
        return {
            "ok": True,
            "group_col": group_col,
            "n": int(n),
            "results": results,
            "df_id": _register(_group_metric_frame(results, group_col)),
        }

    @tool
    def group_share(df_id: int, group_col: str, value_col: str) -> dict:
        """对 df_id 对应的表按 group_col 分组求 value_col 的占比（占全体总和的比例），
        并判断每组是否高于平均占比（1/组数）。

        返回每组的 value（合计）、share（占比）、above_average（是否高于平均）。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        for c in (group_col, value_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        df = df.copy()
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
        grouped = df.groupby(group_col, dropna=False)[value_col].sum()
        total = float(grouped.sum())
        n_groups = int(len(grouped))
        avg_share = (1.0 / n_groups) if n_groups else 0.0

        results: dict[str, Any] = {}
        for g, v in grouped.items():
            share = (float(v) / total) if total else 0.0
            results[str(g)] = {
                "value": float(v),
                "share": share,
                "above_average": bool(share > avg_share),
            }
        return {
            "ok": True,
            "group_col": group_col,
            "total": total,
            "average_share": avg_share,
            "results": results,
            # 每组一行的 df_id：供 intersect_groups 做多条件求交（见 _group_metric_frame）
            "df_id": _register(_group_metric_frame(results, group_col)),
        }

    @tool
    def group_mom_growth_rate(
        df_id: int,
        group_col: str,
        value_col: str,
        date_col: str,
        date_freq: str = "month",
        rank_by: str = "last",
    ) -> dict:
        """按 group_col 分组、date_col 排序，算每组 value_col 的**环比增长率**并排名。

        - date_freq：时间粒度 day/week/month/quarter/year（默认 month）
        - rank_by："last" 看最后一期的环比（默认，即「最近一期谁增长最快」）；
          "mean" 看各期环比的均值（整段区间的平均增速）

        返回每组 growth_rate（**小数**，0.15 表示 +15%）与 rank（1 = 增长最快），
        以及按增长率降序的 ranking 名单。

        与 group_mom_growth 的分工：那个只回答「是否**连续**增长」（布尔），
        这个回答「增长了多少 / 谁增长最快 / 排第几」——「环比增长最快」类问题缺的是后者。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        if date_freq not in _DATE_FREQ_PERIOD:
            return {"ok": False, "error": f"不支持的时间粒度 {date_freq!r}，可用：{sorted(_DATE_FREQ_PERIOD)}。"}
        if rank_by not in ("last", "mean"):
            return {"ok": False, "error": f"不支持的 rank_by {rank_by!r}，可用：['last', 'mean']。"}
        for c in (group_col, value_col, date_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        df = df.copy()
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
        dt = _to_datetime(df[date_col])
        if not dt.notna().any():
            return {"ok": False, "error": f"列 {date_col} 无法解析为日期，无法计算环比。"}
        df["_period"] = _apply_date_freq(dt, date_freq)

        # 先按 (组, 期) 汇总：传进来的可能是原始流水（要按月求和），
        # 也可能是 group_aggregate 已经算好的每组每期一行（此时求和 = 原值，无副作用）。
        series = df.groupby([group_col, "_period"], dropna=False)[value_col].sum()

        results: dict[str, Any] = {}
        for g, sub in series.groupby(level=0, dropna=False):
            vals = sub.droplevel(0).sort_index().astype(float)
            periods = [str(p) for p in vals.index]
            if len(vals) < 2:
                results[str(g)] = {
                    "growth_rate": None,
                    "periods": periods,
                    "values": [float(v) for v in vals],
                    "note": "期数不足（<2），无法计算环比",
                }
                continue
            # 手工算 (本-上)/上 而不是 pct_change：上期为 0 时 pct_change 会给出 inf，
            # inf 一旦流进排序，这一组会以「增长无穷大」稳居第一 —— 静默的错误答案。
            prev = vals.shift(1)
            with np.errstate(divide="ignore", invalid="ignore"):
                rates = (vals - prev) / prev.replace(0, np.nan)
            valid = rates.dropna()
            rate = None if valid.empty else float(valid.iloc[-1] if rank_by == "last" else valid.mean())
            results[str(g)] = {
                "growth_rate": rate,
                "growth_rate_pct": None if rate is None else round(rate * 100.0, 4),
                "periods": periods,
                "values": [float(v) for v in vals],
                "growth_rates": [None if pd.isna(r) else float(r) for r in rates],
            }

        # 排名：growth_rate 为 None 的组排在最后（无法比较，不能当作 0）
        ranking = sorted(
            (g for g in results if results[g]["growth_rate"] is not None),
            key=lambda g: results[g]["growth_rate"],
            reverse=True,
        )
        for i, g in enumerate(ranking, start=1):
            results[g]["rank"] = i
        return {
            "ok": True,
            "group_col": group_col,
            "rank_by": rank_by,
            "date_freq": date_freq,
            "results": results,
            "ranking": ranking,
            # 每组一行的 df_id：供 intersect_groups 做多条件求交（见 _group_metric_frame）
            "df_id": _register(
                _group_metric_frame(
                    {g: {"growth_rate": v["growth_rate"], "rank": v.get("rank")} for g, v in results.items()},
                    group_col,
                )
            ),
        }

    @tool
    def group_cagr(
        df_id: int,
        group_col: str,
        value_col: str,
        date_col: str,
        start: str = "",
        end: str = "",
        date_freq: str = "year",
        method: str = "cagr",
    ) -> dict:
        """按 group_col 分组，算每组在 [start, end] 区间内 value_col 的**年均增长率**。

        - start / end：区间边界（**含**端点），按 date_freq 的粒度给出 ——
          date_freq="year" 时形如 start="2015"、end="2020"；留空表示该侧不设边界
        - date_freq：时间粒度 day/week/month/quarter/year（默认 year）。
          算「年均增长率」就把它设为 year。
        - method：cagr（复合年均增长率，默认，``(末值/首值)^(1/期数) - 1``）或
          aagr（各期增长率的**算术平均**）。「年均增长率」这句中文两种读法都常见，
          故都提供；不确定时先用 cagr。

        返回每组 growth_rate（**小数**，0.3 表示年均 +30%）、growth_rate_pct、
        periods_count、start_value / end_value（区间首末值）、values（各期值）、
        periods（各期标签），以及按增长率降序的 ranking 与 rank（1 = 增长最快）。

        与 group_mom_growth_rate 的分工：那个是**环比**（相邻两期、不年化），
        问「2015-2020 年间年均增长率是多少」必须用这个 —— 拿环比算会把 5 年的
        总增速当成一年的。区间内期数不足（<2）、首值或末值非正时，该组
        growth_rate 为 None 并附 note（零基数/负基数的复合增长率没有定义，
        **绝不返回 inf**，否则排序时它会以「增长无穷大」稳居第一）。

        末值同时以 end_value 出现在返回里 —— 「末年保有量超过 X」这类条件直接拿它
        配合 intersect_groups 筛（``{"col": "end_value", "op": ">", "value": 100000}``），
        不必再单独跑一次算子。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        if date_freq not in _DATE_FREQ_PERIOD:
            return {"ok": False, "error": f"不支持的时间粒度 {date_freq!r}，可用：{sorted(_DATE_FREQ_PERIOD)}。"}
        if method not in ("cagr", "aagr"):
            return {"ok": False, "error": f"不支持的方法 {method!r}，可用：['cagr', 'aagr']。"}
        for c in (group_col, value_col, date_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        df = df.copy()
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
        dt = _to_datetime(df[date_col])
        if not dt.notna().any():
            return {"ok": False, "error": f"列 {date_col} 无法解析为日期，无法计算年均增长率。"}
        # 期值统一成字符串再比较区间：year 粒度是整数年（"2015"），其余是
        # "2020-01"/"2020Q1" 这类 period 串 —— 两种形态下字典序都与时间顺序一致。
        df["_period"] = _apply_date_freq(dt, date_freq).astype(str)

        lo, hi = str(start or "").strip(), str(end or "").strip()
        if lo:
            df = df[df["_period"] >= lo]
        if hi:
            df = df[df["_period"] <= hi]
        if df.empty:
            return {
                "ok": False,
                "error": (
                    f"区间 [{start or '-∞'}, {end or '+∞'}] 内没有任何数据，"
                    "请放宽区间或核对 date_freq 与取值形态（year 用 '2015'，month 用 '2015-01'）。"
                ),
            }

        # 先按 (组, 期) 汇总：传进来的可能是原始流水（要按年求和），
        # 也可能是 group_aggregate 已经算好的每组每期一行（此时求和 = 原值，无副作用）。
        series = df.groupby([group_col, "_period"], dropna=False)[value_col].sum()

        results: dict[str, Any] = {}
        for g, sub in series.groupby(level=0, dropna=False):
            vals = sub.droplevel(0).sort_index().astype(float)
            first_val = float(vals.iloc[0])
            last_val = float(vals.iloc[-1])
            entry: dict[str, Any] = {
                "growth_rate": None,
                "growth_rate_pct": None,
                "periods_count": max(len(vals) - 1, 0),
                "start_value": first_val,
                "end_value": last_val,
                "periods": [str(p) for p in vals.index],
                "values": [float(v) for v in vals],
            }
            if len(vals) < 2:
                entry["note"] = "区间内期数不足（<2），无法计算增长率"
            elif first_val <= 0 or last_val <= 0:
                # 复合增长率在非正基数上没有定义（负值、从 0 起步都会让幂运算出 nan）
                entry["note"] = "区间首值或末值非正，增长率无定义"
            else:
                span = len(vals) - 1
                if method == "cagr":
                    rate: float | None = (last_val / first_val) ** (1.0 / span) - 1.0
                else:
                    prev = vals.shift(1)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        rates = ((vals - prev) / prev.replace(0, np.nan)).dropna()
                    rate = float(rates.mean()) if len(rates) else None
                if rate is None:
                    entry["note"] = "各期增长率均无定义，无法计算"
                else:
                    entry["growth_rate"] = float(rate)
                    entry["growth_rate_pct"] = round(float(rate) * 100.0, 4)
            results[str(g)] = entry

        # 排名：growth_rate 为 None 的组排在最后（无法比较，不能当作 0）
        ranking = sorted(
            (g for g in results if results[g]["growth_rate"] is not None),
            key=lambda g: results[g]["growth_rate"],
            reverse=True,
        )
        for i, g in enumerate(ranking, start=1):
            results[g]["rank"] = i
        return {
            "ok": True,
            "group_col": group_col,
            "value_col": value_col,
            "date_col": date_col,
            "date_freq": date_freq,
            "method": method,
            "start": lo,
            "end": hi,
            "results": results,
            "ranking": ranking,
            # 每组一行的 df_id：供 intersect_groups 做多条件求交。
            # 带上首末值 —— 「末年水平超过 X」正是这类多步题的另一个分句。
            "df_id": _register(
                _group_metric_frame(
                    {
                        g: {
                            "growth_rate": v["growth_rate"],
                            "start_value": v["start_value"],
                            "end_value": v["end_value"],
                            "rank": v.get("rank"),
                        }
                        for g, v in results.items()
                    },
                    group_col,
                )
            ),
        }

    @tool
    def group_vs_overall_avg(
        df_id: int, group_col: str, value_col: str, agg: str = "avg"
    ) -> dict:
        """按 group_col 分组算 value_col 的指标，再与**全体组的同一指标均值**比较。

        - agg：每组先算哪个指标 —— avg（客单价，默认）/ sum（合计）/ count（销量、笔数）/
          min / max

        返回 overall_avg（全体组的指标均值）与每组的 value、diff（与均值之差）、
        diff_pct（相对均值高/低百分之几）、above_average（是否高于整体平均）。

        与 group_share 的分工：那个比的是**占比**（share vs 1/组数，回答「占比高于平均」），
        这个比的是**水平值**（每组指标 vs 全体组均值）——
        「客单价高于全品类平均」「保有量高于平均值」需要的是后者，用 group_share 会答错。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        if agg not in _AGG_FUNCS:
            return {"ok": False, "error": f"不支持的聚合函数 {agg!r}，可用：{sorted(_AGG_FUNCS)}。"}
        for c in (group_col, value_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        df = df.copy()
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
        grouped = df.groupby(group_col, dropna=False)[value_col].agg(_PANDAS_AGG[agg])
        # 无偏均值：全体组各自指标的算术平均。注意它**不等于**总体均值
        # （后者是加权平均），问的是「哪些组的水平高于各组的平均水平」时用这个才对。
        overall = float(grouped.mean()) if len(grouped) else 0.0

        results: dict[str, Any] = {}
        for g, v in grouped.items():
            v = float(v)
            diff = v - overall
            results[str(g)] = {
                "value": v,
                "diff": diff,
                "diff_pct": round(diff / overall * 100.0, 4) if overall else None,
                "above_average": bool(v > overall),
            }
        return {
            "ok": True,
            "group_col": group_col,
            "agg": agg,
            "overall_avg": overall,
            "results": results,
            "df_id": _register(_group_metric_frame(results, group_col)),
        }

    @tool
    def intersect_groups(df_ids: list, group_col: str, conditions: list) -> dict:
        """把若干「每组一行」的算子结果按 group_col 合并，求**同时满足全部条件**的组。

        - df_ids：要合并的 df_id 列表，取自 group_aggregate / group_share /
          group_correlate / group_mom_growth / group_mom_growth_rate / group_cagr /
          group_vs_overall_avg 的返回值（每个都是一组一行的指标表）
        - conditions：条件列表，每项形如 ``{"col": "corr", "op": ">", "value": 0}``
          或 ``{"col": "coffee_name", "op": "contains", "value": "Latte"}``；
          op 可用 > >= < <= == != contains not_contains in（也认 gt/ge/le/lt/eq/ne）
        - **列对列**比较：``value`` 也可以写成列引用
          ``{"col": "increment", "factor": 0.5}``（可选 ``offset``），表示与**另一列**
          比较，即 ``本列 <op> 该列 * factor + offset``。
          跨两张指标表合并时，**第二个表的同名列会带上自己的 df_id 后缀**
          （df_id=7 那组的 growth_rate 合并后叫 ``growth_rate_7``），真实列名见返回的 columns。

        典型用法（多步检索的多条件交集题）：「哪些品类同时满足现金支付环比增长最快、
        客单价高于全品类平均、且销售额占比高于平均」—— 把三个算子各自算成一张每组一行的
        指标表，再一次性求交。返回 matched（满足全部条件的组名）与 per_condition
        （每个条件各自筛出几组、筛出谁），后者用于核对是哪个条件把结果筛空的。

        「预测期增长率高于历史均值」「增量超过历史总增量 50%」这类**同一地区两段区间
        互比**的题，只能靠列引用表达（把两段区间各算一张 group_cagr 指标表，再比）。
        """
        if not df_ids:
            return {"ok": False, "error": "df_ids 不能为空。"}
        if not conditions:
            return {"ok": False, "error": "conditions 不能为空。"}

        merged: pd.DataFrame | None = None
        for did in df_ids:
            frame = _get(int(did))
            if frame is None:
                return {"ok": False, "error": f"df_id={did} 不存在。"}
            if group_col not in frame.columns:
                return {
                    "ok": False,
                    "error": f"df_id={did} 缺少分组列 {group_col}，其可用列：{list(frame.columns)}。",
                }
            if merged is None:
                merged = frame.copy()
            else:
                # 同名指标列（如两个算子都叫 value）保留首个，后者加 _<df_id> 后缀，
                # 并在返回里给出 columns 让模型看到真实列名 —— 不返回就会照旧名去筛，条件静默失效。
                merged = merged.merge(
                    frame, on=group_col, how="outer", suffixes=("", f"_{did}")
                )

        names_all = merged[group_col].astype(str)
        matched = set(names_all)
        per_condition: list[dict] = []
        for cond in conditions:
            col = str(cond.get("col") or "")
            op = str(cond.get("op") or "==")
            value = cond.get("value")
            if col not in merged.columns:
                return {
                    "ok": False,
                    "error": f"条件列 {col} 不在合并后的列里，可用列：{list(merged.columns)}。",
                }
            if isinstance(value, dict):
                # 列对列：{"col": "increment", "factor": 0.5} → 本列 > increment * 0.5。
                # 「增量超过历史总增量 50%」这类**两段区间互比**只能这样表达；
                # 早前 conditions 只收标量，模型会反复试、反复被拒（白烧轮次）。
                ref = str(value.get("col") or "")
                if ref not in merged.columns:
                    return {
                        "ok": False,
                        "error": f"条件 {cond} 引用的列 {ref} 不在合并后的列里，可用列：{list(merged.columns)}。",
                    }
                try:
                    factor = float(value.get("factor", 1))
                    offset = float(value.get("offset", 0))
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"条件 {cond} 的 factor/offset 必须是数字。"}
                mask = _mask_for_pair(merged[col], op, merged[ref], factor, offset)
                if mask is None:
                    return {
                        "ok": False,
                        "error": (
                            f"条件 {cond} 无法应用（列 {col} 与列 {ref} 比较），"
                            "可用比较符：> >= < <= == !=。"
                        ),
                    }
            else:
                mask = _mask_for(merged[col], op, value)
                if mask is None:
                    return {
                        "ok": False,
                        "error": f"条件 {cond} 无法应用（列 {col}），可用比较符：{_OP_HELP}。",
                    }
            mask = mask.fillna(False)
            hit = names_all[mask].tolist()
            per_condition.append(
                {"col": col, "op": op, "value": value, "count": len(hit), "matched": hit[:MAX_ROWS]}
            )
            matched &= set(hit)

        return {
            "ok": True,
            "group_col": group_col,
            "columns": [str(c) for c in merged.columns],
            "group_count": int(len(merged)),
            "matched_count": len(matched),
            # 按合并表的原始顺序输出，便于与输入表对照
            "matched": [g for g in names_all.tolist() if g in matched],
            "per_condition": per_condition,
        }

    @tool
    def linear_regression(df_id: int, x_col: str, y_col: str) -> dict:
        """对 df_id 对应的表做一元线性回归（y ~ x），返回斜率/截距/拟合优度 R² 与相关系数 r。

        典型用法：量化某列随另一列（或时间下标）变化的趋势方向与强度，也可作为时序预测的趋势基础。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        for c in (x_col, y_col):
            if c not in df.columns:
                return {"ok": False, "error": f"列 {c} 不存在，可用列：{list(df.columns)}。"}

        xs = pd.to_numeric(df[x_col], errors="coerce")
        ys = pd.to_numeric(df[y_col], errors="coerce")
        mask = xs.notna() & ys.notna()
        xs = xs[mask].to_numpy(dtype=float)
        ys = ys[mask].to_numpy(dtype=float)
        n = int(len(xs))
        if n < 2:
            return {"ok": False, "error": "有效样本不足（<2），无法回归。"}

        slope, intercept = np.polyfit(xs, ys, 1)
        y_pred = slope * xs + intercept
        ss_res = float(np.sum((ys - y_pred) ** 2))
        ss_tot = float(np.sum((ys - ys.mean()) ** 2))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        r = float(np.corrcoef(xs, ys)[0, 1])
        return {
            "ok": True,
            "slope": float(slope),
            "intercept": float(intercept),
            "r2": float(r2),
            "r": float(r),
            "n": n,
        }

    @tool
    def forecast_time_series(
        df_id: int,
        date_col: str,
        value_col: str,
        horizon: int = 1,
        method: str = "linear",
        group_col: str = "",
    ) -> dict:
        """对 df_id 对应的表做时序外推预测（向前 horizon 期）。

        - date_col：时间/顺序列，按它排序后取 value_col 的历史序列（可为空字符串，用行序）
        - value_col：要预测的数值列
        - horizon：向前预测期数（默认 1）
        - method：linear（线性趋势外推，默认）/ average（滚动移动平均）/ growth_rate（复合增长率外推）
        - group_col：可选，按某列分组分别预测（如按品类/国家/地区）

        返回 predictions（预测值列表，按时间先后）与拟合信息；有 group_col 时返回 results 映射。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table / group_aggregate。"}
        if value_col not in df.columns:
            return {"ok": False, "error": f"列 {value_col} 不存在，可用列：{list(df.columns)}。"}
        if date_col and date_col not in df.columns:
            return {"ok": False, "error": f"列 {date_col} 不存在，可用列：{list(df.columns)}。"}
        if group_col and group_col not in df.columns:
            return {"ok": False, "error": f"列 {group_col} 不存在，可用列：{list(df.columns)}。"}
        if method not in ("linear", "average", "growth_rate"):
            return {"ok": False, "error": f"不支持的预测方法 {method}，可用：linear/average/growth_rate。"}
        horizon = max(1, int(horizon))

        df = df.copy()
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")

        def _series(sub: pd.DataFrame):
            if date_col:
                order = _to_datetime(sub[date_col]).sort_values().index
                sub = sub.loc[order]
            return sub[value_col].to_numpy(dtype=float)

        if group_col:
            results: dict[str, Any] = {}
            for g, sub in df.groupby(group_col, dropna=False):
                results[str(g)] = _forecast_series(_series(sub), method, horizon)
            return {
                "ok": True,
                "group_col": group_col,
                "method": method,
                "horizon": horizon,
                "results": results,
            }

        out = _forecast_series(_series(df), method, horizon)
        out.update({"ok": True, "method": method, "horizon": horizon})
        return out

    @tool
    def detect_anomalies(
        df_id: int, value_col: str, method: str = "zscore", threshold: float = 3.0
    ) -> dict:
        """检测 df_id 对应表中 value_col 的异常值（离群点）。

        - method：zscore（标准分数法，默认，|z| > threshold 判异常，threshold 默认 3 即 3σ）或
          iqr（箱线法，超出 [Q1-1.5*IQR, Q3+1.5*IQR] 判异常，忽略 threshold）
        - threshold：zscore 法的阈值

        返回异常数量、异常值列表与判定边界/阈值。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table。"}
        if value_col not in df.columns:
            return {"ok": False, "error": f"列 {value_col} 不存在，可用列：{list(df.columns)}。"}
        s = pd.to_numeric(df[value_col], errors="coerce").dropna().to_numpy(dtype=float)
        if len(s) < 2:
            return {"ok": False, "error": "有效样本不足，无法检测异常。"}

        if method == "iqr":
            q1, q3 = float(np.percentile(s, 25)), float(np.percentile(s, 75))
            iqr = q3 - q1
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            mask = (s < lo) | (s > hi)
            anomalies = [float(v) for v in s[mask]]
            bound = {"lower": float(lo), "upper": float(hi)}
            thr = None
        else:  # zscore（默认）
            mean, std = float(np.mean(s)), float(np.std(s, ddof=1))
            if std == 0:
                return {
                    "ok": True,
                    "n_anomalies": 0,
                    "anomalies": [],
                    "method": method,
                    "threshold": float(threshold),
                    "note": "标准差为 0，无离群值",
                }
            z = (s - mean) / std
            mask = np.abs(z) > threshold
            anomalies = [float(v) for v in s[mask]]
            bound = None
            thr = float(threshold)

        return {
            "ok": True,
            "n_anomalies": int(len(anomalies)),
            "anomalies": anomalies[:MAX_ROWS],
            "method": method,
            "threshold": thr,
            "bound": bound,
            "n": int(len(s)),
        }

    @tool
    def cluster_kmeans(
        df_id: int, value_cols: list, k: int = 3, max_iter: int = 50, seed: int = 0
    ) -> dict:
        """对 df_id 对应的表按 value_cols（若干数值列）做 K-Means 聚类（数据挖掘）。

        - value_cols：用于聚类的数值列名列表
        - k：簇数（默认 3）
        - max_iter：最大迭代次数（默认 50）
        - seed：随机种子（固定值保证结果可复现）

        返回每簇样本数与质心，并把簇标签写入新列 __cluster、注册为新 df_id，
        供后续 group_aggregate 按 __cluster 聚合。
        """
        df = _get(df_id)
        if df is None:
            return {"ok": False, "error": f"df_id={df_id} 不存在，请先 load_table。"}
        missing = [c for c in value_cols if c not in df.columns]
        if missing:
            return {"ok": False, "error": f"列 {missing} 不存在，可用列：{list(df.columns)}。"}
        if not value_cols:
            return {"ok": False, "error": "value_cols 不能为空。"}

        X = df[value_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        mask = ~np.isnan(X).any(axis=1)
        X = X[mask]
        n = int(len(X))
        if n == 0:
            return {"ok": False, "error": "无有效数值样本，无法聚类。"}
        k = min(int(k), n)

        # KMeans++ 初始化（固定种子）
        rng = np.random.default_rng(seed)
        centers = [X[int(rng.integers(n))]]
        for _ in range(k - 1):
            d2 = np.min([np.sum((X - c) ** 2, axis=1) for c in centers], axis=0)
            d2 = np.maximum(d2, 1e-12)
            idx = rng.choice(n, p=d2 / d2.sum())
            centers.append(X[idx])
        centers = np.array(centers)

        labels = np.zeros(n, dtype=int)
        for _ in range(max_iter):
            dist = np.array([np.sum((X - c) ** 2, axis=1) for c in centers])
            new_labels = np.argmin(dist, axis=0)
            new_centers = []
            for j in range(k):
                pts = X[new_labels == j]
                new_centers.append(pts.mean(axis=0) if len(pts) else X[int(rng.integers(n))])
            new_centers = np.array(new_centers)
            if np.array_equal(new_labels, labels) or np.allclose(new_centers, centers):
                centers, labels = new_centers, new_labels
                break
            centers, labels = new_centers, new_labels

        cluster_sizes = {str(j): int((labels == j).sum()) for j in range(k)}
        centroids = [[float(v) for v in c] for c in centers]
        inertia = float(np.sum((X - centers[labels]) ** 2))

        tagged = df.copy()
        tagged["__cluster"] = -1
        tagged.loc[mask, "__cluster"] = labels
        new_id = _register(tagged)

        return {
            "ok": True,
            "df_id": new_id,
            "k": k,
            "cluster_sizes": cluster_sizes,
            "centroids": centroids,
            "inertia": inertia,
            "columns": [str(c) for c in value_cols],
        }

    # 新增算子务必同步登记在这里 —— ``@tool`` 只是包装，不登记就等于不存在，
    # 而模型只会拿到这个列表，它连「有这个工具」都看不到。
    return [
        get_schema,
        load_table,
        group_aggregate,
        group_correlate,
        group_mom_growth,
        group_mom_growth_rate,
        group_cagr,
        group_share,
        group_vs_overall_avg,
        intersect_groups,
        linear_regression,
        forecast_time_series,
        detect_anomalies,
        cluster_kmeans,
    ]
