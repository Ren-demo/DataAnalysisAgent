"""决策报告：结构化报告模型与解析。

报告 agent 使用 ``create_agent``（带工具）做多步下钻推理，最终输出 JSON 报告。
由于 DeepSeek「思考模式」不支持 ``response_format``（强制 tool_choice），
这里采用**手动 JSON 解析**（与 ``app/llm/intent.py`` 的 ``_extract_json`` 同思路），
解析失败则降级为「以原文作摘要」的最小报告，保证报告能力不阻断主结果。
"""
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas import QueryResponse


class ReportResult(BaseModel):
    """决策报告结构化输出（经 SSE ``report`` 事件推送给前端）。"""

    title: str = Field(description="报告标题")
    summary: str = Field(description="核心结论，一段话概括")
    key_findings: list[str] = Field(description="关键发现，3~5 条要点")
    analysis: str | None = Field(default=None, description="详细分析 / 多步推理过程")
    recommendations: list[str] | None = Field(default=None, description="建议 / 下一步行动")
    confidence: Literal["high", "medium", "low"] | None = Field(default=None, description="结论置信度")


_REPORT_SYSTEM_PROMPT = (
    "你是一名资深数据分析师。给定用户的问题、已生成的 SQL 与查询结果，"
    "你可以调用工具（get_schema / generate_sql / execute_sql）做进一步的追问与下钻分析。"
    "如果本次还提供了 web_search（联网搜索）或 knowledge_search（本地知识库检索）工具，"
    "可在需要外部事实或领域知识时调用它们补充信息；工具不可用不影响产出报告。"
    "最后输出一个 JSON 对象作为决策分析报告，字段为："
    "title（报告标题）、summary（核心结论，一段话）、"
    "key_findings（关键发现，3~5 条要点的字符串数组）、"
    "analysis（详细分析，可选）、recommendations（建议/下一步，字符串数组，可选）、"
    "confidence（high / medium / low，可选）。"
    "只输出这个 JSON 对象，不要输出任何其它文字、解释或 Markdown 代码块围栏。"
)


# 报告 agent 输入中最多携带的结果行数，防止大结果撑爆上下文
MAX_REPORT_ROWS = 20


def _build_report_input(result: QueryResponse, sql: str | None = None) -> str:
    """把查询结果整理成报告 agent 的输入文本（截断大结果）。

    ``sql`` 优先取传入值（图内 state.sql，因 result.sql 默认已隐藏），回退到 result.sql。
    """
    shown = result.rows[:MAX_REPORT_ROWS]
    lines = [
        f"用户问题：{result.question}",
        f"执行的 SQL：{sql or result.sql}",
        f"结果列：{result.columns}",
        f"结果行数：{result.row_count}",
        f"结果数据（前 {len(shown)} 行）：",
    ]
    lines.extend(str(row) for row in shown)
    return "\n".join(lines)


def _extract_json(text: str) -> str | None:
    """从文本中提取首个 JSON 对象字符串（剥离 markdown 围栏）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    return text[start : end + 1]


def parse_report(text: str, question: str) -> ReportResult:
    """解析 agent 输出的 JSON 报告；失败时降级为「以原文作摘要」的最小报告。"""
    raw = _extract_json(text)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                confidence = data.get("confidence")
                if confidence not in ("high", "medium", "low"):
                    confidence = None
                return ReportResult(
                    title=str(data.get("title") or "数据分析报告"),
                    summary=str(data.get("summary") or ""),
                    key_findings=[str(x) for x in data.get("key_findings", []) if x],
                    analysis=data.get("analysis"),
                    recommendations=[str(x) for x in data.get("recommendations", []) if x] or None,
                    confidence=confidence,
                )
        except Exception:
            pass
    # 降级：把原始文本当作摘要
    return ReportResult(
        title=question,
        summary=(text or "").strip()[:500],
        key_findings=[],
    )
