"""对话/查询路由：POST /api/query、POST /api/query/stream、POST /api/chat/stream。"""
import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from app.agents.graph import graph as orchestrator_graph
from app.config import settings
from app.database.engine import build_source_schema_text, get_dialect, get_engine
from app.errors import AppError
from app.llm.intent import detect_want_sql
from app.llm.text2sql import extract_sql, generate_sql_stream
from app.schemas import ChatRequest, QueryRequest, QueryResponse
from app.services.memory_service import attach_report, record_turn
from app.services.query_service import execute_sql, run_query

router = APIRouter()

logger = logging.getLogger(__name__)

# 图静默超过该秒数即下发一行 SSE 注释保活（见 config.sse_heartbeat_seconds 的说明）
_HEARTBEAT_SECONDS = settings.sse_heartbeat_seconds


def _map_error(exc: Exception) -> dict:
    """把图内 / 消费侧抛出的异常映射为 error 事件载荷（error_code 契约与改造前一致）。

    原先这段是一串 ``except`` 直接包着 ``async for``；图改跑在独立任务里之后，
    任务内冒泡的异常不会再被消费侧接住，故抽成函数供两处复用。
    """
    if isinstance(exc, AppError):
        return {"type": "error", "message": exc.message, "error_code": exc.error_code}
    if isinstance(exc, SQLAlchemyError):
        return {
            "type": "error",
            "message": "数据源连接失败，请检查数据源配置或网络后重试。",
            "error_code": "DATASOURCE_UNAVAILABLE",
        }
    if isinstance(exc, ValueError):
        # 「未知/失效数据源」（如连接弹窗里删掉了当前选中的连接，顶栏仍停在失效 id）
        # 必须是 DATASOURCE_UNAVAILABLE：前端「修复连接」按钮只认它，
        # 映射成 SYSTEM_ERROR 会让补救入口失灵，用户只能手动重选数据源。
        return {"type": "error", "message": str(exc), "error_code": "DATASOURCE_UNAVAILABLE"}
    # 兜底分支：真正的 500 只在这里被吞成一句通用文案，不留日志就完全查不出来
    logger.error("chat/stream 处理失败。", exc_info=exc)
    return {"type": "error", "message": "系统出现异常，请稍后重试。", "error_code": "SYSTEM_ERROR"}


@router.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    """接收自然语言问题，返回 SQL、结构化结果与图表类型。"""
    return run_query(
        request.question,
        request.data_source_id,
        chart_hint=request.chart_hint,
        include_sql=detect_want_sql(request.question),
    )


