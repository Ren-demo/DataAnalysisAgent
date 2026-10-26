"""对话上下文治理：近期窗口截断 + 旧对话滚动摘要，控制闲聊链路 token。

核心思路：
- 历史按条数分成「近期窗口」与「窗口之前的旧对话」两部分；
- 近期窗口直接进 prompt（另有 token 预算做二次裁剪）；
- 旧对话增量折叠进一段滚动摘要（存 MongoDB，见 memory_service），
  以「摘要 + 近期窗口」替代全量历史，既省 token 又保留长期记忆。

全程静默降级：MongoDB / LLM 不可用时回退为纯截断，不影响主链路。
"""
import logging

from app.config import settings
from app.llm.summary import generate_summary
from app.schemas import ChatMessage
from app.services.memory_service import get_summary_state, save_summary_state

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数（DeepSeek 无公开 tokenizer，用保守启发式）。

    CJK 及全角字符 ≈ 1 token/字，其余字符 ≈ 4 字符/token；略高估以便更保守裁剪。
    """
    text = text or ""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _history_tokens(history: list[ChatMessage]) -> int:
    return sum(estimate_tokens(m.content) for m in history)


def trim_history(
    history: list[ChatMessage],
    max_messages: int,
    max_tokens: int,
) -> list[ChatMessage]:
    """裁剪历史：先按条数取最近 ``max_messages`` 条，再按 token 预算从最旧端丢弃。

    ``max_messages`` 是确定性的摘要边界；token 预算仅在近期窗口内部二次裁剪。
    """
    if not history:
        return []
    recent = list(history[-max_messages:]) if max_messages > 0 else list(history)
    while len(recent) > 1 and _history_tokens(recent) > max_tokens:
        recent = recent[1:]
    return recent


async def build_chat_context(
    session_id: str | None,
    history: list[ChatMessage] | None,
) -> dict:
    """组装闲聊上下文：返回 ``{"history": 近期窗口, "summary": 滚动摘要或 None}``。

    摘要触发条件：历史条数超过 ``chat_history_max_messages`` 且提供了 ``session_id``。
    折叠方式为增量式：只把尚未摘要的新旧对话（``old[summary_upto:]``）并入已有摘要。
    """
    history = history or []
    max_messages = settings.chat_history_max_messages
    recent = trim_history(history, max_messages, settings.chat_history_max_tokens)

    summary: str | None = None
    if max_messages > 0 and len(history) > max_messages and session_id:
        old = history[:-max_messages]
        prev_summary, upto, persisted = await get_summary_state(session_id)
        # 折叠边界以**服务端持久化的消息数**为准：history 完全来自客户端请求体，
        # 长度可以随便编；直接拿 len(old) 当边界，等于把水位交给调用方决定。
        if persisted:
            old = old[: max(0, persisted - max_messages)]
        upto = max(0, min(upto, len(old)))
        new_old = old[upto:]
        if new_old:
            generated = await generate_summary(prev_summary, new_old)
            if generated is None:
                # 生成失败：保留旧摘要并**不推进水位**，这批旧对话下次仍会重试折叠。
                # （无条件推进等于把它们永久踢出上下文，见 summary.generate_summary）
                summary = prev_summary
            else:
                # 条件写入：并发两轮读到同一水位时只让先到者写入，避免水位回拨
                await save_summary_state(session_id, generated, len(old), expected_upto=upto)
                summary = generated
        else:
            summary = prev_summary

    return {"history": recent, "summary": summary}
