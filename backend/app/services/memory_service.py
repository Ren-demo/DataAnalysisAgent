"""会话记忆服务：把每轮问答持久化到 MongoDB（一条会话一条文档）。

写入/读取失败均静默降级：会话记忆是可选能力，MongoDB 不可用不影响对话主链路。
"""
import asyncio
import datetime
import logging
import uuid
from typing import Any

from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.database.mongo import get_sessions_collection
from app.llm.title import generate_title
from app.schemas import QueryResponse, SessionDetail, SessionMessage, SessionSummary

logger = logging.getLogger(__name__)

_TITLE_MAX_LEN = 30

# 在途的后台标题生成任务。asyncio 只持弱引用，不留强引用的话任务可能在执行中途被 GC。
_pending_titles: set[asyncio.Task] = set()


def _schedule_title(col, session_id: str, question: str) -> None:
    """在**后台**生成并写入会话标题，不阻塞调用方。

    标题生成是一次完整 LLM 往返（1~3s）。record_turn 处于 SSE 主链路上且结果要
    同步回给用户，await 它等于把新会话的第一条 ``result`` 事件压后整整一次 LLM 调用。
    失败照样静默降级：没有 title 时列表接口会回退到首条提问（见 list_sessions）。
    """
    async def _run() -> None:
        try:
            title = await generate_title(question)
            await col.update_one({"session_id": session_id}, {"$set": {"title": title}})
        except Exception:
            logger.warning("会话标题生成失败，已跳过。", exc_info=True)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:
        logger.info("当前无运行中的事件循环，会话标题本次跳过。")
        return
    _pending_titles.add(task)
    task.add_done_callback(_pending_titles.discard)


