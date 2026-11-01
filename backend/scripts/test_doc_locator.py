"""文档表格定位与检索呈现的离线自检：不连 Milvus、不连 embedding、不依赖 LLM。

覆盖三条**多表文档**（赛题 Table_Query 500 张表 / Multi-step 48 张表）上的关键路径：

1. ``retrieve_doc_scored`` 的阈值语义 —— Milvus 的 metric 是 **L2 距离，越小越相似**，
   故 ``max_distance`` 是**上界**。方向搞反的话，要么把无关表当候选（收窄后确定地用错表），
   要么一张都定位不到（每次回退全量 schema，收窄形同虚设）。
2. ``_locate_doc_tables`` 的各条出口：题干块 id 精确命中、白名单过滤、**按内容去重**、
   零候选时**沿用上一轮用过的表**（且可信命中优先）、定位不到返回空。去重这条尤其要紧：
   文档里同一张表常被复制成多个块（赛题咖啡表 4 份、能源表 5 份），不去重就会把同构表的
   多个副本一起交给模型，它分别聚合再把结果相加 —— 数值直接翻倍，且形态完全正常。
3. ``_chunk_header`` 对表格块的「摘录」标注 —— Milvus 里表格块只有前 5 行样本，
   不标注模型就会拿它算计数/合计，报出一个看起来像答案的假统计量。

用替身函数（monkeypatch 模块属性）驱动，数据库与向量库一概不碰。
运行：``cd backend && python scripts/test_doc_locator.py``
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import app.services.document_service as dsvc
from app.agents.graph import _chunk_header, _locate_doc_tables
from app.config import settings


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _hit(content: str, block_id: str, score: float, kind: str = "table") -> dict:
    return {"content": content, "block_id": block_id, "block_index": 0, "kind": kind, "score": score}


def _patch_vector_hits(hits: list[dict]):
    """替身：绕过 embedding HTTP 与 Milvus，直接给出「检索到的块」。

    同时挡掉 ``get_doc_table_row_count``：真函数会去真 ``docs.db`` 里数
    ``doc_x_0`` 这种虚构表的行数，每张都抛 ``no such table`` 并打一整段栈，
    一次运行刷屏数百行 —— 真失败会被淹在里面。定位逻辑只用 ``content``/``block_id``，
    行数仅供 RAG 抬头措辞，置 0 不影响任何断言。
    """
    dsvc._doc_vector_hits = lambda doc_id, question, top_k, with_score: [
        dict(h, score=h["score"]) if with_score else {k: v for k, v in h.items() if k != "score"}
        for h in hits
    ]
    dsvc.get_doc_table_row_count = lambda table_name: 0


def _patch_block_map(mapping: dict[str, str]):
    """替身：块 id → 表名。

    打在 ``dsvc``（而不是 ``graph``）上：块寻址逻辑自本轮起住在
    ``engine.locate_doc_tables``，它是在**调用时**才 ``from app.services.document_service
    import get_doc_table_by_block``，故只有替换模块属性才拦得住。
    """
    dsvc.get_doc_table_by_block = lambda doc_id, block_id: mapping.get(str(block_id))


def _locate(
    ds: str, question: str, tables: list[str], prior_tables: list[str] | None = None
) -> list[str]:
    return asyncio.run(_locate_doc_tables(ds, question, tables, prior_tables=prior_tables))


def test_scored_thresholds() -> None:
    """retrieve_doc_scored：L2 距离是上界，超阈值判「没定位到」，margin 内成组保留。"""
    dsvc.get_doc_meta = lambda doc_id: None  # 抬头退回 doc_id，不影响断言

    # 真命中 0.87 / 语义无关 0.98~1.03 / 完全无关 1.43（2026-09 用 text-embedding-v4 实测标定）
    _patch_vector_hits([_hit("a", "b1", 0.87), _hit("b", "b2", 1.43)])
    got = dsvc.retrieve_doc_scored("dx", "q", top_k=5)
    _check("最优命中 0.87 ≤ 阈值 → 保留", len(got) == 1 and got[0]["block_id"] == "b1", str(got))
    _check("超 margin 的 1.43 被挡掉", all(g["block_id"] != "b2" for g in got), str(got))

    # 全部超阈值 → 空，交由调用方回退全量 schema
    _patch_vector_hits([_hit("a", "b1", 1.20), _hit("b", "b2", 1.43)])
    _check("最优命中 1.20 > 阈值 → 判为未定位", dsvc.retrieve_doc_scored("dx", "q", top_k=5) == [])

    # 同分副本成组保留（内容逐字相同的副本分数完全一致）
    _patch_vector_hits([_hit("a", "b1", 0.90), _hit("a", "b2", 0.90), _hit("c", "b3", 1.10)])
    got = dsvc.retrieve_doc_scored("dx", "q", top_k=5)
    _check("同分副本成组保留", sorted(g["block_id"] for g in got) == ["b1", "b2"], str(got))

    _patch_vector_hits([])
    _check("无命中 → 空", dsvc.retrieve_doc_scored("dx", "q", top_k=5) == [])

    _check("阈值为上界且默认 0.95", settings.doc_table_max_distance == 0.95, str(settings.doc_table_max_distance))
    _check("margin 默认 0.10", settings.doc_table_score_margin == 0.10, str(settings.doc_table_score_margin))
    # 边界：恰好等于阈值算命中（用 <= 而非 <）
    _patch_vector_hits([_hit("a", "b1", 0.95)])
    _check("恰好等于阈值算命中", len(dsvc.retrieve_doc_scored("dx", "q", top_k=5)) == 1)


def test_locate_doc_tables() -> None:
    """_locate_doc_tables 的四条出口。"""
    all_tables = ["doc_x_0", "doc_x_1", "doc_x_2", "doc_x_3", "doc_x_4"]

    # 1) 非文档源 / 块级数据源：一律不定位（调用方走既有 scoped schema）
    _patch_vector_hits([_hit("a", "b1", 0.80)])
    _patch_block_map({"b1": "doc_x_0"})
    _check("上传源不定位", _locate("upload:t", "随便问什么", all_tables) == [])
    _check("外部源不定位", _locate("mysql:demo", "随便问什么", all_tables) == [])
    _check("块级数据源不定位", _locate("doc:dx:blk1", "随便问什么", all_tables) == [])

    # 2) 题干块 id 精确命中 —— 早于向量检索，且必须过白名单
    _patch_vector_hits([_hit("a", "b1", 0.80)])
    _patch_block_map({"blk9": "doc_x_2", "b1": "doc_x_0"})
    _check(
        "题干块 id 精确命中",
        _locate("doc:dx", "=== blk9 === 这张表里有多少行？", all_tables) == ["doc_x_2"],
    )
    # 块 id 指向的表不属于本数据源 → 拒绝（防跨文档读数）
    _patch_block_map({"blk9": "doc_OTHER_7"})
    _check("块 id 指向别文档的表 → 拒绝", _locate("doc:dx", "=== blk9 === 有多少行？", all_tables) == [])

    # 3) 向量召回 + **按内容去重**：同一张表的多个副本只能算一个候选
    _patch_block_map({"b1": "doc_x_0", "b2": "doc_x_1", "b3": "doc_x_2", "b4": "doc_x_3"})
    _patch_vector_hits([
        _hit("同一份咖啡表", "b1", 0.88),   # 4 个副本，内容逐字相同
        _hit("同一份咖啡表", "b2", 0.88),
        _hit("同一份咖啡表", "b3", 0.88),
        _hit("同一份咖啡表", "b4", 0.88),
        _hit("另一张表", "b5", 0.90),
    ])
    _patch_block_map({"b1": "doc_x_0", "b2": "doc_x_1", "b3": "doc_x_2", "b4": "doc_x_3", "b5": "doc_x_4"})
    got = _locate("doc:dx", "各品类的销量是多少？", all_tables)
    _check("同内容副本去重后只留 2 张表", got == ["doc_x_0", "doc_x_4"], str(got))

    # 正文块不占用去重名额：文本块内容相同也不能把后面的表格挤掉
    _patch_vector_hits([
        _hit("同样的一段正文", "t1", 0.86, kind="text"),
        _hit("同样的一段正文", "t2", 0.86, kind="text"),
        _hit("咖啡表", "b1", 0.88),
    ])
    _patch_block_map({"b1": "doc_x_0"})  # t1/t2 无对应表
    got = _locate("doc:dx", "文档里提到了什么？", all_tables)
    _check("正文块不占用去重名额", got == ["doc_x_0"], str(got))

    # 4) 检索失败 / 无命中 → 返回空（调用方回退全量 schema + 日志）
    #    用完必须还原：``locate_doc_tables`` 是**调用时**才 import ``retrieve_doc_scored``，
    #    替身一旦留着不还，下面的「无命中」与「白名单」两段会一起走 _boom —— 断言虽然仍是 []，
    #    但那一段就退化成了空测，「命中的表不在白名单 → 拒绝」（防跨文档读数）实际没被覆盖。
    orig_retrieve = dsvc.retrieve_doc_scored

    def _boom(doc_id, question, top_k=None, **kw):
        raise RuntimeError("Milvus 不可达")

    dsvc.retrieve_doc_scored = _boom
    _check("检索抛异常 → 空（不阻断主链路）", _locate("doc:dx", "文档里提到了什么？", all_tables) == [])
    dsvc.retrieve_doc_scored = orig_retrieve

    _patch_vector_hits([])
    _check("检索无命中 → 空", _locate("doc:dx", "文档里提到了什么？", all_tables) == [])

    # 命中的块在表白名单之外（如别文档的表）→ 不采用
    _patch_vector_hits([_hit("别文档的表", "b9", 0.85)])
    _patch_block_map({"b9": "doc_OTHER_1"})
    _check("命中的表不在白名单 → 空", _locate("doc:dx", "各品类销量？", all_tables) == [])


def test_prior_tables_fallback() -> None:
    """第三级定位：检索零候选时沿用上一轮用过的表；可信命中永远优先。"""
    all_tables = ["doc_x_0", "doc_x_1", "doc_x_2"]

    # 线上实测形态：最优命中 0.9512 略超 0.95 → 检索零候选
    _patch_vector_hits([_hit("超级商店表", "b2", 0.9512)])
    _patch_block_map({"b2": "doc_x_2"})
    _check(
        "零候选 + 上文 → 沿用上一轮查询的表",
        _locate("doc:dx", "统计各个月的营业额", all_tables, prior_tables=["doc_x_1"])
        == ["doc_x_1"],
    )
    _check(
        "零候选 + 无上文 → 空（既有行为不变）",
        _locate("doc:dx", "统计各个月的营业额", all_tables) == [],
    )
    _check(
        "上文表不在白名单 → 拒绝",
        _locate("doc:dx", "统计各个月的营业额", all_tables, prior_tables=["doc_OTHER_0"])
        == [],
    )

    # 当轮检索有可信命中（0.88 ≤ 0.95）→ 以检索为准，不被上文带偏（话题切换）
    _patch_vector_hits([_hit("咖啡表", "b1", 0.88)])
    _patch_block_map({"b1": "doc_x_0", "b2": "doc_x_2"})
    _check(
        "可信命中优先于上文",
        _locate("doc:dx", "各月营业额", all_tables, prior_tables=["doc_x_1"]) == ["doc_x_0"],
    )

    # 白名单过滤 + 去重 + 保持原顺序
    _patch_vector_hits([])
    _check(
        "上文去重保序",
        _locate(
            "doc:dx",
            "各月营业额",
            all_tables,
            prior_tables=["doc_x_2", "doc_x_0", "doc_x_2", "doc_OTHER_9"],
        )
        == ["doc_x_2", "doc_x_0"],
    )

    # 检索抛异常（Milvus 不可用）时，已确认用过的表仍可续接
    orig_retrieve = dsvc.retrieve_doc_scored

    def _boom(doc_id, question, top_k=None, **kw):
        raise RuntimeError("Milvus 不可达")

    dsvc.retrieve_doc_scored = _boom
    try:
        _check(
            "检索异常 + 上文 → 沿用",
            _locate("doc:dx", "各月营业额", all_tables, prior_tables=["doc_x_1"])
            == ["doc_x_1"],
        )
    finally:
        dsvc.retrieve_doc_scored = orig_retrieve


def test_used_tables_extraction() -> None:
    """used_tables：从 SQL 解析被引用的表；解析失败返回 []，绝不影响查询执行。"""
    from app.services.query_service import _extract_used_tables

    class _FakeDialect:
        def __init__(self, name: str) -> None:
            self.name = name

    class _FakeEngine:
        def __init__(self, name: str) -> None:
            self.dialect = _FakeDialect(name)

    _check(
        "SQLite 双引号表名",
        _extract_used_tables('SELECT * FROM "doc_x_0" LIMIT 5') == ["doc_x_0"],
    )
    _check(
        "JOIN 多表去重保序",
        _extract_used_tables(
            'SELECT * FROM "doc_x_0" a JOIN "doc_x_1" b ON a.k = b.k '
            'JOIN "doc_x_0" c ON c.k = a.k'
        )
        == ["doc_x_0", "doc_x_1"],
    )
    # 默认方言解析反引号会抛 ParseError，必须按 engine.dialect.name 选方言
    _check(
        "MySQL 反引号（按引擎方言解析）",
        _extract_used_tables("SELECT * FROM `doc_x_0`", _FakeEngine("mysql")) == ["doc_x_0"],
    )
    _check("无表 SQL → 空", _extract_used_tables("SELECT 1") == [])
    _check("非法 SQL → 空（不抛）", _extract_used_tables("SELEC nope") == [])
    _check("引擎缺失时按默认方言解析", _extract_used_tables('SELECT * FROM "t"') == ["t"])


def test_fallback_schema_dedupes() -> None:
    """定位失败 → 回退全量 schema 时**也必须去重**。

    线上 4× 误差的直接触发条件就是这条回退路径：48 张表的 DDL 原样进 prompt，
    其中咖啡表有 4 张同构副本，模型 UNION 后聚合。故这里把定位打成「恒返回空」，
    断言进 ``build_schema_text`` 的表名已经是去重后的结果。
    """
    import app.database.engine as eng

    orig = (
        eng.resolve_table_names,
        eng.locate_doc_tables,
        eng.build_schema_text,
        eng.build_scoped_schema_text,
        dsvc.dedupe_doc_tables,
    )
    calls: list[tuple[list[str], bool]] = []
    seen_prior: list[list[str] | None] = []

    def _record(engine, names, with_samples=False):
        calls.append((list(names), with_samples))
        return "|".join(names)

    def _fake_locate(ds, q, tables, *, prior_tables=None):
        seen_prior.append(prior_tables)
        return []

    def _must_not_call(*a, **kw):
        raise AssertionError("doc 源回退时不应再走 build_scoped_schema_text（会重新取回全部表）")

    try:
        eng.locate_doc_tables = _fake_locate
        eng.build_schema_text = _record
        eng.build_scoped_schema_text = _must_not_call

        # 1) 去重后仍超 schema_max_tables：走不带取值区的全量 DDL
        eng.resolve_table_names = lambda ds: [f"doc_x_{i}" for i in range(48)]
        dsvc.dedupe_doc_tables = lambda doc_id, tables: [f"doc_x_{i}" for i in range(23)]
        got = eng.build_source_schema_text(None, "doc:abcdefabcdef", "各月营业额")
        _check("回退路径调用 build_schema_text 一次", len(calls) == 1, str(calls))
        _check("回退路径用的是去重后的 23 张表", len(calls[0][0]) == 23, str(calls[0][0]))
        _check("表数仍超上限时不附带取值区", calls[0][1] is False, str(calls[0]))
        _check("schema 文本来自去重后的表", got.startswith("doc_x_0|doc_x_1"), got[:40])

        # 2) 去重后表数有界：附带取值区
        calls.clear()
        dsvc.dedupe_doc_tables = lambda doc_id, tables: tables[:3]
        eng.build_source_schema_text(None, "doc:abcdefabcdef", "各月营业额")
        _check("表数在 schema_max_tables 内时附带取值区", calls[0][1] is True, str(calls[0]))
        _check("未给上文时 prior_tables 为 None", seen_prior[-1] is None, str(seen_prior))

        # 2b) 上文续接参数必须透传到定位函数（graph._prepare_query_node 靠它续接）
        calls.clear()
        eng.build_source_schema_text(
            None, "doc:abcdefabcdef", "各月营业额", prior_tables=["doc_x_1"]
        )
        _check("prior_tables 透传到定位函数", seen_prior[-1] == ["doc_x_1"], str(seen_prior))

        # 3) 按块寻址（doc:<id>:<块id>）：只解析出一张表，不能参与「副本去重」——
        #    历史文档里该块若恰好是副本，会被判成非首张而剔除，schema 直接变空。
        calls.clear()
        eng.resolve_table_names = lambda ds: ["doc_x_1"]
        dsvc.dedupe_doc_tables = lambda doc_id, tables: []  # 一旦被调用就会清空 schema
        got = eng.build_source_schema_text(None, "doc:abcdefabcdef:blk1", "")
        _check("按块寻址时不做副本去重", got == "doc_x_1", got)

        # 4) 非 doc 源不受影响：表数超上限时仍走 build_scoped_schema_text
        #    （表数在上限内的源本来就走 build_schema_text，与去重无关）
        calls.clear()
        eng.resolve_table_names = lambda ds: [f"t{i}" for i in range(10)]
        eng.build_scoped_schema_text = lambda engine, ds: "SCOPED"
        got = eng.build_source_schema_text(None, "upload:abc", "")
        _check("非 doc 源仍走 build_scoped_schema_text", got == "SCOPED", got)
        _check("非 doc 源不调用 build_schema_text", calls == [], str(calls))
    finally:
        (
            eng.resolve_table_names,
            eng.locate_doc_tables,
            eng.build_schema_text,
            eng.build_scoped_schema_text,
            dsvc.dedupe_doc_tables,
        ) = orig


def test_chunk_header() -> None:
    """表格块必须带「摘录」标注；正文块不带。"""
    text_head = _chunk_header({"title": "报告A", "block_id": "b1", "kind": "text"})
    _check("正文块抬头无摘录标注", "表格摘录" not in text_head, text_head)
    _check("正文块抬头含出处", text_head == "[来源：报告A · 片段 b1]", text_head)

    tbl_head = _chunk_header({"title": "报告A", "block_id": "b3", "kind": "table", "row_count": 897})
    _check("表格块抬头标注摘录", "表格摘录" in tbl_head, tbl_head)
    _check("表格块抬头给出全表行数", "全表共 897 行" in tbl_head, tbl_head)
    _check("表格块抬头仍含出处", "报告A" in tbl_head and "b3" in tbl_head, tbl_head)

    unknown = _chunk_header({"title": "报告A", "block_id": "b3", "kind": "table", "row_count": None})
    _check("行数未知时措辞兜底而非编造", "全表行数未知" in unknown, unknown)

    # 没有 block_id 时退回顺位编号，不能出现空片段号
    fallback = _chunk_header({"title": "报告A", "block_id": "", "block_index": 4, "kind": "text"})
    _check("无块 id 时用顺位编号", fallback == "[来源：报告A · 片段 #5]", fallback)


def main() -> None:
    test_scored_thresholds()
    test_locate_doc_tables()
    test_prior_tables_fallback()
    test_used_tables_extraction()
    test_fallback_schema_dedupes()
    test_chunk_header()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
