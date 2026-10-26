"""FastAPI 应用入口。"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database.engine import dispose_all_engines
from app.database.milvus import dispose_vector_stores
from app.database.mongo import close_client, ensure_indexes
from app.routers import chat, connections, datasource, memory, meta
from app.services.connection_service import seed_runtime_datasources
from app.services.document_service import ensure_docs_schema, reconcile_orphan_vectors

# 全局日志配置：此前项目没有任何 logging 配置，各处 `logger.warning(...)` 的降级记录
# 全都落进「无 handler → lastResort(ERROR)」的黑洞，等于没写。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# 第三方库的 INFO 日志噪音较大（httpx 每次请求、pymilvus 连接细节），只留 WARNING
for _noisy in ("httpx", "httpcore", "openai", "pymilvus", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 MongoDB 会话记忆与已保存连接（失败静默），退出时释放全部资源。

    收尾放在 ``finally`` 里：初始化抛异常时也要走释放路径，否则进程内已建的
    连接池与 Milvus client 会一直挂着。
    """
    try:
        await ensure_indexes()
        await seed_runtime_datasources()
        # 初始化 docs.db 基础表。文档链路的读取（对账 / list_documents / get_doc_tables 等）
        # 都假定 doc_meta / doc_blocks 存在，而此前只有 import_document 会建表 ——
        # 「docs.db 文件已存在但没有表」的环境下，下面的对账会以 no such table 抛出并
        # 打出一整页 traceback。是阻塞 DDL，故移出事件循环；失败不影响启动。
        try:
            await asyncio.to_thread(ensure_docs_schema)
        except Exception:
            logging.getLogger(__name__).warning("docs.db 基础表初始化失败，已跳过。", exc_info=True)
        # 对账孤儿文档向量：清理「SQLite 记录已删、Milvus 向量没删掉」的残留。
        # 会真的连 Milvus（连不上则内部跳过），故移出事件循环；失败不影响启动。
        try:
            await asyncio.to_thread(reconcile_orphan_vectors)
        except Exception:
            logging.getLogger(__name__).warning("孤儿向量对账失败，已跳过。", exc_info=True)
        yield
    finally:
        await close_client()
        dispose_all_engines()
        dispose_vector_stores()


app = FastAPI(title="智能数据分析系统", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meta.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(datasource.router, prefix="/api")
app.include_router(connections.router, prefix="/api")
app.include_router(memory.router, prefix="/api")


@app.get("/")
def root() -> dict:
    return {"name": "智能数据分析系统", "docs": "/docs", "health": "/api/health"}
