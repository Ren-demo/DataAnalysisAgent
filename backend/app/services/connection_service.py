"""外部数据库连接管理：MongoDB 持久化 + 内存注册表 + 连接测试。

连接配置存于 MongoDB ``connections`` 集合（password 明文，与 datasources.json 约定一致），
启动时播种进 ``datasource_config`` 的内存注册表，供同步查询链路零阻塞解析。
所有 Mongo 读写失败静默降级，不拖垮主链路。
"""
import asyncio
import datetime
import logging
import uuid

from sqlalchemy.exc import SQLAlchemyError

from app.database.datasource_config import (
    ExternalDataSource,
    remove_runtime_datasource,
    set_runtime_datasources,
    upsert_runtime_datasource,
)
from app.database.engine import (
    build_external_engine,
    external_engine_key,
    get_relationships,
    get_schema_info,
    invalidate_engine,
)
from app.database.mongo import get_connections_collection
from app.errors import AppError
from app.schemas import RelationshipInfo, TableInfo
from app.services.memory_service import clear_last_data_source

logger = logging.getLogger(__name__)


def _new_id() -> str:
    """生成运行时数据源 id（带 ext_ 前缀，避免与静态 datasources.json 的 id 冲突）。"""
    return f"ext_{uuid.uuid4().hex[:12]}"


def _default_port(ds_type: str) -> int:
    """按类型返回缺省端口。"""
    return 5432 if ds_type == "postgresql" else 3306


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ds_from_doc(doc: dict) -> ExternalDataSource:
    """Mongo 文档 → ExternalDataSource。"""
    return ExternalDataSource(
        id=doc["id"],
        name=doc.get("name", ""),
        type=doc.get("type", "mysql"),
        host=doc.get("host", ""),
        port=doc.get("port") or _default_port(doc.get("type", "mysql")),
        database=doc.get("database", ""),
        user=doc.get("user", ""),
        password=doc.get("password", ""),
    )


def _friendly_connect_error(e: Exception) -> str:
    """把连接异常翻译成面向用户的中文提示；绝不回显原始异常（可能含渲染后的 URL/密码）。"""
    # MySQL（pymysql）常用错误码以 int 形式出现在 DBAPI 异常 args[0]
    orig = getattr(e, "orig", None)
    code: object = None
    if orig is not None and isinstance(orig, Exception):
        args = getattr(orig, "args", ()) or ()
        if args and isinstance(args[0], int):
            code = args[0]

    if code == 1045:
        return "连接失败：用户名或密码错误。"
    if code == 1049:
        return "连接失败：数据库不存在，请检查数据库名。"
    if code == 1044:
        return "连接失败：当前用户无权访问该数据库。"
    if code == 2003:
        return "连接失败：无法连接主机/端口，请检查 IP 与端口。"

    name = type(e).__name__
    if name == "ProgrammingError":
        return "连接失败：用户名、密码或数据库名不正确。"
    if name == "OperationalError":
        return "连接失败：无法访问该主机/端口，请检查 IP、端口与网络。"
    return "连接失败：请检查主机、端口、用户名、密码与数据库名是否正确。"


def _inspect_external(ds: ExternalDataSource) -> tuple[list[TableInfo], list[RelationshipInfo]]:
    """真实连接并读取实时表结构（表 + 字段 + 关联）；连接失败抛 AppError(DATASOURCE_CONNECT_FAILED)。"""
    engine = build_external_engine(ds)
    try:
        tables = get_schema_info(engine)
        rels = [RelationshipInfo(**r) for r in get_relationships(engine)]
    except SQLAlchemyError as e:
        raise AppError(_friendly_connect_error(e), "DATASOURCE_CONNECT_FAILED") from e
    except Exception as e:
        raise AppError(
            "连接失败：请检查主机、端口、用户名、密码与数据库名是否正确。",
            "DATASOURCE_CONNECT_FAILED",
        ) from e
    finally:
        engine.dispose()
    return tables, rels


async def inspect_external(ds: ExternalDataSource) -> tuple[list[TableInfo], list[RelationshipInfo]]:
    """异步包装连接测试，避免阻塞事件循环。"""
    return await asyncio.to_thread(_inspect_external, ds)


