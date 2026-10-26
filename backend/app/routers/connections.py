"""外部数据库连接管理路由：增删改查 + 连接测试。"""
from fastapi import APIRouter, HTTPException

from app.database.datasource_config import ExternalDataSource
from app.errors import AppError
from app.schemas import (
    ConnectionCreate,
    ConnectionCreateResponse,
    ConnectionInfo,
    ConnectionTestResponse,
)
from app.services.connection_service import (
    _default_port,
    delete_connection,
    get_saved_connection,
    inspect_external,
    list_saved_connections,
    save_connection,
)

router = APIRouter()


def _ds_from_create(req: ConnectionCreate, ds_id: str = "") -> ExternalDataSource:
    """把表单输入转成 ExternalDataSource（port 缺省按 type 补 3306/5432）。"""
    return ExternalDataSource(
        id=ds_id,
        name=req.name,
        type=req.type,
        host=req.host,
        port=req.port or _default_port(req.type),
        database=req.database,
        user=req.user,
        password=req.password,
    )


@router.get("/connections", response_model=list[ConnectionInfo])
async def list_all_connections() -> list[ConnectionInfo]:
    """列出已保存连接（不含密码）。"""
    saved = await list_saved_connections()
    return [
        ConnectionInfo(
            id=ds.id,
            name=ds.name,
            type=ds.type,
            host=ds.host,
            port=ds.port,
            database=ds.database,
            user=ds.user,
        )
        for ds in saved
    ]


@router.post("/connections/test", response_model=ConnectionTestResponse)
async def test_connection_endpoint(req: ConnectionCreate) -> ConnectionTestResponse:
    """仅测试连接（不保存），返回实时表结构或友好错误。"""
    try:
        tables, rels = await inspect_external(_ds_from_create(req))
    except AppError as e:
        return ConnectionTestResponse(ok=False, error=e.message, error_code=e.error_code)
    return ConnectionTestResponse(ok=True, tables=tables, relationships=rels)


@router.post("/connections", response_model=ConnectionCreateResponse)
async def create_connection(req: ConnectionCreate) -> ConnectionCreateResponse:
    """先测通再保存，返回新连接与实时表结构；失败内联友好错误。"""
    ds = _ds_from_create(req)
    try:
        tables, rels = await inspect_external(ds)
    except AppError as e:
        return ConnectionCreateResponse(error=e.message, error_code=e.error_code)

    saved = await save_connection(ds)
    if saved is None:
        return ConnectionCreateResponse(
            error="连接保存失败，请检查 MongoDB 服务是否可用。",
            error_code="SYSTEM_ERROR",
        )
    return ConnectionCreateResponse(
        id=saved.id, name=saved.name, type=saved.type, tables=tables, relationships=rels
    )


@router.put("/connections/{ds_id}", response_model=ConnectionCreateResponse)
async def update_connection(ds_id: str, req: ConnectionCreate) -> ConnectionCreateResponse:
    """编辑已有连接（password 留空保留旧密码）；先测通再覆盖写。"""
    existing = await get_saved_connection(ds_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="连接不存在")

    password = req.password if req.password else existing.password
    ds = ExternalDataSource(
        id=ds_id,
        name=req.name,
        type=req.type,
        host=req.host,
        port=req.port or _default_port(req.type),
        database=req.database,
        user=req.user,
        password=password,
    )
    try:
        tables, rels = await inspect_external(ds)
    except AppError as e:
        return ConnectionCreateResponse(error=e.message, error_code=e.error_code)

    saved = await save_connection(ds)
    if saved is None:
        return ConnectionCreateResponse(
            error="连接保存失败，请检查 MongoDB 服务是否可用。",
            error_code="SYSTEM_ERROR",
        )
    return ConnectionCreateResponse(
        id=saved.id, name=saved.name, type=saved.type, tables=tables, relationships=rels
    )


@router.delete("/connections/{ds_id}")
async def remove_connection(ds_id: str) -> dict:
    """删除连接（Mongo + 注册表 + 引擎缓存）。"""
    if not await delete_connection(ds_id):
        raise HTTPException(status_code=404, detail="连接不存在")
    return {"deleted": True}
