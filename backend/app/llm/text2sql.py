"""text2sql：自然语言 → SELECT 语句（支持 SQLite / MySQL / PostgreSQL 方言）。

采用 few-shot 提示 + sqlglot 语法校验 + 失败自动重试。
不设规则兜底：模型不可用或多次校验失败时抛出 AppError，由上层转为友好的系统错误提示。
"""
import re

import sqlglot
from sqlglot import exp

from app.errors import AppError
from app.llm.client import get_llm

# 通用 few-shot 示例：使用抽象表名/列名（t/category/value/date_col 均为占位），
# 仅演示 SQL 结构与函数写法，不绑定任何具体业务表（实际表名/列名以注入的表结构为准）。
# 通用示例跨方言复用；日期函数相关示例按方言单独给出（strftime / DATE_FORMAT / to_char）。
_COMMON_FEW_SHOT = [
    (
        "统计各分类的数量",
        'SELECT category AS "分类", COUNT(*) AS "数量" '
        'FROM t GROUP BY category ORDER BY "数量" DESC',
    ),
    (
        "各分类的合计值是多少",
        'SELECT category AS "分类", SUM(value) AS "合计" '
        'FROM t GROUP BY category ORDER BY "合计" DESC',
    ),
    (
        "每个分类的平均值是多少",
        'SELECT category AS "分类", ROUND(AVG(value), 2) AS "平均值" '
        'FROM t GROUP BY category ORDER BY "平均值" DESC',
    ),
    (
        "各分类占总量的比例",
        'SELECT category AS "分类", '
        "ROUND(SUM(value) * 100.0 / (SELECT SUM(value) FROM t), 2) AS \"占比(%)\" "
        'FROM t GROUP BY category ORDER BY "占比(%)" DESC',
    ),
    (
        "数值最高的前5条记录",
        'SELECT name AS "名称", value AS "数值" '
        'FROM t ORDER BY value DESC LIMIT 5',
    ),
    (
        "某个条件下的记录数",
        'SELECT COUNT(*) AS "数量" FROM t WHERE value > 100',
    ),
]

# 多表 JOIN 示例：使用抽象表名 t1/t2/a/b/c 与列 id/category_id/b_id/c_id 占位，
# 仅演示 JOIN 的 ON 写法（ON 条件依据注入的『表关联关系』映射），不绑定具体业务表。
_JOIN_FEW_SHOT = [
    (
        "统计每个分类下的记录数量",
        'SELECT t2.name AS "分类", COUNT(*) AS "数量" '
        'FROM t1 JOIN t2 ON t1.category_id = t2.id GROUP BY t2.name ORDER BY "数量" DESC',
    ),
    (
        "查询每条记录所属分类的名称和值",
        'SELECT t1.name AS "记录", t2.name AS "分类", t1.value AS "数值" '
        'FROM t1 LEFT JOIN t2 ON t1.category_id = t2.id',
    ),
    (
        "关联三张表做统计",
        'SELECT a.name AS "名称", b.name AS "分类", c.value AS "数值" '
        'FROM a JOIN b ON a.b_id = b.id JOIN c ON b.c_id = c.id',
    ),
]

# 日期函数相关示例（按方言）：使用抽象 date_col 列，演示按月/按季度分组写法
_DATE_FEW_SHOT: dict[str, list[tuple[str, str]]] = {
    "sqlite": [
        (
            "按月份统计合计",
            'SELECT strftime(\'%Y-%m\', date_col) AS "月份", SUM(value) AS "合计" '
            'FROM t GROUP BY "月份" ORDER BY "月份"',
        ),
        (
            "按季度统计合计",
            "SELECT CASE "
            "WHEN CAST(strftime('%m', date_col) AS INTEGER) BETWEEN 1 AND 3 THEN 'Q1' "
            "WHEN CAST(strftime('%m', date_col) AS INTEGER) BETWEEN 4 AND 6 THEN 'Q2' "
            "WHEN CAST(strftime('%m', date_col) AS INTEGER) BETWEEN 7 AND 9 THEN 'Q3' "
            "ELSE 'Q4' END AS \"季度\", SUM(value) AS \"合计\" "
            'FROM t GROUP BY "季度" ORDER BY "季度"',
        ),
    ],
    "mysql": [
        (
            "按月份统计合计",
            'SELECT DATE_FORMAT(date_col, \'%Y-%m\') AS "月份", SUM(value) AS "合计" '
            'FROM t GROUP BY "月份" ORDER BY "月份"',
        ),
        (
            "按季度统计合计",
            "SELECT CONCAT('Q', QUARTER(date_col)) AS \"季度\", SUM(value) AS \"合计\" "
            'FROM t GROUP BY "季度" ORDER BY "季度"',
        ),
    ],
    "postgres": [
        (
            "按月份统计合计",
            'SELECT to_char(date_col, \'YYYY-MM\') AS "月份", SUM(value) AS "合计" '
            'FROM t GROUP BY "月份" ORDER BY "月份"',
        ),
        (
            "按季度统计合计",
            "SELECT to_char(date_col, '\"Q\"Q') AS \"季度\", SUM(value) AS \"合计\" "
            'FROM t GROUP BY "季度" ORDER BY "季度"',
        ),
    ],
}

