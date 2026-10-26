"""滚动摘要生成：把超出近期窗口的旧对话压缩成一段简明摘要，保留长期记忆。

属于可选增强能力：LLM 不可用 / 无 API Key / 解析失败时静默回退（返回旧摘要或空串），
绝不抛出异常打断主链路。
"""
import logging

from app.config import settings
from app.llm.client import get_llm
from app.schemas import ChatMessage

logger = logging.getLogger(__name__)

# 单条消息并入摘要时的字符上限（避免超长回答整段塞进摘要 prompt）
_MESSAGE_MAX_CHARS = 400

_SYSTEM_PROMPT = (
    "你是对话记忆压缩器。把给定对话内容压缩成一段简明摘要，"
    "保留关键事实：用户的数据分析目标、已得出的重要结论、涉及的数据源/表/字段、"
    "以及用户明显的偏好或约定。忽略寒暄与冗余细节。"
)


def _message_to_text(m: ChatMessage) -> str:
    """把单条消息转为「角色：内容」文本，超长截断。"""
    content = (m.content or "").strip()
    if len(content) > _MESSAGE_MAX_CHARS:
        content = content[:_MESSAGE_MAX_CHARS] + "…"
    role = "用户" if m.role == "user" else "助手"
    return f"{role}：{content}"


async def generate_summary(
    previous_summary: str | None,
    new_messages: list[ChatMessage],
) -> str | None:
    """生成/更新滚动摘要；**生成失败返回 None**。

    - 无旧摘要：概括 ``new_messages``。
    - 有旧摘要：把旧摘要与 ``new_messages`` 合并成一段新的简明摘要。
    - 失败返回 ``None``（不再伪装成「摘要就是旧摘要」）。

    返回值必须能区分「成功」与「失败」：调用方要靠它决定是否推进 ``summary_upto``。
    早前失败时返回旧摘要，调用方无从分辨照样推进水位 —— 这批旧对话既没被写进摘要、
    又永远不会再被折叠，等于**静默丢掉一段对话历史**。
    """
    if not new_messages:
        return previous_summary

    transcript = "\n".join(_message_to_text(m) for m in new_messages)
    max_chars = settings.chat_summary_max_chars

    if previous_summary:
        prompt = (
            f"{_SYSTEM_PROMPT}\n"
            f"请输出不超过 {max_chars} 字的摘要。\n\n"
            f"===== 此前摘要 =====\n{previous_summary}\n\n"
            f"===== 新增对话 =====\n{transcript}\n\n"
            "请把此前摘要与新增对话合并为一段不超过 "
            f"{max_chars} 字的简明摘要，只输出摘要本身。"
        )
    else:
        prompt = (
            f"{_SYSTEM_PROMPT}\n"
            f"请输出不超过 {max_chars} 字的摘要。\n\n"
            f"===== 对话 =====\n{transcript}\n\n"
            f"请概括为一段不超过 {max_chars} 字的摘要，只输出摘要本身。"
        )

    try:
        llm = get_llm(temperature=0.0)
        resp = await llm.ainvoke(prompt)
        summary = str(resp.content or "").strip().strip('"“”‘’')
        if summary:
            return summary
    except Exception:
        # 降级：摘要仅用于省 token，失败不影响主链路（但调用方不会推进折叠水位）
        logger.warning("摘要生成失败，本次不推进折叠水位。", exc_info=True)
    return None