async def _read_saved_connections() -> list[ExternalDataSource] | None:
    """读取 Mongo 中全部已保存连接；**读不到时返回 None**（区别于「读到了，但是空」）。

    这个区分是必须的：``_reload_registry`` 拿这个结果去**整体替换**内存注册表，
    把「读失败」也当成「没有连接」的话，Mongo 一抖动（未启动、网络闪断、鉴权过期）
    所有已保存的外部连接会瞬间变成「未知数据源」，正在用它们的查询全部报错。
    """
    col = get_connections_collection()
    if col is None:
        return None  # Mongo 未配置/未连上：同样是「读不到」，不是「没有连接」
    try:
        result: list[ExternalDataSource] = []
        cursor = col.find({}).sort("created_at", -1)
        async for doc in cursor:
            result.append(_ds_from_doc(doc))
        return result
    except Exception:
        logger.warning("读取连接列表失败。", exc_info=True)
        return None


async def list_saved_connections() -> list[ExternalDataSource]:
    """读取 Mongo 中全部已保存连接（内部含 password）。Mongo 不可用返回 []。"""
    return await _read_saved_connections() or []


async def get_saved_connection(ds_id: str) -> ExternalDataSource | None:
    """按 id 读取单条已保存连接；不存在或 Mongo 不可用返回 None。"""
    col = get_connections_collection()
    if col is None:
        return None
    try:
        doc = await col.find_one({"id": ds_id})
    except Exception:
        logger.warning("读取连接失败。", exc_info=True)
        return None
    return _ds_from_doc(doc) if doc else None


async def _reload_registry() -> None:
    """以 Mongo 现有内容整体对账内存注册表；**读失败时保持现有注册表不动**。

    调用方在本函数之前已先行 ``upsert``/``remove`` 过注册表（见 save/delete），
    故即便这里读失败，用户本次的增删也已生效，不会出现「删了但还能用」。
    """
    items = await _read_saved_connections()
    if items is None:
        logger.warning("连接列表读取失败，保留现有运行时数据源注册表。")
        return
    set_runtime_datasources(items)


async def seed_runtime_datasources() -> None:
    """启动播种：把 Mongo 中已保存连接载入内存注册表（失败静默）。"""
    try:
        await _reload_registry()
    except Exception:
        logger.warning("运行时数据源播种失败，已跳过。", exc_info=True)


async def save_connection(ds: ExternalDataSource) -> ExternalDataSource | None:
    """写入/更新 Mongo（按 id upsert）并同步注册表；Mongo 不可用返回 None。"""
    col = get_connections_collection()
    if col is None:
        return None
    if not ds.id:
        ds = ds.model_copy(update={"id": _new_id()})
    payload = {
        "name": ds.name,
        "type": ds.type,
        "host": ds.host,
        "port": ds.port,
        "database": ds.database,
        "user": ds.user,
        "password": ds.password,
    }
    try:
        await col.update_one(
            {"id": ds.id},
            {"$set": payload, "$setOnInsert": {"created_at": _now_iso()}},
            upsert=True,
        )
    except Exception:
        logger.warning("连接保存失败。", exc_info=True)
        return None
    # 先直接落进注册表：本次保存**立即**可解析，不依赖下面那次整体对账成功
    upsert_runtime_datasource(ds)
    await _reload_registry()      # 再对账：补上启动时因 Mongo 不可用而漏载的旧连接
    invalidate_engine(external_engine_key(ds.id))  # 同 id 重新保存后使用新凭据
    return ds


async def delete_connection(ds_id: str) -> bool:
    """删除连接（Mongo + 注册表 + 引擎缓存）；返回是否删除。"""
    col = get_connections_collection()
    if col is None:
        return False
    try:
        res = await col.delete_one({"id": ds_id})
    except Exception:
        logger.warning("连接删除失败。", exc_info=True)
        return False
    if res.deleted_count:
        # 先直接摘掉：即便随后对账因 Mongo 读取失败而跳过，本条也不会残留在注册表里
        remove_runtime_datasource(ds_id)
        await _reload_registry()
        invalidate_engine(external_engine_key(ds_id))
        # 历史会话若把它记作 last_data_source_id，打开旧会话会自动选回这个已失效的 id
        await clear_last_data_source(ds_id)
    return res.deleted_count > 0
