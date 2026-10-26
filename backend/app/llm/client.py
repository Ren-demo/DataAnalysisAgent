"""DeepSeek LLM 客户端封装。

通过 LangChain 的 ChatOpenAI 接入 DeepSeek 的 OpenAI 兼容端点。
未配置 API Key 时抛出 AppError，由上层转为友好的系统错误提示（不设规则兜底）。
"""
from langchain_openai import ChatOpenAI

from app.config import settings
from app.errors import AppError


def get_llm(temperature: float = 0.0) -> ChatOpenAI:
    """构造 DeepSeek ChatOpenAI 客户端；未配置 Key 时抛 AppError。

    temperature 默认 0（确定性，用于 SQL 生成与意图分类）；
    闲聊场景可传入更大值（如 0.7）让语气更自然。
    """
    if not settings.deepseek_api_key:
        raise AppError(
            "智能问答服务暂未开通，请联系管理员完成配置后重试。",
            "NO_API_KEY",
        )

    return ChatOpenAI(
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        temperature=temperature,
        api_key=settings.deepseek_api_key,
        # OpenAI SDK 默认 600s 超时 × 2 次重试：单次调用最长可挂 30 分钟，
        # 而这条链路在 SSE 里是串行的，前端会一直转圈且无任何提示。
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )