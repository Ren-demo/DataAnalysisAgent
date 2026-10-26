"""数据源隔离离线自检：不依赖 LLM。

覆盖 ``app/database/engine.py`` 的 ``build_scoped_schema_text``（全链路统一口径），验证：

1. ``upload:<表>`` 只把**该表**写进 text2sql 的 schema 文本 ——
   所有 upload 源共用同一个 uploads.db 引擎，早前不限定表名，未选中的表也会进 prompt；
2. ``doc:<doc_id>`` 只暴露该文档提取出的表（``doc:<doc_id>:<块>`` 进一步收窄到单表）；
3. 外部数据源在「读不到表」时退回不限定（保持旧行为，避免瞬时反射失败给出空 schema）；
4. ``_infer_relationships`` 对**无主键**表（pandas ``to_sql(index=False)`` 建出的
   upload/doc 表）也能推断关联 —— 早前「目标表无主键就跳过」使该能力在这两类源上恒为空。

运行：``cd backend && python scripts/test_datasource_scope.py``
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import create_engine, text

from app.config import settings
from app.database.engine import (
    build_schema_text,
    build_scoped_schema_text,
    get_engine,
    get_relationships,
    invalidate_engine,
)


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        uploads = data_dir / "uploads.db"
        eng = create_engine(f"sqlite:///{uploads.as_posix()}")
        with eng.begin() as conn:
            conn.execute(text('CREATE TABLE "订单" ("id" INTEGER, "金额" REAL)'))
            conn.execute(text('CREATE TABLE "客户" ("id" INTEGER, "名称" TEXT)'))
            conn.execute(text('CREATE TABLE "私密表" ("secret" TEXT)'))
        eng.dispose()

        old_data_dir = settings.data_dir
        settings.data_dir = data_dir
        invalidate_engine("uploads")
        try:
            engine = get_engine("upload:订单")
            txt = build_scoped_schema_text(engine, "upload:订单")

            # 1) 只含选中的表
            _check("upload 源：选中表在 schema 中", "订单" in txt)
            _check("upload 源：未选中表不进 schema（客户）", "客户" not in txt, txt)
            _check("upload 源：未选中表不进 schema（私密表）", "私密表" not in txt, txt)

            # 对照：不限定表名时（修复前的行为）确实会全部暴露 —— 证明过滤是承重的
            unfiltered = build_schema_text(engine)
            _check("对照：不限定表名时三张表全部暴露", all(t in unfiltered for t in ("订单", "客户", "私密表")))

            # 2) doc 源：按文档归属过滤（patch 掉 docs.db 读取，聚焦传参口径）
            with patch("app.database.engine.resolve_table_names", return_value=["doc_ab_0"]) as m:
                build_scoped_schema_text(engine, "doc:ab")
            _check("doc 源：按文档归属收窄", m.call_args[0][0] == "doc:ab")

            # 3) 外部源：读不到表时退回不限定（only_tables=None）
            with patch("app.database.engine.resolve_table_names", return_value=[]), \
                 patch("app.database.engine.build_schema_text") as bs:
                build_scoped_schema_text(engine, "ext_1")
            _check("外部源：读不到表时 only_tables=None", bs.call_args.kwargs.get("only_tables") is None)

            with patch("app.database.engine.resolve_table_names", return_value=["t1"]), \
                 patch("app.database.engine.build_schema_text") as bs:
                build_scoped_schema_text(engine, "ext_1")
            _check("外部源：读得到表时按表过滤", bs.call_args.kwargs.get("only_tables") == ["t1"])

            # 4) upload/doc 源即便归属为空也**不**退回不限定（宁可空 schema 也不串表）
            with patch("app.database.engine.resolve_table_names", return_value=[]), \
                 patch("app.database.engine.build_schema_text") as bs:
                build_scoped_schema_text(engine, "doc:textonly")
            _check("doc 源：归属为空时传空列表而非 None", bs.call_args.kwargs.get("only_tables") == [])

            # 5) 无主键表的关联推断（回归 F4）
            #    订单.customer_id → 客户（无主键、无外键，只能退化到 id 列）
            nopk = create_engine("sqlite://")
            with nopk.begin() as conn:
                conn.execute(text('CREATE TABLE "orders" ("order_id" TEXT, "customer_id" TEXT, "amount" REAL)'))
                conn.execute(text('CREATE TABLE "customers" ("customer_no" TEXT, "name" TEXT)'))
            rels = get_relationships(nopk)
            _check(
                "无主键表也能推断出关联",
                any(
                    r["from_table"] == "orders"
                    and r["from_column"] == "customer_id"
                    and r["to_table"] == "customers"
                    for r in rels
                ),
                str(rels),
            )
            # 目标表无主键 → 退化取首列作为引用列
            to_col = next((r["to_column"] for r in rels if r["to_table"] == "customers"), None)
            _check("引用列退化为目标表首列", to_col == "customer_no", str(to_col))
            _check("关联类型为 inferred", all(r["kind"] == "inferred" for r in rels), str(rels))

            # 目标表有主键时仍优先用主键，且不再退化
            withpk = create_engine("sqlite://")
            with withpk.begin() as conn:
                conn.execute(text('CREATE TABLE "orders" ("order_id" TEXT, "customer_id" TEXT)'))
                conn.execute(text('CREATE TABLE "customers" ("cid" TEXT PRIMARY KEY, "name" TEXT)'))
            rels2 = get_relationships(withpk)
            _check(
                "有主键时引用主键列",
                any(r["to_table"] == "customers" and r["to_column"] == "cid" for r in rels2),
                str(rels2),
            )
        finally:
            settings.data_dir = old_data_dir
            invalidate_engine("uploads")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
