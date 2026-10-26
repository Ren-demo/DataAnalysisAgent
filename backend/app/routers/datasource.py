"""数据源管理路由：上传 Excel/CSV 或文档、列出数据源、删除数据源。"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.errors import AppError
from app.database.engine import parse_doc_id
from app.services.connection_service import delete_connection
from app.services.datasource_service import delete_upload_data_source, import_file, list_datasources
from app.services.document_service import delete_document, import_document

router = APIRouter()

logger = logging.getLogger(__name__)

# 文档类扩展名（其余走 Excel/CSV 导入）
_DOC_EXTS = {".md", ".txt", ".pdf", ".docx"}


@router.get("/datasources")
def list_all_datasources() -> dict:
    """列出内置与已上传的数据源（含文档）。"""
    return {"data_sources": list_datasources()}


@router.post("/datasources/upload")
def upload_datasource(file: UploadFile = File(...)) -> dict:
    """上传 Excel/CSV（导入 SQLite 表）或文档（导入为 doc: 数据源），返回 data_source_id。"""
    try:
        ext = Path(file.filename or "").suffix.lower()
        data_source_id = import_document(file) if ext in _DOC_EXTS else import_file(file)
        return {"data_source_id": data_source_id}
    except AppError as e:
        return {"error": e.message, "error_code": e.error_code}
    except Exception:
        # 归一的文案会掩盖真实原因（Milvus 不可用、embedding 报错、SQLite 写失败…），
        # 只有日志能区分。文件名一并记下便于定位。
        logger.exception("上传处理失败：%s", getattr(file, "filename", "?"))
        return {"error": "上传处理失败，请检查文件格式。", "error_code": "SYSTEM_ERROR"}


@router.delete("/datasources/{data_source_id}")
async def remove_datasource(data_source_id: str) -> dict:
    """统一删除数据源（upload / doc / 外部连接），按 id 前缀分流。"""
    if data_source_id.startswith("upload:"):
        ok = await asyncio.to_thread(delete_upload_data_source, data_source_id[len("upload:"):])
    elif data_source_id.startswith("doc:"):
        # 按块条目（doc:<doc_id>:<table>）归一化到父文档后删除
        doc_id = parse_doc_id(data_source_id)[0]
        ok = await asyncio.to_thread(delete_document, doc_id)
    else:
        ok = await delete_connection(data_source_id)  # 复用已有 async 删除
    if not ok:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return {"deleted": True}
