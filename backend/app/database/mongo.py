"""MongoDB（Motor 异步客户端）连接与会话集合访问。

连接是惰性且容错的：MongoDB 不可用时相关操作返回 None / 空结果，
保证会话记忆是可选能力，不拖垮主链路。
"""
import logging
import threading

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_collection: AsyncIOMotorCollection | None = None
_connections_collection: AsyncIOMotorCollection | None = None

# check-then-set 保护：这三个全局量虽是惰性初始化，但调用方不止事件循环 ——
# 同步路由 `/api/query` 跑在线程池里，也会经 query_service 走到会话记忆。
# 无锁时并发首访会各自构造一个 AsyncIOMotorClient，落败的那个**永不关闭**
# （每个都自带监控线程与连接池）。
_lock = threading.Lock()

# 已保存的 MySQL/PostgreSQL 连接集合名（供历史会话全局复用）
CONNECTIONS_COLLECTION = "connections"


def get_client() -> AsyncIOMotorClient | None:
    """返回（惰性创建的）MongoDB 客户端；URI 非法等初始化失败返回 None。"""
    global _client
    with _lock:
        if _client is None:
            try:
                _client = AsyncIOMotorClient(
                    settings.mongo_uri,
                    serverSelectionTimeoutMS=2000,
                )
            except Exception:
                logger.warning("MongoDB 客户端初始化失败，会话记忆将不可用。", exc_info=True)
                _client = None
        return _client


def get_sessions_collection() -> AsyncIOMotorCollection | None:
    """返回 sessions 集合；MongoDB 不可用时返回 None。"""
    global _collection
    client = get_client()
    if client is None:
        return None
    with _lock:
        if _collection is None:
            _collection = client[settings.mongo_db]["sessions"]
        return _collection


def get_connections_collection() -> AsyncIOMotorCollection | None:
    """返回 connections 集合（已保存的外部数据库连接）；MongoDB 不可用时返回 None。"""
    global _connections_collection
    client = get_client()
    if client is None:
        return None
    with _lock:
        if _connections_collection is None:
            _connections_collection = client[settings.mongo_db][CONNECTIONS_COLLECTION]
        return _connections_collection


async def ensure_indexes() -> None:
    """建立 session_id 唯一索引与 updated_at 降序索引（失败静默忽略）。"""
    col = get_sessions_collection()
    if col is None:
        return
    try:
        await col.create_index([("session_id", 1)], unique=True)
        await col.create_index([("updated_at", -1)])
    except Exception:
        logger.warning("MongoDB 索引建立失败，会话记忆仍可用但无索引加速。", exc_info=True)

    conn = get_connections_collection()
    if conn is None:
        return
    try:
        await conn.create_index([("id", 1)], unique=True)
        await conn.create_index([("created_at", -1)])
    except Exception:
        logger.warning("connections 集合索引建立失败，连接管理仍可用但无索引加速。", exc_info=True)


async def close_client() -> None:
    """关闭客户端连接（应用退出时调用）。"""
    global _client, _collection, _connections_collection
    with _lock:
        if _client is not None:
            _client.close()
            _client = None
            _collection = None
            _connections_collection = None
