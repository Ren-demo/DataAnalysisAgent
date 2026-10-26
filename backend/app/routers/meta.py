"""元信息路由：健康检查、表结构。"""
from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from app.database.engine import get_engine, get_relationships, get_schema_info
from app.schemas import DataSourceInfo, RelationshipInfo, SchemaResponse
from app.services.datasource_service import list_datasources

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/schema", response_model=SchemaResponse)
def schema() -> SchemaResponse:
    data_sources = []
    for ds in list_datasources():
        try:
            engine = get_engine(ds["id"])
            tables = get_schema_info(engine)
            relationships = [RelationshipInfo(**r) for r in get_relationships(engine)]
        except SQLAlchemyError:
            # 外部数据源连接失败：标记不可用，不整体 500
            data_sources.append(
                DataSourceInfo(
                    id=ds["id"],
                    name=ds["name"],
                    type=ds["type"],
                    tables=[],
                    relationships=[],
                    available=False,
                )
            )
            continue
        data_sources.append(
            DataSourceInfo(
                id=ds["id"],
                name=ds["name"],
                type=ds["type"],
                tables=tables,
                relationships=relationships,
            )
        )
    return SchemaResponse(data_sources=data_sources)