_DEFAULT_DIALECT = "sqlite"

# 完整 few-shot：通用示例 + JOIN 示例 + 各方言的日期示例
FEW_SHOT: dict[str, list[tuple[str, str]]] = {
    dialect: _COMMON_FEW_SHOT + _JOIN_FEW_SHOT + _DATE_FEW_SHOT[dialect]
    for dialect in _DATE_FEW_SHOT
}

_DIALECT_NAMES = {
    "sqlite": "SQLite",
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
}


def _system_prompt(dialect: str) -> str:
    """按方言构造 system prompt。"""
    name = _DIALECT_NAMES.get(dialect, "SQLite")
    # SQLite / PostgreSQL 用双引号包裹标识符，MySQL 用反引号
    quote_hint = "双引号" if dialect in ("sqlite", "postgres") else "反引号"
    return (
        f"你是一名 {name} 数据分析专家。根据给定的表结构和示例，"
        f"将用户的中文问题转换为一条 {name} SELECT 查询语句。\n"
        "要求：\n"
        "1. 只输出一条 SELECT 语句，不要任何解释、注释或多余文字；\n"
        "2. 语句结尾不要加分号；\n"
        "3. 中文别名使用双引号包裹，如 AS \"分类\"；\n"
        "4. 严格依据『数据库表结构』里的真实表名与字段名；示例仅演示 SQL 结构与函数写法，"
        "其中的 t/category/value/date_col 均为占位，不得照抄；\n"
        "5. 引用真实表名/列名时必须原样照抄（含引号）；"
        f"名字含中文、空格或以数字开头时用{quote_hint}包裹（表结构里已按此格式给出）；\n"
        f"6. 严格使用 {name} 方言的日期/字符串函数，禁止混用其它数据库的函数；\n"
        "7. 涉及多表时使用 JOIN 连接，ON 条件依据『表关联关系』注释中的映射，"
        "禁止生成没有关联条件的笛卡尔积；\n"
        "8. WHERE / HAVING 里的**取值必须来自表结构里给出的真实取值**（『全部数据』『样本』"
        "『全部取值』等注释行中的字面量），原样照抄，不得翻译、改写或杜撰；"
        "表结构里没有给出所需取值时，**不要猜一个值**——先写一条不带该条件的查询"
        "（必要时 SELECT * 或 SELECT DISTINCT 该列）把全表拉出来，交由后续判断。"
    )


MAX_RETRY = 3


def extract_sql(text: str) -> str:
    """从模型输出中提取纯 SQL：剥离围栏、首尾空白、末尾分号。"""
    text = (text or "").strip()
    # 剥离 ```sql ... ``` 或 ``` ... ``` 围栏
    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if m:
        text = m.group(1).strip()
    return text.rstrip(";").strip()


# 只读查询的合法根节点（含集合运算，见下）
_READ_ROOT_TYPES = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,
    exp.Values,
    exp.With,
)

# 任何位置出现即判为写操作（含 CTE 内部嵌套，必须递归检查）
_WRITE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Command,
    exp.TruncateTable,
)


def _validate_sql(sql: str, dialect: str = "sqlite") -> None:
    """校验 SQL：可解析且**整条语句只读**。失败抛 ValueError。

    只查根节点是不够的：``WITH del AS (DELETE FROM t RETURNING id) SELECT * FROM del``
    的根节点是 Select，写操作藏在 CTE 里，PostgreSQL 上会真的执行删除。
    因此这里做两件事——根节点须是只读查询类型（放行 UNION/INTERSECT/EXCEPT 等集合运算），
    再递归遍历全部节点，命中任何写操作即拒。
    """
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as e:
        raise ValueError(f"SQL 无法解析：{e}") from e

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        # 多语句（如 SELECT 1; DROP TABLE t）解析为多个根节点，一律拒绝
        raise ValueError("仅允许单条 SELECT 查询语句")

    root = statements[0]
    if not isinstance(root, _READ_ROOT_TYPES):
        raise ValueError("仅允许 SELECT 查询语句")

    # 递归检查：写操作可能嵌套在 CTE / 子查询中
    for node in root.walk():
        if isinstance(node, _WRITE_TYPES):
            raise ValueError(f"仅允许只读查询，检测到写操作：{node.key.upper()}")


# 公开别名：供 agent 工具复用只读校验（只读查询，含 UNION/INTERSECT/EXCEPT）
validate_sql = _validate_sql


