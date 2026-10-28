"""外部数据源配置：从 backend/datasources.json 加载 MySQL/PostgreSQL 连接信息。

配置文件损坏或缺失时返回空列表，绝不抛异常，保证 upload 数据源不受影响。
"""
import logging
import threading
from functools import lru_cache

from pydantic import BaseModel, Field

from app.config import BACKEND_DIR

logger = logging.getLogger(__name__)

# 配置文件路径（datasources.json 位于 backend 根目录；.env 已统一放在项目根目录）
DS_CONFIG_PATH = BACKEND_DIR / "datasources.json"


class ExternalDataSource(BaseModel):
    """一条外部数据源配置。"""

    id: str
    name: str
    type: str
    host: str
    port: int = 3306
    database: str
    user: str
    password: str = ""


class DatasourceConfig(BaseModel):
    """datasources.json 的根结构。"""

    datasources: list[ExternalDataSource] = Field(default_factory=list)


@lru_cache(maxsize=1)
def _load_config() -> DatasourceConfig:
    """加载配置文件（缓存）。损坏/缺失时返回空配置，绝不抛异常。"""
    if not DS_CONFIG_PATH.exists():
        return DatasourceConfig()

    try:
        return DatasourceConfig.model_validate_json(DS_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("datasources.json 解析失败，忽略外部数据源配置：%s", e)
        return DatasourceConfig()


# 运行时（MongoDB）注册的外部数据源：启动时播种，增删改后同步更新。
#
# 写入确实只发生在事件循环线程（lifespan 播种 + async 路由增删），但**读取**不是：
# ``/api/query`` 是同步 ``def``，跑在 Starlette 的线程池里，会经 ``engine.get_datasource_by_id``
# 读到这个注册表。即「跨线程可见性」问题真实存在 —— 早前此处注释断言其不存在，是错的。
# 尤其 ``list_external_datasources`` 的 ``merged.update(_runtime_datasources)`` 会**遍历**该 dict，
# 与另一线程的 ``upsert``/``remove`` 并发时可能抛
# ``RuntimeError: dictionary changed size during iteration``。故读写一律加锁。
_runtime_datasources: dict[str, ExternalDataSource] = {}
_lock = threading.Lock()


def set_runtime_datasources(items: list[ExternalDataSource]) -> None:
    """整体替换运行时数据源注册表（启动播种 / 增删后对账）。"""
    global _runtime_datasources
    with _lock:
        _runtime_datasources = {ds.id: ds for ds in items}


def upsert_runtime_datasource(ds: ExternalDataSource) -> None:
    """新增或覆盖一条运行时数据源。"""
    with _lock:
        _runtime_datasources[ds.id] = ds


def remove_runtime_datasource(ds_id: str) -> None:
    """移除一条运行时数据源。"""
    with _lock:
        _runtime_datasources.pop(ds_id, None)


def get_datasource_by_id(ds_id: str) -> ExternalDataSource | None:
    """按 id 反查外部数据源配置（静态 json 与运行时 Mongo 的并集）；不存在返回 None。"""
    with _lock:
        ds = _runtime_datasources.get(ds_id)
    return ds or _get_static_datasource_by_id(ds_id)


def _get_static_datasource_by_id(ds_id: str) -> ExternalDataSource | None:
    for ds in _load_config().datasources:
        if ds.id == ds_id:
            return ds
    return None


def list_external_datasources() -> list[ExternalDataSource]:
    """返回全部外部数据源：datasources.json 与运行时(MongoDB)的并集，运行时覆盖同 id。"""
    merged: dict[str, ExternalDataSource] = {ds.id: ds for ds in _load_config().datasources}
    with _lock:
        merged.update(_runtime_datasources)  # 加锁：并发 upsert 会让无锁 update 抛 RuntimeError
    return list(merged.values())