"""本地知识库检索：扫描 ``data/knowledge/`` 下的 .md/.txt，分块后写入 Milvus 做向量检索。

供决策报告 agent 的 ``knowledge_search`` 工具复用；按**文件清单**缓存分块结果，
目录不存在或无内容时返回空。
"""
import logging
import threading
from pathlib import Path

from app.config import settings
from app.database.milvus import vector_index, vector_search
from app.services.chunking import chunk_text

logger = logging.getLogger(__name__)

_ALLOWED_EXT = {".md", ".txt"}

# 语料缓存：路径 → (文件清单, 语料)。
# 清单是**逐文件**的 (相对路径, mtime_ns, size)，不是目录 mtime：
# 目录 mtime 只在**增删文件**时变化，原地编辑一个文件（大小与文件名都不变）
# 是**改不到**目录 mtime 的，用目录 mtime 作失效键会让编辑永远不生效。
_cache: dict[str, tuple[tuple, list[dict]]] = {}
# check-then-act 保护：事件循环里的检索与报告 agent 工具线程会同时调进来，
# 无锁时两边会各重建一遍语料，并在 Milvus 上互相覆盖 / 泄漏 store
_lock = threading.Lock()


def _file_manifest(root: Path) -> tuple:
    """语料目录的文件清单：``((相对路径, mtime_ns, 字节数), ...)``，按路径排序。"""
    items: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in _ALLOWED_EXT:
            continue
        try:
            st = path.stat()
        except OSError:
            continue  # 扫描期间被删除/无权限，跳过
        items.append((str(path.relative_to(root)), st.st_mtime_ns, st.st_size))
    return tuple(items)


def load_corpus() -> list[dict]:
    """返回知识库语料 ``[{'source', 'content'}]``；目录不存在/无文件返回空列表。

    文件清单变化时重建内存缓存，并同步重建 Milvus 向量索引。
    """
    root = settings.knowledge_base_dir
    if not root.exists():
        return []

    manifest = _file_manifest(root)
    key = str(root)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == manifest:
            return cached[1]

        corpus: list[dict] = []
        for rel, _mtime, _size in manifest:
            path = root / rel
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                logger.warning("知识库文件读取失败，已跳过：%s", rel, exc_info=True)
                continue
            for chunk in chunk_text(text, settings.doc_chunk_size, settings.doc_chunk_overlap):
                corpus.append({"source": path.name, "content": chunk})

        # 先重建向量索引、**再**发布新缓存：反过来的话，并发读者会拿着新语料去查尚未重建的
        # 旧索引（检索到上一版内容）。全程持锁，避免两个线程同时重建。
        # 注意 drop_old=True 会在 Milvus 侧重建集合，此处仍是**查询路径上的破坏性重建**：
        # 恰好在此刻正在检索的请求可能读到空结果。知识库是运维侧手动维护的本地目录，
        # 触发面很窄；若要彻底消除，需改为版本化集合名 + 后台重建，超出本轮范围。
        vector_index(
            settings.milvus_knowledge_collection,
            [d["content"] for d in corpus],
            [{"source": d["source"]} for d in corpus],
            drop_old=True,
        )
        _cache[key] = (manifest, corpus)
        return corpus


def search_knowledge(query: str, top_k: int | None = None) -> str:
    """检索本地知识库，返回拼接好的 top-k 片段文本；无内容返回空串。"""
    top_k = top_k or settings.doc_top_k
    corpus = load_corpus()  # 确保语料已加载并（若内容变化）同步重建向量索引
    if not corpus:
        return ""

    hits = vector_search(settings.milvus_knowledge_collection, query, top_k=top_k)
    return "\n\n".join(f"[来源：{h.get('source', '')}]\n{h['content']}" for h in hits)
