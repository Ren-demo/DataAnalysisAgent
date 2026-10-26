# 样例提问（用于客观评分）

> 内置示例库已移除，本系统现为「上传文件 / 外部数据源」驱动。以下样例提问为**通用模板**，使用抽象占位表/列名（表 `t`，列 `category` / `value` / `date_col`）演示各类 SQL 模式；实际使用时将占位符替换为目标数据源的真实表名与列名即可。
> `chart_type` 采用**严格模式**：仅当提问显式出现「柱状图 / 折线图 / 饼图」时 `chart_hint ∈ {bar,line,pie}` 才出图，否则一律 `table`，不做启发式推断。下表 `chart_type` 列为「显式指定图表类型」时的取值示例，实际未指定时默认 `table`。

| # | 样例提问 | 期望 SQL（SQLite 方言，抽象占位） | chart_type |
| --- | --- | --- | --- |
| 1 | 统计各分类的数量 | `SELECT category AS "分类", COUNT(*) AS "数量" FROM t GROUP BY category ORDER BY "数量" DESC` | bar |
| 2 | 各分类的合计值 | `SELECT category AS "分类", SUM(value) AS "合计" FROM t GROUP BY category ORDER BY "合计" DESC` | bar |
| 3 | 按月份统计合计值趋势 | `SELECT strftime('%Y-%m', date_col) AS "月份", SUM(value) AS "合计" FROM t GROUP BY "月份" ORDER BY "月份"` | line |
| 4 | 各分类占总量的比例 | `SELECT category AS "分类", ROUND(SUM(value) * 100.0 / (SELECT SUM(value) FROM t), 2) AS "占比" FROM t GROUP BY category ORDER BY "占比" DESC` | pie |
| 5 | 某个分类下的记录 | `SELECT * FROM t WHERE category = '某分类' ORDER BY date_col` | table |
| 6 | 全年合计值 | `SELECT SUM(value) AS "合计" FROM t WHERE date_col >= '2024-01-01' AND date_col < '2025-01-01'` | table |
| 7 | 数值最高的前5条记录 | `SELECT * FROM t ORDER BY value DESC LIMIT 5` | bar |
| 8 | 各分类的平均值 | `SELECT category AS "分类", ROUND(AVG(value), 2) AS "平均值" FROM t GROUP BY category` | bar |
| 9 | 某季度各分类的合计值 | `SELECT category AS "分类", SUM(value) AS "合计" FROM t WHERE date_col >= '2024-04-01' AND date_col <= '2024-06-30' GROUP BY category ORDER BY "合计" DESC` | bar |
| 10 | 各月的记录数 | `SELECT strftime('%Y-%m', date_col) AS "月份", COUNT(*) AS "记录数" FROM t GROUP BY "月份" ORDER BY "月份"` | line |

## 覆盖的能力维度

- **基础查询 / 时间过滤**：#1、#5、#6、#9（`WHERE date_col BETWEEN ...`）
- **分组聚合**：#1、#2、#3、#8、#10（`GROUP BY` + `SUM` / `AVG` / `COUNT`）
- **排序 / 限量**：#7（`ORDER BY ... LIMIT`）
- **占比 / 比例计算**：#4（子查询 + 百分比）
- **日期函数**：#3、#10（`strftime`）

> 注：多表关联（JOIN）示例需基于实际数据源的多表关系编写；系统内置的 text2sql FEW_SHOT 同样以抽象占位表/列名演示 SQL 结构与函数写法，真实表名/列名严格以 `GET /api/schema` 返回的表结构为准。
