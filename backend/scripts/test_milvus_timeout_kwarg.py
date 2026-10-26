"""Milvus 写入超时自检：守住「首次写入一个还不存在的集合」这条路径。

背景（真实的线上故障）：langchain-milvus 0.4.0 的 ``Milvus.add_embeddings`` 把形参
``**kwargs`` 这个局部名字重新赋值了 —— 目标集合尚不存在时执行
``kwargs = {"embeddings": ..., "metadatas": ...}`` 并顺手 ``kwargs["timeout"] = self.timeout``，
随后却调用 ``self.client.insert(..., timeout=timeout, **kwargs)``，同一个 timeout 传了两次，
必然抛 ``TypeError: insert() got multiple values for keyword argument 'timeout'``。
它是 TypeError 而非 MilvusException，上游的 except 接不住，会裸抛到调用方。

触发条件三条同时成立：store 用**真值 timeout 构造**、目标集合**尚不存在**、写入行数 ≥ 1。
也就是说，只要把超时放回 ``Milvus(...)`` 构造函数，「文档首次导入」与「知识库 drop_old 重建」
就会 100% 失败；而失败那次已经先把空集合建了出来（``_init`` 在 insert 之前跑完），
于是**重试又恰好成功** —— 这正是该缺陷极难定位的原因。

本自检用替身客户端把这条路径固化成断言：**不写入任何真实集合**，
只驱动真实的 ``app.database.milvus`` 与真实的 ``langchain_milvus`` 代码。

注意：替身只覆盖 ``MilvusClient`` / ``AsyncMilvusClient``，而 ``langchain_milvus`` 0.4.0
的 ``Milvus.__init__`` 在拿到 ``connection_args={"uri": ...}`` 时走的是 pymilvus 的**ORM
``connections.connect()``**，这条路径没有被替身拦住 —— 故本脚本**需要本机 Milvus 可达**
（``MILVUS_URI``，默认 ``http://localhost:19530``）。Milvus 未启动时会以
``MilvusException: Fail connecting to server on localhost:19530`` 失败，
那是环境问题，不是代码回归。

运行：``cd backend && python scripts/test_milvus_timeout_kwarg.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from unittest import mock

import langchain_milvus.vectorstores.milvus as lm
from langchain_core.embeddings import Embeddings
from pymilvus import CollectionSchema, MilvusClient as _RealMilvusClient

from app.config import settings
from app.database import milvus as milvus_mod

# 上游缺陷的复现签名：这些字段一旦在 add_embeddings 里被塞进 kwargs，
# 就说明上游仍未修，我们的「调用处传超时」规避必须保留。
_BUGGY_SOURCE_MARKER = 'kwargs["timeout"] = self.timeout'


class _FakeEmbeddings(Embeddings):
    """定长假向量：把 embedding 这一步摘掉，自检不需要任何外部服务。"""

    DIM = 8

    def embed_documents(self, texts):
        return [[0.1] * self.DIM for _ in texts]

    def embed_query(self, text):
        return [0.1] * self.DIM


class _ServerState:
    """替身「服务端」的可见状态：已存在的集合 + 收到的 insert 调用。"""

    def __init__(self, existing=()):
        self.collections = set(existing)
        self.insert_calls: list[dict] = []
        self.other_calls: list[str] = []


def _make_stub(state: _ServerState):
    """造一个替身 MilvusClient：只要被走到的方法都实现，且**不建立任何连接**。"""

    class _StubMilvusClient:
        def __init__(self, *args, **kwargs):
            self._using = "default"  # Milvus.__init__ 会读它当 alias

        def has_collection(self, name, **kwargs):
            return name in state.collections

        def create_schema(self, **kwargs):
            # 与 pymilvus MilvusClient.create_schema 同体：返回真实 schema 对象，
            # 让 langchain_milvus 真正去 add_field（字段推导逻辑仍在被测）
            kwargs["check_fields"] = False
            return CollectionSchema([], **kwargs)

        def create_collection(self, collection_name=None, **kwargs):
            state.collections.add(collection_name)

        def drop_collection(self, name, **kwargs):
            state.collections.discard(name)

        def describe_collection(self, name, **kwargs):
            return {"collection_name": name, "fields": []}

        def list_indexes(self, name, **kwargs):
            return []  # 还没建索引

        def prepare_index_params(self, *args, **kwargs):
            # 复用 pymilvus 的真实实现（纯内存构造，不碰网络）
            return _RealMilvusClient.prepare_index_params(*args, **kwargs)

        def create_index(self, **kwargs):
            state.other_calls.append("create_index")

        def insert(self, collection_name, data, timeout=None, partition_name="", **kwargs):
            state.insert_calls.append(
                {
                    "collection": collection_name,
                    "rows": len(data),
                    "timeout": timeout,
                    "extra_kwargs": sorted(kwargs),
                }
            )
            return {"insert_count": len(data), "ids": [str(i) for i in range(len(data))], "cost": 0}

        def __getattr__(self, item):
            # 兜底：其余服务端调用（建索引/加载等）一律当作「服务端接受了」，
            # 只登记名字，便于发现替身与真实接口的偏差
            def _noop(*args, **kwargs):
                state.other_calls.append(item)
                return None

            return _noop

    return _StubMilvusClient


class _StubAsyncMilvusClient:
    """langchain_milvus 构造期会顺手建一个异步客户端，给个空实现免掉告警噪音。"""

    def __init__(self, *args, **kwargs):
        pass


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _run(collection: str, texts: list[str], metadatas: list[dict],
         drop_old: bool, pre_existing: bool) -> tuple[_ServerState, Exception | None]:
    """跑一次真实的 vector_index，返回替身服务端状态与异常（无异常为 None）。"""
    state = _ServerState({collection} if pre_existing else set())
    milvus_mod.dispose_vector_stores()  # 清掉上一个场景缓存的 store（替身）
    try:
        with mock.patch.object(lm, "MilvusClient", _make_stub(state)), \
                mock.patch.object(lm, "AsyncMilvusClient", _StubAsyncMilvusClient), \
                mock.patch.object(milvus_mod, "get_embeddings", lambda: _FakeEmbeddings()):
            milvus_mod.vector_index(collection, texts, metadatas, drop_old=drop_old)
            err = None
    except Exception as e:  # noqa: BLE001 — 自检就是要看它抛不抛
        err = e
    finally:
        milvus_mod.dispose_vector_stores()
    return state, err


_TEXT_METAS = [
    {"doc_id": "d1", "block_id": "b1", "kind": "text", "block_index": 0},
    {"doc_id": "d1", "block_id": "b2", "kind": "text", "block_index": 1},
]
_KNOWLEDGE_METAS = [{"source": "a.md"}, {"source": "b.md"}]


def main() -> None:
    # 1) 文档导入路径：集合**不存在**时的首次写入（本次线上故障的确切场景）
    state, err = _run("doc_chunks", ["甲", "乙"], _TEXT_METAS, drop_old=False, pre_existing=False)
    _check("文档首次导入（集合不存在）写入成功", err is None, repr(err))
    _check("写入行数正确", state.insert_calls and state.insert_calls[0]["rows"] == 2,
           repr(state.insert_calls))
    _check(
        f"写入超时生效（timeout={settings.milvus_timeout_seconds}）",
        bool(state.insert_calls) and state.insert_calls[0]["timeout"] == settings.milvus_timeout_seconds,
        repr(state.insert_calls),
    )
    _check("写入后集合已建出", "doc_chunks" in state.collections)

    # 2) 知识库重建路径：drop_old=True 会先把集合 drop 掉，必然落入同一个分支
    state, err = _run("knowledge_chunks", ["甲", "乙"], _KNOWLEDGE_METAS,
                      drop_old=True, pre_existing=True)
    _check("知识库 drop_old 重建写入成功", err is None, repr(err))
    _check(
        "重建超时生效",
        bool(state.insert_calls) and state.insert_calls[0]["timeout"] == settings.milvus_timeout_seconds,
        repr(state.insert_calls),
    )

    # 3) 集合已存在时同样能写（回归：别为了规避缺陷把正常路径写坏）
    state, err = _run("doc_chunks", ["甲", "乙"], _TEXT_METAS, drop_old=False, pre_existing=True)
    _check("集合已存在时写入成功", err is None, repr(err))
    _check(
        "集合已存在时超时同样生效",
        bool(state.insert_calls) and state.insert_calls[0]["timeout"] == settings.milvus_timeout_seconds,
        repr(state.insert_calls),
    )

    # 4) 守住「不要把 timeout 挪回构造函数」这条约束 —— 挪回去上面两条立刻复发 TypeError
    store = milvus_mod.get_vector_store("__probe_nonexistent__")
    _check("vector store 的构造级 timeout 为 None", store.timeout is None, repr(store.timeout))
    milvus_mod.dispose_vector_stores()

    # 5) 上游缺陷是否仍存在（信息性提示，不作为失败条件）
    try:
        src = Path(lm.__file__).read_text(encoding="utf-8")
    except Exception:
        src = ""
    if _BUGGY_SOURCE_MARKER in src:
        print("\n[info] 上游 langchain-milvus 仍存在该 kwargs 覆写缺陷（issue #91，修复 PR #142 未合并，"
              "0.4.0 已是最新发布版）：超时继续在调用处传，勿挪回 Milvus(...) 构造函数。")
    else:
        print("\n[info] 上游源码里已找不到该缺陷标记，可能已修复（PR #142 把 kwargs 改名 init_kwargs）："
              "可评估把超时挪回构造函数 —— 注意 PR #142 只改同步分支，异步 aadd_embeddings 需另查。")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