def _sse(data: dict) -> str:
    """构造一条 SSE 事件（data: JSON）。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/query/stream")
async def query_stream(request: QueryRequest) -> StreamingResponse:
    """流式查询：逐 token 推送 SQL，最后推送完整结果。"""
    return StreamingResponse(
        _stream_response(request.question, request.data_source_id, request.chart_hint),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/stream")
async def chat_stream_endpoint(request: ChatRequest) -> StreamingResponse:
    """统一对话流式入口：自动判别数据查询 / 闲聊并分流，聚合需求附图表与决策报告。"""
    return StreamingResponse(
        _chat_stream_response(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_response(
    question: str,
    data_source_id: str,
    chart_hint: str | None = None,
):
    """查询流式响应生成器：sql_token 事件流 + 最终 result/error 事件（供 /query/stream 用）。"""
    try:
        # 三个调用都是同步阻塞的（get_engine / build_source_schema_text 会实际读库反射，
        # 后者对 doc 源还可能走 embedding HTTP + Milvus 检索），
        # 在 async 生成器里直接调用会占住事件循环，拖停同进程的其它 SSE 流。
        engine = await asyncio.to_thread(get_engine, data_source_id)
        dialect = get_dialect(data_source_id)
        # 只暴露本数据源的表：upload 源共用 uploads.db、doc 源共用 docs.db，不限定就会串表。
        # 走 build_source_schema_text（而非 build_scoped_schema_text）是为了与
        # /chat/stream 同口径：doc 源先按块 id / 向量定位收窄，再把取值附进 schema 文本。
        schema_text = await asyncio.to_thread(
            build_source_schema_text, engine, data_source_id, question
        )
    except ValueError as e:
        # 「未知/失效数据源」必须是 DATASOURCE_UNAVAILABLE：前端「修复连接」入口
        # 只认这个码（与 DATASOURCE_CONNECT_FAILED），映射成 SYSTEM_ERROR 会让补救按钮失灵。
        yield _sse({"type": "error", "message": str(e), "error_code": "DATASOURCE_UNAVAILABLE"})
        return
    except SQLAlchemyError:
        yield _sse({
            "type": "error",
            "message": "数据源连接失败，请检查数据源配置或网络后重试。",
            "error_code": "DATASOURCE_UNAVAILABLE",
        })
        return

    want_sql = detect_want_sql(question)
    collected: list[str] = []
    try:
        async for token in generate_sql_stream(question, schema_text, dialect):
            collected.append(token)
            if want_sql:
                yield _sse({"type": "sql_token", "content": token})
    except AppError as e:
        yield _sse({"type": "error", "message": e.message, "error_code": e.error_code})
        return

    sql = extract_sql("".join(collected))
    result = await asyncio.to_thread(execute_sql, question, sql, engine, chart_hint, want_sql)
    if result.error:
        yield _sse({
            "type": "error",
            "message": result.error,
            "error_code": result.error_code,
        })
    else:
        yield _sse({"type": "result", "data": result.model_dump()})


async def _chat_stream_response(request: ChatRequest):
    """统一流式响应：交给 LangGraph 编排图（意图分类 → 查询/闲聊分流 → 决策报告）。

    图内节点经 ``get_stream_writer`` 产出的事件 dict 直接作为 SSE 事件，
    旧 5 事件契约不变，新增 ``report`` / ``progress`` 事件（前端对未知事件安全忽略）。
    硬错误（AppError / SQLAlchemyError / ValueError）从节点抛出，在此映射为 error 事件。
    拿到 result 事件即把本轮问答持久化到 MongoDB 会话记忆（report 事件到达后再补写决策报告），
    避免被后续可选 report 节点耗时 / 流中断拖累；写入失败静默。

    **图跑在独立任务里、事件经队列搬运**，生成器按 ``sse_heartbeat_seconds`` 超时取：
    分析 / 决策报告 agent 会连续跑十几轮 LLM 往返且中途一个事件都不下发，直接
    ``async for`` 图的话整段静默，前端空闲超时会把这类健康的长任务单方面掐断。
    静默期间改发一行 SSE 注释（``: keepalive``）——注释行同样让 ``reader.read()``
    返回并重置空闲计时器，而前端 ``consumeSSE`` 与 ``batch_answer.py`` 都已跳过它。
    **不设总时长上限**：答案质量优先，允许跑到自然结束，用户可随时点「停止」。
    """
    initial = {
        "question": request.question,
        "data_source_id": request.data_source_id,
        "history": request.history,
        "session_id": request.session_id,
    }
    queue: asyncio.Queue = asyncio.Queue()
    done = object()  # 哨兵：与事件 dict 区分开，避免用 None 之类可能与事件混淆的值

    async def _run_graph() -> None:
        try:
            async for event in orchestrator_graph.astream(initial, stream_mode="custom"):
                queue.put_nowait(event)
        except Exception as e:
            # 异常必须显式搬上队列：图跑在任务里，任务内冒泡的异常不会传到消费侧的
            # async for，那里只会正常结束 —— 不搬就会「静默无结果也无错误」。
            queue.put_nowait(_map_error(e))
        finally:
            # put_nowait 而非 await put：finally 里不再有挂起点，取消时也一定执行
            queue.put_nowait(done)

    task = asyncio.create_task(_run_graph())
    message_id: str | None = None
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"  # 必须带空行，否则前端切块不会 flush
                continue
            if event is done:
                break
            # 下面两处副作用留在**消费侧**：队列只搬运事件，搬进任务会打乱
            # 「先持久化 result、后补写 report」的既有次序。
            if event.get("type") == "result":
                final_result = event.get("data") or {}
                # 关键修复：拿到最终结果立即持久化，避免被后续可选 report 节点耗时 / 流中断拖累
                if request.session_id and not final_result.get("error"):
                    message_id = await record_turn(
                        request.session_id,
                        request.question,
                        final_result,
                        request.data_source_id,
                    )
            elif event.get("type") == "report":
                # 决策报告到达后补写到对应 assistant 消息（可选、失败静默）
                if message_id:
                    await attach_report(request.session_id, message_id, event.get("data"))
            yield _sse(event)
    except Exception as e:
        # 兜底：上面的 record_turn / attach_report 也可能抛，此前正是被这段接住的
        yield _sse(_map_error(e))
    finally:
        # 客户端断开 / 点「停止」时 StreamingResponse 会关闭本生成器；不取消任务的话
        # 图内的 agent 会继续跑完所有 LLM 往返（真实成本，且结果无人接收）。
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