def _build_prompt(question: str, schema_text: str, dialect: str = "sqlite") -> str:
    """拼接 few-shot prompt。"""
    examples = FEW_SHOT.get(dialect, FEW_SHOT[_DEFAULT_DIALECT])
    examples_text = "\n\n".join(f"问题：{q}\nSQL：{sql}" for q, sql in examples)
    return (
        f"{_system_prompt(dialect)}\n\n"
        f"===== 数据库表结构 =====\n{schema_text}\n\n"
        f"===== 示例 =====\n{examples_text}\n\n"
        f"===== 当前问题 =====\n{question}\n"
        f"请输出对应的 SQL："
    )


def _response_to_text(response) -> str:
    """把 ChatOpenAI 的响应转为字符串。

    ``content`` 可能是字符串，也可能是结构化块列表（部分模型/网关返回
    ``[{"type": "text", "text": "..."}]``）。直接 ``str()`` 会得到 Python repr
    而不是 SQL —— 提取与校验必然失败，且失败信息指向「SQL 无法解析」，
    与真实原因（解析响应格式）不符。故这里按块拼接文本。
    """
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, dict) and isinstance(text.get("value"), str):
                    parts.append(text["value"])  # {"type":"text","text":{"value": ...}} 形态
        return "".join(parts)
    return str(content)


def _retry_prompt(
    question: str, schema_text: str, dialect: str, last_sql: str, error: Exception
) -> str:
    """重试提示：带上**上一次的 SQL 原文**。

    早前只追加一句错误信息，模型看不到自己刚写了什么，重试往往原样再犯同一个错；
    把上一版 SQL 回喂后，模型才有具体的修正对象。
    """
    base = _build_prompt(question, schema_text, dialect)
    last = (last_sql or "").strip()
    if last:
        return (
            f"{base}\n你上次输出的 SQL 是：\n{last}\n\n"
            f"它校验失败：{error}。请针对该问题重写一条正确的 SELECT 语句，只输出 SQL。"
        )
    return f"{base}\n你上次没有输出可用的 SQL（{error}）。请直接输出一条正确的 SELECT 语句。"


def generate_sql(question: str, schema_text: str, dialect: str = "sqlite") -> str:
    """生成 SQL。模型不可用或多次校验失败时抛 AppError。"""
    llm = get_llm()  # 未配置 Key / 初始化失败会抛 AppError
    prompt = _build_prompt(question, schema_text, dialect)

    for attempt in range(MAX_RETRY):
        # 网络 / 超时：重试，而非立即失败
        try:
            response = llm.invoke(prompt)
        except Exception as e:
            if attempt == MAX_RETRY - 1:
                raise AppError(
                    "大模型服务暂时不可用，请稍后重试或检查网络与 API Key 配置。",
                    "LLM_UNAVAILABLE",
                ) from e
            continue

        sql = extract_sql(_response_to_text(response))

        # 语法 + 只读校验：失败把上一次 SQL 连同错误一起回喂，让模型有针对性修正
        try:
            _validate_sql(sql, dialect)
            return sql
        except Exception as e:
            prompt = _retry_prompt(question, schema_text, dialect, sql, e)

    raise AppError(
        "多次生成 SQL 均未通过校验，请换一种方式描述问题。",
        "SQL_ERROR",
    )


async def generate_sql_stream(question: str, schema_text: str, dialect: str = "sqlite"):
    """流式生成 SQL：**校验通过后**才回放 token；失败自动重试，多次失败抛 AppError。

    与早前的两处差别都是必要的：

    - 逐 token 直接 yield 会在校验**之前**就把内容推给前端，用户看到一段最终
      未通过校验、也从未执行的 SQL（展示与执行不一致）；故先缓存，通过后回放。
    - 单次失败即终止，而 ``MAX_RETRY`` 只作用于非流式的 ``generate_sql`` ——
      文档承诺的「失败自动重试」在主 SSE 链路上并不生效；故这里同样重试。
    """
    llm = get_llm()
    prompt = _build_prompt(question, schema_text, dialect)

    for attempt in range(MAX_RETRY):
        collected: list[str] = []
        try:
            async for chunk in llm.astream(prompt):
                token = _response_to_text(chunk)
                if token:
                    collected.append(token)
        except Exception as e:
            # 网络 / 超时：重试，而非立即失败
            if attempt == MAX_RETRY - 1:
                raise AppError(
                    "大模型服务暂时不可用，请稍后重试或检查网络与 API Key 配置。",
                    "LLM_UNAVAILABLE",
                ) from e
            continue

        sql = extract_sql("".join(collected))
        try:
            _validate_sql(sql, dialect)
        except Exception as e:
            prompt = _retry_prompt(question, schema_text, dialect, sql, e)
            continue

        # 校验通过才回放（此时 sql 必非空，collected 亦非空；or 分支仅作兜底）
        for token in collected or [sql]:
            yield token
        return

    raise AppError(
        "多次生成 SQL 均未通过校验，请换一种方式描述问题。",
        "SQL_ERROR",
    )