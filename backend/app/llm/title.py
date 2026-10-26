"""会话标题生成：把首条用户问题概括为简短标题（LLM 概括 + 截断兜底）。

标题仅用于历史会话列表展示，属于可选增强能力：
LLM 不可用 / 无 API Key / 解析失败时静默回退为截断问题原文，绝不抛出异常打断主链路。
"""
from app.llm.client import get_llm

_TITLE_MAX_LEN = 20

_SYSTEM_PROMPT = (
    "你是会话标题生成器。根据用户的首条问题，生成一个不超过 12 个字的简短标题，"
    "准确概括其核心意图。直接输出标题本身，不要引号、标点或任何额外解释。\n"
    "示例：问题「统计各分类的商品数量」→ 输出「分类商品数量统计」"
)


def _truncate(text: str, max_len: int) -> str:
    """按字符数截断，超长加省略号。"""
    text = (text or "").strip()
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


async def generate_title(question: str) -> str:
    """生成会话标题；LLM 失败时回退为截断首条问题。"""
    q = (question or "").strip()
    if not q:
        return "新对话"
    try:
        llm = get_llm(temperature=0.0)
        resp = await llm.ainvoke(f"{_SYSTEM_PROMPT}\n\n问题：{q}")
        title = str(resp.content or "").strip().strip('"“”‘’')
        if title:
            return _truncate(title, _TITLE_MAX_LEN)
    except Exception:
        # 静默回退：标题仅用于展示，失败不影响主链路
        pass
    return _truncate(q, _TITLE_MAX_LEN)
