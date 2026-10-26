"""文档表格定位与检索呈现的离线自检：不连 Milvus、不连 embedding、不依赖 LLM。

覆盖三条**多表文档**（赛题 Table_Query 500 张表 / Multi-step 48 张表）上的关键路径：

1. ``retrieve_doc_scored`` 的阈值语义 —— Milvus 的 metric 是 **L2 距离，越小越相似**，
   故 ``max_distance`` 是**上界**。方向搞反的话，要么把无关表当候选（收窄后确定地用错表），
   要么一张都定位不到（每次回退全量 schema，收窄形同虚设）。
2. ``_locate_doc_tables`` 的四条出口：题干块 id 精确命中、白名单过滤、**按内容去重**、
   定位不到返回空。去重这条尤其要紧：文档里同一张表常被复制成多个块（赛题咖啡表 4 份、
   能源表 5 份），不去重就会把同构表的多个副本一起交给模型，它分别聚合再把结果相加
   —— 数值直接翻倍，且形态完全正常。
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


def _locate(ds: str, question: str, tables: list[str]) -> list[str]:
    return asyncio.run(_locate_doc_tables(ds, question, tables))


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
    test_chunk_header()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
