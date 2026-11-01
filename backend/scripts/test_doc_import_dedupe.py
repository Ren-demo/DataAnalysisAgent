"""文档导入去重离线自检：真实解析赛题 Multi-step 文档，验证「副本块只建一张表」。

线上事故（数据源 ``doc:c9ef73266f4e``）：文档把同一张咖啡表逐字节复制成 4 个块，
入库后成了 4 张同构表；某次表格定位失败、回退全量 schema 时，模型把 4 张表 UNION 聚合，
「各月营业额 / 客单数」正好是真实值的 4.000 倍（28200.8 = 7050.2 × 4 …），形态完全正常。

本脚本不走 Milvus / embedding / LLM：``vector_index`` 打成空操作，docs.db 指向临时目录，
只验证入库这一端的不变式：

1. 48 个表格块只落 **23 张表**（副本按内容指纹合并，Energy 5 连、Coffee 4 连）；
2. 四个咖啡块 id 都映射到**同一张**表；
3. 该表仍是**全量** 896 行、``money`` 合计 30592.94（合并没有丢行）；
4. ``doc_blocks`` 仍逐块记一行（出处引用与块 id 寻址不受影响）；
5. ``dedupe_doc_tables`` 对**历史文档**（库里已是多张副本表）同样只留首张。

运行：``cd backend && python scripts/test_doc_import_dedupe.py``
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import text

import app.services.document_service as dsvc
from app.config import settings
from app.database.engine import invalidate_engine

DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "赛题一数据"
    / "非结构化数据"
    / "Multi-step_Retrieval-数据.md"
)
# 文档前 4 个块各含一份逐字节相同的 896 行咖啡表（diff 实测 IDENTICAL）
COFFEE_BLOCKS = ["05ab28a47b924ae", "365e37e0ce2c42c", "af715ca17de1457", "5054d79994aa463"]
EXPECTED_TABLES = 23
EXPECTED_TABLE_BLOCKS = 48
EXPECTED_ROWS = 896
EXPECTED_MONEY = 30592.94


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


class _StubUpload:
    """``read_upload_limited`` 只用 filename / size / file，无需真的 FastAPI UploadFile。"""

    def __init__(self, path: Path):
        raw = path.read_bytes()
        self.filename = path.name
        self.size = len(raw)
        self.file = io.BytesIO(raw)


def _use_temp_docs_db() -> Path:
    """把 docs.db 指到临时目录：既保住本地开发库，也让断言可重复。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="docs_dedupe_"))
    invalidate_engine("docs")
    settings.data_dir = tmpdir
    return tmpdir


def test_import_merges_copies() -> tuple[str, str, str]:
    _check("赛题样例文档存在", DOC_PATH.exists(), str(DOC_PATH))
    ds = dsvc.import_document(_StubUpload(DOC_PATH))
    _check("导入返回 doc:<12 位十六进制>", ds.startswith("doc:") and len(ds) == 16, ds)
    doc_id = ds[len("doc:"):]

    engine = dsvc._docs_engine()
    with engine.connect() as conn:
        table_blocks = conn.execute(
            text("SELECT COUNT(*) FROM doc_blocks WHERE doc_id = :d AND kind = 'table'"),
            {"d": doc_id},
        ).scalar()
        block_rows = conn.execute(
            text("SELECT COUNT(*) FROM doc_blocks WHERE doc_id = :d"), {"d": doc_id}
        ).scalar()
    _check(
        "表格块逐块落库（出处引用不丢）",
        int(table_blocks) == EXPECTED_TABLE_BLOCKS,
        f"{table_blocks} vs {EXPECTED_TABLE_BLOCKS}",
    )
    _check("doc_blocks 总行数 ≥ 表格块数（正文块另计）", int(block_rows) >= int(table_blocks), str(block_rows))

    tables = dsvc.get_doc_tables(doc_id)
    _check(
        f"48 个表格块去重后只有 {EXPECTED_TABLES} 张表",
        len(tables) == EXPECTED_TABLES,
        f"{len(tables)} 张：{tables}",
    )

    mapped = [dsvc.get_doc_table_by_block(doc_id, b) for b in COFFEE_BLOCKS]
    _check("4 个咖啡块都有表名", all(mapped), str(mapped))
    _check("4 个咖啡块映射到同一张表", len(set(mapped)) == 1, str(mapped))

    coffee = mapped[0]
    money_col = "money"
    with engine.connect() as conn:
        cols = [c[1] for c in conn.execute(text(f'PRAGMA table_info("{coffee}")')).all()]
        rows = conn.execute(text(f'SELECT COUNT(*), SUM("{money_col}") FROM "{coffee}"')).one()
    _check("合并后的表仍含 money 列", money_col in cols, str(cols))
    _check("合并后仍是全量 896 行", int(rows[0]) == EXPECTED_ROWS, str(rows[0]))
    _check(
        "合并后 money 合计仍为 30592.94（没有把副本行算进来）",
        abs(float(rows[1]) - EXPECTED_MONEY) < 0.01,
        str(rows[1]),
    )
    return ds, doc_id, coffee


