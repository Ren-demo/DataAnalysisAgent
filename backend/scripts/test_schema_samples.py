"""取值区（让 text2sql 看得见数据）的离线自检：不连 Milvus / embedding / LLM。

**为什么需要它**（2026-10 定位到的根因）：doc 源查询链路交给 text2sql 的 schema 文本
早前**只有 DDL、零取值**。而文档抽出的表大多是「行标签 + 数值」的**透视表**，
语义全在某一列的**取值**里 —— 例如 ``col = 'less : amount representing interest'``
对应的 73512。模型没见过任何字面量，只能照中文问题猜一个不存在的值，
写出 ``WHERE col = '用于支付利息的未来最低租赁付款部分'``，**必然 0 行**；
而 0 行不会被重试，直接被当成「未查到匹配数据」返回。
取值区就是把真实取值补进 prompt（小表全量 dump、大表样本 + 文本列去重取值）。

覆盖 ``app/database/engine.py`` 的 ``_cell_text`` / ``_table_value_lines`` /
``build_schema_text(with_samples=True)`` / ``build_source_schema_text``：

1. 小表**全量** dump，且确实带上那行关键取值（本文件的回归靶子）；
2. ``with_samples=False`` 与改造前**逐字节一致**（纯 DDL，不掺任何取值行）；
3. 大表给前 N 行样本 + 文本列的**全部去重取值**（覆盖「答案行落在样本窗口之外」），
   且不给数值列（对数值列列去重值既无用又费预算）；
4. 去重值**超过上限就整个不给** —— 截断的取值清单会让模型以为「就这些」，比不给更糟；
5. 字符预算用尽即停止追加并**留痕**（末尾注明省略了几张表）—— 静默截断等于骗人；
6. 单表读值失败**只跳过该表**，不毁掉整份 schema；
7. 表数超过 ``schema_max_tables`` 就整份不附取值（防 prompt 爆炸的兜底）；
8. ``build_source_schema_text`` 的分支表：doc 源定位命中 → 收窄 + 取值；
   定位不到 → **回退全量且刻意不带取值**；非 doc 源表数有界 → 带取值。

运行：``cd backend && python scripts/test_schema_samples.py``
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import create_engine, text

import app.database.engine as eng_mod
from app.config import settings
from app.database.engine import build_schema_text, build_source_schema_text

PIVOT_TABLE = "doc_a85671194933_10"

# 改造前 build_schema_text 对这张表产出的**全部内容**（82 字符，逐字节快照）。
# 取值区必须是**纯追加**：这条断言就是「没有回归」的定义。
PIVOT_DDL_ONLY = (
    "CREATE TABLE doc_a85671194933_10 (\n"
    "  col TEXT,\n"
    '  "amount ( in thousands )" TEXT\n'
    ");"
)

INTEREST_LABEL = "less : amount representing interest"
INTEREST_VALUE = "73512"


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _new_engine():
    return create_engine("sqlite://")


def _pivot_engine():
    """与赛题 ``doc_a85671194933_10`` 同构：行标签 + 数值两列文本，9 行以内。"""
    eng = _new_engine()
    with eng.begin() as c:
        c.execute(
            text(
                f'CREATE TABLE "{PIVOT_TABLE}" ('
                '"col" TEXT, "amount ( in thousands )" TEXT)'
            )
        )
        rows = [
            ("future minimum lease payments", "$ 470,802"),
            ("less : current portion", "3 2 7 0 0"),
            (INTEREST_LABEL, INTEREST_VALUE),
        ]
        for label, val in rows:
            c.execute(
                text(f'INSERT INTO "{PIVOT_TABLE}" VALUES (:a, :b)'), {"a": label, "b": val}
            )
    return eng


def test_small_table_full_dump() -> None:
    """小表（≤ schema_full_dump_rows）给全部行 —— 关键取值必须在里面。"""
    out = build_schema_text(_pivot_engine(), [PIVOT_TABLE], with_samples=True)
    _check("小表标注为「全部数据」", f"-- 表 {PIVOT_TABLE} 共 3 行，全部数据：" in out, out)
    _check("关键行标签已进入 prompt", INTEREST_LABEL in out, out)
    _check("关键取值已进入 prompt", INTEREST_VALUE in out, out)
    _check("取值行按 DDL 列序对齐", f"-- col | \"amount ( in thousands )\"" in out, out)


def test_no_samples_byte_identical() -> None:
    """with_samples=False 与改造前逐字节一致（纯 DDL，零取值行）。"""
    out = build_schema_text(_pivot_engine(), [PIVOT_TABLE])
    _check("默认参数=纯 DDL，与改造前逐字节一致", out == PIVOT_DDL_ONLY, repr(out))
    _check("显式 with_samples=False 同上", out == build_schema_text(_pivot_engine(), [PIVOT_TABLE], with_samples=False))
    _check("纯 DDL 里不含取值区标记", "-- 表 " not in out and "-- 列 " not in out, out)


def _big_engine() -> object:
    """60 行、文本列 12 个去重值（< 上限）、另有一数值列。"""
    eng = _new_engine()
    with eng.begin() as c:
        c.execute(text('CREATE TABLE "t_big" ("label" TEXT, "n" INTEGER)'))
        for i in range(60):
            c.execute(
                text('INSERT INTO "t_big" VALUES (:a, :b)'), {"a": f"label{i % 12:02d}", "b": i}
            )
    return eng


def test_big_table_sample_plus_distinct() -> None:
    """大表：前 N 行样本 + 文本列的完整去重取值（补样本窗口盖不到的答案行）。"""
    out = build_schema_text(_big_engine(), ["t_big"], with_samples=True)
    _check(
        "大表标注为「样本（前 20 行）」",
        "-- 表 t_big 共 60 行，样本（前 20 行）：" in out,
        out,
    )
    n_rows = len(re.findall(r"^-- label\d\d \| ", out, flags=re.M))
    _check("样本行数为 20（表头行不算）", n_rows == 20, str(n_rows))
    # 列名按方言引号渲染：简单小写标识符不加引号，"amount ( in thousands )" 才加
    _check("文本列给出全部去重取值（共 12 个）", "-- 列 label 的全部取值（共 12 个）：" in out, out)
    _check("去重取值含窗口外的 label11", "label11" in out.split("全部取值")[1], out)
    _check("数值列不列去重取值", "-- 列 n " not in out, out)


def test_distinct_over_limit_skipped() -> None:
    """去重值超上限 → 整列不给（截断的清单会让模型以为「就这些」）。

    注意必须同时越过 ``schema_full_dump_rows``：行数不够时走的是**全量 dump** 分支，
    根本不会去列去重取值，断言会在错误的理由上通过。
    """
    total, distinct = settings.schema_full_dump_rows + 10, settings.schema_max_distinct_values + 10
    eng = _new_engine()
    with eng.begin() as c:
        c.execute(text('CREATE TABLE "t_wide" ("label" TEXT, "n" INTEGER)'))
        for i in range(total):
            c.execute(text('INSERT INTO "t_wide" VALUES (:a, :b)'), {"a": f"u{i % distinct}", "b": i})
    out = build_schema_text(eng, ["t_wide"], with_samples=True)
    _check("超过全量行数才走样本分支", f"-- 表 t_wide 共 {total} 行，样本（前 20 行）：" in out, out)
    _check("超上限的列不给去重取值", "-- 列 label" not in out, out)


def test_char_budget_omits_and_notes() -> None:
    """字符预算用尽即停止追加，并注明省略了几张表（留痕，不静默截断）。"""
    eng = _new_engine()
    names = [f"t{i}" for i in range(3)]
    with eng.begin() as c:
        for n in names:
            c.execute(text(f'CREATE TABLE "{n}" ("k" TEXT, "v" INTEGER)'))
            for i in range(15):
                c.execute(text(f'INSERT INTO "{n}" VALUES (:a, :b)'), {"a": f"row-{i:03d}", "b": i})

    orig = settings.schema_max_chars
    settings.schema_max_chars = 500  # 只够第一张表
    try:
        out = build_schema_text(eng, names, with_samples=True)
    finally:
        settings.schema_max_chars = orig

    m = re.search(r"-- （另有 (\d+) 张表的取值区因超出字符预算已省略）", out)
    _check("末尾注明省略张数", m is not None, out)
    omitted = int(m.group(1)) if m else -1
    _check("列出的表 = 总表数 − 省略数", len(names) - omitted == out.count("-- 表 "), out)
    _check("预算内的表确实带上了取值", "row-000" in out, out)
    _check("超预算的表没有取值", out.count("row-000") == 1, out)


def test_single_table_failure_isolated() -> None:
    """单表读值失败只跳过该表，不毁掉整份 schema。"""
    eng = _pivot_engine()
    with eng.begin() as c:
        c.execute(text('CREATE TABLE "t_ok" ("k" TEXT)'))
        c.execute(text('INSERT INTO "t_ok" VALUES (:a)'), {"a": "ok-value"})

    real = eng_mod._table_value_lines

    def _flaky(engine, insp, table_name, quote):
        if table_name == PIVOT_TABLE:
            raise RuntimeError("表被并发删除")
        return real(engine, insp, table_name, quote)

    eng_mod._table_value_lines = _flaky
    try:
        out = build_schema_text(eng, [PIVOT_TABLE, "t_ok"], with_samples=True)
    finally:
        eng_mod._table_value_lines = real

    _check("坏表仍保留 DDL", f"CREATE TABLE {PIVOT_TABLE} (" in out, out)
    _check("坏表不附取值", INTEREST_LABEL not in out, out)
    _check("其它表的取值不受影响", "ok-value" in out, out)


def test_table_cap_disables_samples() -> None:
    """表数超过 schema_max_tables → 整份不附取值（防 prompt 爆炸的兜底）。"""
    eng = _new_engine()
    names = [f"c{i}" for i in range(settings.schema_max_tables + 1)]
    with eng.begin() as c:
        for n in names:
            c.execute(text(f'CREATE TABLE "{n}" ("k" TEXT)'))
            c.execute(text(f'INSERT INTO "{n}" VALUES (:a)'), {"a": "v"})

    out = build_schema_text(eng, names, with_samples=True)
    _check("超过表数上限时一律不出取值区", "-- 表 " not in out, out)
    _check("但 DDL 一张不少", out.count("CREATE TABLE") == len(names), out)


def test_cell_text() -> None:
    """单元格渲染：单行化 + 超长截断（防一个超长文本撑爆 prompt）。"""
    _check("None → 空串", eng_mod._cell_text(None) == "")
    _check("换行折叠为空格", eng_mod._cell_text("a\nb\r\nc") == "a b  c", repr(eng_mod._cell_text("a\nb\r\nc")))
    long = "x" * 500
    got = eng_mod._cell_text(long)
    _check("超长截断并留省略号", got.endswith("…") and len(got) == eng_mod._VALUE_CELL_MAX_CHARS + 1, str(len(got)))


def test_source_schema_text_branches() -> None:
    """build_source_schema_text 的分支表（三条查询链路的唯一入口）。"""
    eng = _new_engine()
    many = [f"d{i}" for i in range(settings.schema_max_tables + 1)]  # 6 张 > 上限
    with eng.begin() as c:
        for n in many:
            c.execute(text(f'CREATE TABLE "{n}" ("k" TEXT)'))
            c.execute(text(f'INSERT INTO "{n}" VALUES (:a)'), {"a": f"v-{n}"})

    real_resolve = eng_mod.resolve_table_names
    real_locate = eng_mod.locate_doc_tables
    try:
        # 1) doc 源定位命中 → 收窄到那一张，且带取值
        eng_mod.resolve_table_names = lambda ds: many
        eng_mod.locate_doc_tables = lambda ds, q, tables: ["d0"]
        out = build_source_schema_text(eng, "doc:dx", "=== blk === 那张表里有多少行？")
        _check("doc 源命中 → 收窄到定位表", "CREATE TABLE d0" in out and "CREATE TABLE d1" not in out, out)
        _check("doc 源命中 → 带取值区", "v-d0" in out, out)

        # 2) doc 源定位不到 → 回退全量，**且刻意不带取值**（239 张表各带样本会撑爆 prompt）
        eng_mod.locate_doc_tables = lambda ds, q, tables: []
        out = build_source_schema_text(eng, "doc:dx", "文档里提到了什么？")
        _check("doc 源未命中 → 回退全量表结构", out.count("CREATE TABLE") == len(many), out)
        _check("doc 源回退时**不带**取值区", "-- 表 " not in out, out)

        # 3) 非 doc 源且表数有界 → 带取值
        eng_mod.resolve_table_names = lambda ds: ["d0", "d1"]
        out = build_source_schema_text(eng, "upload:订单", "")
        _check("非 doc 源表数有界 → 带取值区", "v-d0" in out and "v-d1" in out, out)
    finally:
        eng_mod.resolve_table_names = real_resolve
        eng_mod.locate_doc_tables = real_locate


def main() -> None:
    test_small_table_full_dump()
    test_no_samples_byte_identical()
    test_big_table_sample_plus_distinct()
    test_distinct_over_limit_skipped()
    test_char_budget_omits_and_notes()
    test_single_table_failure_isolated()
    test_table_cap_disables_samples()
    test_cell_text()
    test_source_schema_text_branches()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
