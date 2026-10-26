"""会话记忆路由：查询历史会话。"""
from fastapi import APIRouter, HTTPException

from app.schemas import SessionDetail, SessionSummary
from app.services.memory_service import delete_session, get_session, list_sessions

router = APIRouter()


@router.get("/sessions", response_model=list[SessionSummary])
async def list_all_sessions() -> list[SessionSummary]:
    """列出历史会话（按更新时间倒序）。"""
    return await list_sessions()


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def fetch_session(session_id: str) -> SessionDetail:
    """返回单个会话的完整消息。"""
    detail = await get_session(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return detail


@router.delete("/sessions/{session_id}")
async def remove_session(session_id: str) -> dict:
    """永久删除指定会话。"""
    if not await delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}