def _ddl_count(schema: str, table_name: str) -> int:
    """某张表在 schema 文本里出现了几条 CREATE TABLE（方言按需加引号，两种都算）。"""
    return schema.count(f"CREATE TABLE {table_name} (") + schema.count(f'CREATE TABLE "{table_name}" (')


def test_fallback_schema_one_copy_only(ds: str, doc_id: str, coffee: str) -> None:
    """端到端（除 LLM 外）：定位失败回退全量时，进 prompt 的 schema 里每张表只出现一次。

    这是线上事故的**直接复现条件**：``locate_doc_tables`` 返回空 → 回退全量 schema。
    修好之后，模型看到的 DDL 里咖啡表只有一张，「UNION 4 张副本再聚合」这条错路不再存在。
    """
    import app.database.engine as eng

    orig_locate = eng.locate_doc_tables
    # 强制走回退分支（**kw 吸收 prior_tables：build_source_schema_text 会透传）
    eng.locate_doc_tables = lambda ds_id, question, tables, **kw: []
    try:
        schema = eng.build_source_schema_text(
            dsvc._docs_engine(), ds, "统计各个月的营业额和客单数"
        )
    finally:
        eng.locate_doc_tables = orig_locate

    ddl_count = schema.count("CREATE TABLE")
    _check(
        f"回退 schema 中每张表只出现一次（{EXPECTED_TABLES} 张）",
        ddl_count == EXPECTED_TABLES,
        f"{ddl_count} 张",
    )
    _check("咖啡表只建一次 DDL", _ddl_count(schema, coffee) == 1, coffee)
    for i in (1, 2, 3):
        # 带括号匹配，避免 doc_x_1 误匹配到 doc_x_10
        _check(f"副本表 doc_{doc_id}_{i} 未被建出/未进 schema", _ddl_count(schema, f"{doc_id}_{i}") == 0)


def test_dedupe_legacy_tables() -> None:
    """历史文档：库里已是多张副本表，查询期按 doc_blocks.content 去重。"""
    doc_id = "000000000000"
    engine = dsvc._docs_engine()
    legacy = ["doc_legacy_0", "doc_legacy_1", "doc_legacy_2", "doc_legacy_3", "doc_legacy_4"]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM doc_blocks WHERE doc_id = :d"), {"d": doc_id})
        for i, (name, content) in enumerate([
            ("doc_legacy_0", "同一份咖啡表"),
            ("doc_legacy_1", "同一份咖啡表"),
            ("doc_legacy_2", "同一份咖啡表"),
            ("doc_legacy_3", "另一张表"),
            ("doc_legacy_4", None),  # 老记录没有 content：无从判断重复，必须保留
        ]):
            conn.execute(
                text(
                    "INSERT INTO doc_blocks "
                    "(doc_id, block_index, block_id, kind, content, table_name, columns) "
                    "VALUES (:d, :i, :b, 'table', :c, :t, NULL)"
                ),
                {"d": doc_id, "i": i, "b": f"legacy{i}", "c": content, "t": name},
            )

    got = dsvc.dedupe_doc_tables(doc_id, legacy)
    _check(
        "历史文档的 3 张同内容副本只留首张",
        got == ["doc_legacy_0", "doc_legacy_3", "doc_legacy_4"],
        str(got),
    )
    _check("表顺序保持调用方传入的顺序", got == [t for t in legacy if t in set(got)], str(got))

    # 白名单外的表（无 doc_blocks 记录）一律保留，避免误删可用表
    _check(
        "无记录的表默认保留",
        dsvc.dedupe_doc_tables(doc_id, ["doc_unknown", "doc_legacy_1"]) == ["doc_unknown"],
        str(dsvc.dedupe_doc_tables(doc_id, ["doc_unknown", "doc_legacy_1"])),
    )
    _check("空列表原样返回", dsvc.dedupe_doc_tables(doc_id, []) == [])
    _check("不存在的文档不影响调用方", dsvc.dedupe_doc_tables("ffffffffffff", ["a", "b"]) == ["a", "b"])


def main() -> None:
    tmpdir = _use_temp_docs_db()
    # 向量化是入库之外唯一的副作用（且可能读到真 Milvus），打成空操作
    dsvc.vector_index = lambda *args, **kwargs: True
    try:
        ds, doc_id, coffee = test_import_merges_copies()
        test_fallback_schema_one_copy_only(ds, doc_id, coffee)
        test_dedupe_legacy_tables()
    finally:
        invalidate_engine("docs")
        settings.data_dir = Path(__file__).resolve().parent.parent / "data"
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()