"""Milvus 向量库单例与检索助手（经 ``langchain-milvus`` vector store）。

向量检索为硬依赖：Milvus 未启动或 embedding 未配置时，调用会直接抛出异常。
按 collection_name 惰性缓存 vector store 实例。
"""
import logging
import threading

from langchain_milvus import Milvus

from app.config import settings
from app.llm.embedding import get_embeddings

logger = logging.getLogger(__name__)

# 按 collection_name 缓存 vector store 实例。
# 缓存键含 milvus_uri：只按 collection 名缓存的话，改了 MILVUS_URI 仍会连旧地址。
_store_cache: dict[tuple[str, str], Milvus] = {}
# 事件循环与工具线程都会调进来（knowledge_service 的语料重建），必须加锁
_lock = threading.Lock()


def _close_store(store: Milvus) -> None:
    """尽力关闭旧 store 底层的 MilvusClient（gRPC 连接池 + 后台线程）。

    ``langchain_milvus`` 的 ``Milvus`` 没有 ``close``/``__del__``，不显式关掉
    底层 client 就只能等 GC，期间连接与线程一直挂着。
    """
    for attr in ("client", "_milvus_client"):
        client = getattr(store, attr, None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
                return
            except Exception:
                logger.warning("关闭 Milvus client 失败，已跳过。", exc_info=True)


def get_vector_store(collection_name: str, drop_old: bool = False) -> Milvus:
    """返回（惰性缓存的）Milvus vector store；``drop_old=True`` 时重建集合。"""
    key = (settings.milvus_uri, collection_name)
    with _lock:
        if not drop_old and key in _store_cache:
            return _store_cache[key]

        # 单次超时（Milvus 挂掉时不要无限等，请求链路上是串行阻塞的）统一在**调用处**传，
        # 见下方 vector_index / vector_search / vector_delete。**不要**挪到这里当构造参数：
        # langchain-milvus 0.4.0 的 Milvus.add_embeddings 把形参 ``**kwargs`` 这个局部名字
        # 重新赋值了 —— 目标集合尚不存在时会执行
        # ``kwargs = {"embeddings": ..., "metadatas": ...}`` 并顺手
        # ``kwargs["timeout"] = self.timeout``，随后却调用
        # ``self.client.insert(..., timeout=timeout, **kwargs)``：同一个 timeout 传了两次，
        # 必然抛 ``TypeError: insert() got multiple values for keyword argument 'timeout'``
        # （site-packages/langchain_milvus/vectorstores/milvus.py:1543-1577）。
        # 它是 TypeError 而非 MilvusException，上游紧随其后的 except 接不住，会裸抛到调用方。
        # 触发面 = 「首次写入一个还不存在的集合」：文档首次导入、知识库 drop_old 重建都必中；
        # 而失败那次已经把空集合建了出来（_init 在 insert 之前跑完），于是**重试又恰好成功**，
        # 故障因此极难定位。
        # 上游状态（2026-09-12 核实）：issue #91 长期 open，修复 PR #142 未合并，
        # 0.4.0 已是最新发布版 —— 没有可升级的版本，只能本地规避。PR #142 也只改同步分支，
        # 异步 aadd_embeddings 的同一个缺陷仍在。将来可这样判断是否已修：在安装目录里 grep
        # ``init_kwargs``（已修）vs ``kwargs = {"embeddings"``（未修），同步/异步两处都要看。
        # 调用处的 timeout 是上游的具名形参，不会进 **kwargs，既能设上超时也不受该缺陷影响。
        # 已知代价（可接受，但别装作没有）：构造级 timeout 同时也是 __init__ → _init → _load
        # 的 load_collection 超时来源，置 None 后这次「加载集合」RPC 就没有 deadline 了
        # （pymilvus 3.0.1 的 MilvusClient 没有兜底默认值，timeout=None 直接透传给 grpc）。
        # 影响面：仅 store 构造这一次的 load，且它就发生在下面的 _lock 里 —— 若 Milvus
        # 半死不活地卡住这个 RPC，会连锁一起挂住。写入/检索/删除三条活跃路径都已带上超时。
        # 回归自检：scripts/test_milvus_timeout_kwarg.py
        store = Milvus(
            embedding_function=get_embeddings(),
            collection_name=collection_name,
            connection_args={"uri": settings.milvus_uri},
            auto_id=True,
            drop_old=drop_old,
        )
        old = _store_cache.get(key)
        _store_cache[key] = store
    # 锁外关闭旧实例：close 可能阻塞，不该占着锁
    if old is not None:
        _close_store(old)
    return store


def dispose_vector_stores() -> None:
    """关闭并清空所有缓存的 vector store（应用关停时调用）。"""
    with _lock:
        stores = list(_store_cache.values())
        _store_cache.clear()
    for store in stores:
        _close_store(store)


def vector_index(
    collection_name: str,
    texts: list[str],
    metadatas: list[dict],
    drop_old: bool = False,
) -> None:
    """把文本向量化写入集合；``drop_old=True`` 时先重建集合（知识库重索引用）。"""
    if not texts:
        return
    get_vector_store(collection_name, drop_old=drop_old).add_texts(
        texts,
        metadatas=metadatas,
        # 必须在调用处传（add_texts 的具名形参），理由见 get_vector_store 的注释
        timeout=settings.milvus_timeout_seconds,
    )


def vector_search(
    collection_name: str,
    query: str,
    top_k: int,
    expr: str | None = None,
    with_score: bool = False,
) -> list[dict]:
    """向量相似度检索，返回 ``[{"content": ..., **metadata}]``（空命中返回空列表）。

    ``with_score=True`` 时每项额外带 ``score``。集合由库自动创建、未指定
    ``index_params``，metric 是 **L2 距离 —— 越小越相似**（与余弦相似度方向相反，
    所以调用方的阈值是「上界」而非「下界」，别照搬余弦的直觉）。

    ``similarity_search`` 内部本就是调 ``similarity_search_with_score`` 再把分数丢掉，
    故取分数不增加检索成本；默认 ``False`` 是为了不改变既有调用方的返回结构
    （``knowledge_service`` 与报告 agent 直接消费这个列表）。
    """
    store = get_vector_store(collection_name)
    if not with_score:
        docs = store.similarity_search(
            query,
            k=top_k,
            expr=expr,
            # 必须在调用处传（similarity_search 的具名形参），理由见 get_vector_store 的注释
            timeout=settings.milvus_timeout_seconds,
        )
        return [{"content": d.page_content, **(d.metadata or {})} for d in docs]

    pairs = store.similarity_search_with_score(
        query,
        k=top_k,
        expr=expr,
        timeout=settings.milvus_timeout_seconds,
    )
    return [
        {"content": d.page_content, "score": float(s), **(d.metadata or {})}
        for d, s in pairs
    ]


def vector_delete(collection_name: str, expr: str) -> bool:
    """按过滤表达式删除集合中的向量（如 ``doc_id == "xxx"``）；失败静默降级。

    返回是否删除成功：调用方（删除文档）据此决定要不要把残留记进日志 ——
    静默吞掉会让向量**永久残留**且无人知晓。
    """
    try:
        get_vector_store(collection_name).delete(
            expr=expr,
            # 顺带补上原本缺失的超时：Milvus.delete 的签名是 (ids, expr, **kwargs)，
            # 既没有 timeout 形参也不读 store 级 timeout，只能经 **kwargs 透传给
            # MilvusClient.delete —— 早前这里其实是**完全没有超时**的
            timeout=settings.milvus_timeout_seconds,
        )
        return True
    except Exception as e:
        logger.warning("删除向量失败，已跳过：%s（%s）", expr, e.__class__.__name__)
        return False


def list_field_values(collection_name: str, field: str, limit: int = 10000) -> list[str]:
    """列出集合中某标量字段的去重取值（用于对账孤儿向量）；不可用返回空列表。"""
    try:
        store = get_vector_store(collection_name)
        client = getattr(store, "client", None)
        if client is None or not client.has_collection(collection_name):
            return []
        rows = client.query(
            collection_name=collection_name,
            filter="",
            output_fields=[field],
            limit=limit,
            timeout=settings.milvus_timeout_seconds,
        )
    except Exception:
        logger.warning("读取向量库字段取值失败，已跳过：%s", field, exc_info=True)
        return []
    return sorted({str(r[field]) for r in rows if r.get(field)})
