"""外部 embedding 客户端（OpenAI 兼容端点）。

通过 LangChain 的 ``OpenAIEmbeddings`` 接入阿里云 DashScope 的 ``text-embedding-v4``
（DeepSeek 不提供 embedding）。向量检索为硬依赖，未配置时构造/调用会直接报错。

注意：DashScope 兼容端点不支持 token ID 输入与 base64 编码，且单请求最多 10 条，
因此需 ``check_embedding_ctx_length=False``（发原始文本而非 token）、
``encoding_format="float"``（覆盖 openai SDK 默认的 base64）与
``chunk_size=10``（限制单次批量大小）。
"""
import threading

from langchain_openai import OpenAIEmbeddings

from app.config import settings

# 客户端按配置缓存：每次构造都会新建一个 httpx client（含连接池），
# 而知识库/文档链路每次建 vector store 都要取一次 → 不缓存就是持续泄漏连接。
_cache: OpenAIEmbeddings | None = None
_cache_key: tuple[str, str, str] | None = None
_lock = threading.Lock()


def get_embeddings() -> OpenAIEmbeddings:
    """构造（并缓存）embedding 客户端。

    缓存键是**全部**构造参数：只按「有没有缓存」判断的话，改 ``EMBEDDING_MODEL``
    等配置后仍会一直用旧客户端，配置看起来不生效。
    """
    global _cache, _cache_key
    key = (
        settings.embedding_model,
        settings.embedding_base_url,
        settings.embedding_api_key,
    )
    with _lock:
        if _cache is not None and _cache_key == key:
            return _cache
        _cache = OpenAIEmbeddings(
            model=settings.embedding_model,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            check_embedding_ctx_length=False,
            chunk_size=10,
            model_kwargs={"encoding_format": "float"},
            # SDK 默认 600s 超时 × 2 次重试：embedding 服务不可达时单次可达 30 分钟，
            # 而文档导入/检索在请求链路上是串行阻塞的
            request_timeout=settings.embedding_timeout_seconds,
            max_retries=1,
        )
        _cache_key = key
        return _cache