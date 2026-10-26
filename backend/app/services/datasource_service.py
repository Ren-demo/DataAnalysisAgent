"""数据源服务：Excel/CSV 文件解析并导入为 SQLite 表。"""
import io
import re
from pathlib import Path

import pandas as pd
from fastapi import UploadFile
from sqlalchemy import inspect, text

from app.config import settings
from app.database.datasource_config import list_external_datasources
from app.database.engine import _engine_kwargs, _get_or_create_engine, invalidate_engine
from app.errors import AppError


def _uploads_engine():
    """uploads.db 的引擎（复用 engine 模块缓存的 ``"uploads"`` 键）。

    早前每个调用点都裸 ``create_engine``：``list_datasources`` 挂在
    ``GET /api/datasources`` / ``GET /api/schema`` 上，每次列表请求都会新建并丢弃一个
    连接池，与 ``engine.py`` 里已有的同名缓存并存两套。
    """
    return _get_or_create_engine("uploads", settings.uploads_db_url, _engine_kwargs("sqlite"))

# 允许的文件扩展名
_ALLOWED_EXTS = {".csv", ".xlsx", ".xls"}


# 标识符中一律替换掉的字符：引号（使标识符无法被安全引用/拼接）
# 与语句分隔符（可截断 SQL），外加控制字符。
_FORBIDDEN_IN_IDENT = re.compile(r'["`;\x00-\x1f\x7f]')


def _clean_identifier(name: str, fallback: str) -> str:
    """把字符串清洗为可安全引用的标识符，尽量保留原始名（中文/空格/数字开头均保留）。

    只替换引号、分号与控制字符，不加 ``t_`` 前缀、不把中文替换成下划线；
    写入 SQLite/外部库时由 SQLAlchemy 自动加引号（非 ASCII / 数字开头 / 含空格会被正确引用）。

    引号必须替换：上传文件名由客户端任意指定，含 ``"`` 的文件名会被 ``to_sql``
    如实建成含引号的物理表名，进而在任何裸拼 SQL 的位置形成注入。
    """
    s = str(name).strip()
    s = _FORBIDDEN_IN_IDENT.sub("_", s)
    s = s.strip()
    if not s:
        s = fallback
    return s


def _sanitize_table_name(filename: str) -> str:
    """由文件名生成合法表名。"""
    return _clean_identifier(Path(filename).stem, "uploaded_data")


def read_upload_limited(file: UploadFile) -> bytes:
    """读取上传文件内容；超过 ``settings.max_upload_mb`` 时抛 AppError。

    只读 ``上限 + 1`` 字节即可判定超限，不会把超大文件整个读进内存。
    先看已声明的 ``size`` 做一次快速拒绝（可能缺失），再按实际读到的长度复核。
    """
    limit = settings.max_upload_mb * 1024 * 1024
    too_large = AppError(
        f"文件过大（超过 {settings.max_upload_mb} MB），请拆分后重试。", "SYSTEM_ERROR"
    )
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared > limit:
        raise too_large
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise too_large
    return content


def _sanitize_columns(columns: list) -> list[str]:
    """清洗列名并去重（重复列追加序号；按大小写不敏感去重以适配 SQLite）。"""
    result: list[str] = []
    seen: set[str] = set()
    for col in columns:
        base = _clean_identifier(col, "col")
        name = base
        n = 1
        while name.lower() in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name.lower())
        result.append(name)
    return result


def _read_frame(filename: str, content: bytes) -> pd.DataFrame:
    """按扩展名读取 DataFrame。"""
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return pd.read_csv(io.BytesIO(content))
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(io.BytesIO(content))
    raise AppError("仅支持 CSV 或 Excel（.xlsx/.xls）文件。", "SYSTEM_ERROR")


def import_file(file: UploadFile) -> str:
    """解析上传文件并导入为 uploads.db 中的一张表，返回 data_source_id。"""
    filename = file.filename or ""
    if Path(filename).suffix.lower() not in _ALLOWED_EXTS:
        raise AppError("仅支持 CSV 或 Excel（.xlsx/.xls）文件。", "SYSTEM_ERROR")

    content = read_upload_limited(file)
    if not content:
        raise AppError("文件内容为空。", "SYSTEM_ERROR")

    try:
        df = _read_frame(filename, content)
    except AppError:
        raise
    except Exception as e:
        raise AppError("文件解析失败，请检查文件格式与内容。", "SYSTEM_ERROR") from e

    if df.empty:
        raise AppError("文件中没有可用数据。", "SYSTEM_ERROR")

    df.columns = _sanitize_columns(list(df.columns))
    table_name = _sanitize_table_name(filename)

    settings.uploads_db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = _uploads_engine()
    with engine.begin() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)

    return f"upload:{table_name}"


def list_datasources() -> list[dict]:
    """列出已上传 + 配置的外部数据源。"""
    result: list[dict] = []
    if settings.uploads_db_path.exists():
        engine = _uploads_engine()
        for table_name in inspect(engine).get_table_names():
            result.append({
                "id": f"upload:{table_name}",
                "name": table_name,
                "type": "upload",
            })
    # 外部配置的数据源（仅元数据，不连接）
    for ds in list_external_datasources():
        result.append({"id": ds.id, "name": ds.name, "type": ds.type})
    # 非结构化文档数据源（doc:<id>；懒加载避免与 document_service 的循环依赖）
    from app.services.document_service import list_documents

    result.extend(list_documents())
    return result


def delete_upload_data_source(table_name: str) -> bool:
    """删除上传数据源（uploads.db 中的一张表）；返回是否删除。"""
    if not settings.uploads_db_path.exists():
        return False
    engine = _uploads_engine()
    if table_name not in inspect(engine).get_table_names():
        return False
    quoted = engine.dialect.identifier_preparer.quote(table_name)
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {quoted}"))
    invalidate_engine("uploads")  # get_engine() 对 upload 用固定 key "uploads"
    return True