def _trim_result(result: dict[str, Any]) -> dict[str, Any]:
    """截断**持久化**的结果行，避免会话文档触及 MongoDB 16MB 上限。

    每轮结果经 ``$push`` 全量追加到同一个会话文档的 ``messages`` 数组：一次万行查询
    就足以把文档推过 16MB，此后该会话**每一次** ``$push`` 都会失败，而写入异常被
    静默吞掉 —— 表现为「这个会话的记忆忽然不再更新」，且没有任何报错。

    只留前 ``settings.memory_max_rows`` 行，并把 ``row_count`` 同步改为保留行数
    （全链路不变式是 ``row_count == len(rows)``，前端表格与图表都据此渲染），
    同时置 ``truncated=True`` 让历史恢复时能看出被截断过。
    """
    limit = settings.memory_max_rows
    rows = result.get("rows") or []
    if limit <= 0 or len(rows) <= limit:
        return result
    trimmed = dict(result)
    trimmed["rows"] = rows[:limit]
    trimmed["row_count"] = limit
    trimmed["truncated"] = True
    return trimmed


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value: Any) -> str | None:
    """把 BSON datetime 转为 ISO 字符串（naive 视为 UTC）。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.isoformat()
    return str(value)


async def record_turn(
    session_id: str,
    question: str,
    result: dict[str, Any],
    data_source_id: str,
) -> str | None:
    """把一轮问答写入会话文档（user + assistant 两条），并更新最后使用的数据源。

    返回 assistant 消息的 message_id（供后续补写决策报告定位）；失败返回 None。
    """
    if not session_id:
        return None
    col = get_sessions_collection()
    if col is None:
        return None

    kind = result.get("kind") or "query"
    answer = result.get("answer") if kind == "chat" else result.get("explanation")

    now = _now()
    message_id = str(uuid.uuid4())
    user_msg: dict[str, Any] = {"role": "user", "content": question, "created_at": now}
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": answer or "",
        "kind": kind,
        "data_source_id": data_source_id,
        "message_id": message_id,
        "created_at": now,
    }
    if kind == "query":
        assistant_msg["sql"] = result.get("sql")
        # 截断后再落库：全量 rows 经 $push 累积会撑爆 16MB 文档上限（见 _trim_result）
        assistant_msg["result"] = _trim_result(result)
    elif result.get("sources"):
        # chat（含 RAG 检索）结果：持久化出处引用，供历史会话恢复展示
        assistant_msg["sources"] = result.get("sources")

    update = {
        "$setOnInsert": {"created_at": now},
        "$set": {"last_data_source_id": data_source_id, "updated_at": now},
        "$push": {"messages": {"$each": [user_msg, assistant_msg]}},
    }
    update_result = None
    for attempt in range(2):
        try:
            update_result = await col.update_one({"session_id": session_id}, update, upsert=True)
            break
        except DuplicateKeyError:
            # 并发 upsert 竞态：两个请求都没查到该会话，于是都走插入路径，后到者撞上
            # session_id 唯一索引（先到者刚插进去）。此时文档已存在，重试一次必然走
            # 纯 $push 分支；不重试的话这一轮问答被静默丢弃（表现为「偶发少一轮对话」）。
            if attempt:
                logger.warning("会话 upsert 重试后仍冲突，本轮记忆已丢弃。", exc_info=True)
                return None
            logger.info("会话 upsert 命中唯一索引，重试一次。")
        except Exception:
            logger.warning("会话记忆写入失败，已跳过。", exc_info=True)
            return None
    # 首次落库（新会话）时，用 LLM 概括首条问题生成标题；之后不再改动。
    # 标题生成放后台：它是一次完整 LLM 往返，await 会把本轮的 result 事件压后（见 _schedule_title）。
    if update_result is not None and update_result.upserted_id is not None:
        _schedule_title(col, session_id, question)
    return message_id


async def clear_last_data_source(data_source_id: str) -> int:
    """把各会话的 ``last_data_source_id`` 从已失效的数据源上摘掉；返回改动条数。

    连接被删除时调用。只动会话级的 ``last_data_source_id``（打开会话时自动选中的数据源），
    历史消息里的 ``data_source_id`` **原样保留** —— 那是当时事实的记录，不是配置。
    不清的话，删掉某连接后每次打开旧会话都会自动选回这个失效 id，
    用户得先手动换一个数据源才能提问。失败静默返回 0。
    """
    if not data_source_id:
        return 0
    col = get_sessions_collection()
    if col is None:
        return 0
    try:
        res = await col.update_many(
            {"last_data_source_id": data_source_id},
            {"$set": {"last_data_source_id": None}},
        )
        return res.modified_count
    except Exception:
        logger.warning("清理会话中的失效数据源引用失败，已跳过。", exc_info=True)
        return 0


async def attach_report(session_id: str, message_id: str, report: dict) -> None:
    """把决策报告补写到对应 assistant 消息（可选、失败静默）。"""
    if not session_id or not message_id:
        return
    col = get_sessions_collection()
    if col is None:
        return
    try:
        await col.update_one(
            {"session_id": session_id, "messages.message_id": message_id},
            {"$set": {"messages.$.report": report}},
        )
    except Exception:
        logger.warning("决策报告补写失败，已跳过。", exc_info=True)


async def get_summary_state(session_id: str | None) -> tuple[str | None, int, int]:
    """读取会话的滚动摘要状态：``(summary, summary_upto, persisted_messages)``。

    ``summary_upto`` 表示已折叠进摘要的「窗口之前旧消息」条数；
    ``persisted_messages`` 是服务端**实际持久化**的消息总条数（0 表示读不到）。
    失败返回 ``(None, 0, 0)``。
    """
    if not session_id:
        return None, 0, 0
    col = get_sessions_collection()
    if col is None:
        return None, 0, 0
    try:
        doc = await col.find_one({"session_id": session_id})
        if doc is None:
            return None, 0, 0
        summary = doc.get("summary")
        upto = doc.get("summary_upto")
        return (
            (summary if isinstance(summary, str) else None),
            int(upto) if isinstance(upto, int) else 0,
            len(doc.get("messages") or []),
        )
    except Exception:
        logger.warning("读取摘要状态失败，已跳过。", exc_info=True)
        return None, 0, 0


async def save_summary_state(
    session_id: str | None,
    summary: str,
    upto: int,
    *,
    expected_upto: int | None = None,
) -> bool:
    """写回会话的滚动摘要状态；失败静默，返回是否写入成功。

    ``expected_upto`` 非 None 时做**条件更新**（compare-and-swap）：只有库里
    ``summary_upto`` 仍等于读取时的值才写入。并发两轮闲聊会读到同一个水位，
    无条件写入时后到者可能拿着更旧的边界覆盖先到者，把水位**往回拨**，
    同一批旧对话被反复折叠进摘要。
    """
    if not session_id or not summary:
        return False
    col = get_sessions_collection()
    if col is None:
        return False
    query: dict[str, Any] = {"session_id": session_id}
    if expected_upto is not None:
        if expected_upto > 0:
            query["summary_upto"] = expected_upto
        else:
            # 首次写入：字段可能还不存在，「不存在」与 0 都要匹配
            query["$or"] = [{"summary_upto": {"$exists": False}}, {"summary_upto": 0}]
    try:
        res = await col.update_one(query, {"$set": {"summary": summary, "summary_upto": upto}})
        return res.matched_count > 0
    except Exception:
        logger.warning("写入摘要状态失败，已跳过。", exc_info=True)
        return False


async def get_last_query_result(session_id: str | None) -> dict[str, Any] | None:
    """返回会话最近一次数据查询的结构化结果（columns/rows 等）；无则 None。

    供「重绘图表」复用：从后往前找最近一条带非空 columns/rows 的 assistant 消息
    （query 与重绘结果二者数据一致，均可复用）。失败静默返回 None。
    """
    if not session_id:
        return None
    col = get_sessions_collection()
    if col is None:
        return None
    try:
        doc = await col.find_one({"session_id": session_id})
        if doc is None:
            return None
        for m in reversed(doc.get("messages") or []):
            if m.get("role") != "assistant":
                continue
            result = m.get("result")
            if isinstance(result, dict) and result.get("columns") and result.get("rows"):
                return result
        return None
    except Exception:
        logger.warning("读取最近查询结果失败，已跳过。", exc_info=True)
        return None


async def delete_session(session_id: str) -> bool:
    """按 session_id 永久删除会话；不存在或删除失败返回 False。"""
    if not session_id:
        return False
    col = get_sessions_collection()
    if col is None:
        return False
    try:
        result = await col.delete_one({"session_id": session_id})
        return result.deleted_count > 0
    except Exception:
        logger.warning("会话删除失败。", exc_info=True)
        return False


async def list_sessions() -> list[SessionSummary]:
    """按更新时间倒序返回会话摘要。"""
    col = get_sessions_collection()
    if col is None:
        return []
    try:
        result: list[SessionSummary] = []
        # 用聚合 + projection 只取列表页真正需要的字段：早前 `col.find({})` 把**所有会话的
        # 所有消息**（含每轮完整结果表格）整份读上来，只为算 message_count 与取首条消息，
        # 会话一多 `GET /api/sessions` 就会慢到不可用。
        # `$size` 在库内算条数；`$slice:1` 只带回首条消息供旧文档的标题回退。
        cursor = col.aggregate(
            [
                {"$sort": {"updated_at": -1}},
                {
                    "$project": {
                        "session_id": 1,
                        "title": 1,
                        "last_data_source_id": 1,
                        "created_at": 1,
                        "updated_at": 1,
                        "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                        "head": {"$slice": [{"$ifNull": ["$messages", []]}, 1]},
                    }
                },
            ]
        )
        async for doc in cursor:
            title = doc.get("title")
            if not title:
                # 旧文档未存标题：回退取首条 user 消息截断
                # （record_turn 恒按 [user, assistant] 顺序 push，故首条即首条 user 消息）
                head = doc.get("head") or []
                if head and head[0].get("role") == "user":
                    title = (head[0].get("content") or "").strip()
                if title and len(title) > _TITLE_MAX_LEN:
                    title = title[:_TITLE_MAX_LEN] + "…"
            result.append(
                SessionSummary(
                    session_id=doc["session_id"],
                    title=title or "（空会话）",
                    last_data_source_id=doc.get("last_data_source_id"),
                    message_count=int(doc.get("message_count") or 0),
                    created_at=_iso(doc.get("created_at")),
                    updated_at=_iso(doc.get("updated_at")),
                )
            )
        return result
    except Exception:
        logger.warning("会话列表读取失败，返回空列表。", exc_info=True)
        return []


async def get_session(session_id: str) -> SessionDetail | None:
    """按 session_id 返回完整会话；不存在或读取失败返回 None。"""
    col = get_sessions_collection()
    if col is None:
        return None
    try:
        doc = await col.find_one({"session_id": session_id})
        if doc is None:
            return None
        messages: list[SessionMessage] = []
        for m in doc.get("messages") or []:
            result_raw = m.get("result")
            messages.append(
                SessionMessage(
                    role=m.get("role", "user"),
                    content=m.get("content") or "",
                    kind=m.get("kind"),
                    data_source_id=m.get("data_source_id"),
                    sql=m.get("sql"),
                    result=QueryResponse(**result_raw) if isinstance(result_raw, dict) else None,
                    report=m.get("report"),
                    sources=m.get("sources"),
                    created_at=_iso(m.get("created_at")),
                )
            )
        return SessionDetail(
            session_id=doc["session_id"],
            last_data_source_id=doc.get("last_data_source_id"),
            created_at=_iso(doc.get("created_at")),
            updated_at=_iso(doc.get("updated_at")),
            messages=messages,
        )
    except Exception:
        logger.warning("会话读取失败。", exc_info=True)
        return None
