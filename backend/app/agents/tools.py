"""决策报告 agent 的工具集。

以闭包按请求现建（``build_tools(engine, dialect, schema_text)``），
让工具捕获当前数据源的引擎/方言/表结构，避免任何全局可变状态。
``execute_sql`` 工具强制经 ``validate_sql`` 只读守卫（只读查询，递归拒绝写操作），
且数据库执行失败时返回**真实错误**（而非吞掉），便于 agent 自我纠正。
"""
import asyncio
import json
import urllib.parse
import urllib.request

import pandas as pd
from langchain_core.tools import tool
from sqlalchemy import Engine

from app.config import settings
from app.llm.text2sql import generate_sql as _generate_sql, validate_sql
from app.services.knowledge_service import load_corpus, search_knowledge
from app.services.query_service import _to_json_safe

# 单次工具执行返回的最大行数
MAX_ROWS = 50


def _duckduckgo_search(query: str, top_k: int = 5) -> str:
    """免 key 的 DuckDuckGo 即时回答检索（stdlib urllib，无新依赖）。"""
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))

    lines: list[str] = []
    abstract = (data.get("AbstractText") or "").strip()
    if abstract:
        lines.append(abstract)
    for topic in (data.get("RelatedTopics") or [])[:top_k]:
        if isinstance(topic, dict) and topic.get("Text"):
            lines.append(topic["Text"])
    return "\n".join(lines) if lines else "未获取到相关搜索结果。"


@tool
async def web_search(query: str) -> str:
    """联网搜索当前问题相关的外部信息；不可用或失败时返回提示，不影响后续推理。"""
    provider = settings.web_search_provider
    if provider == "none":
        return "联网搜索未启用，请基于已有数据回答。"

    def _run() -> str:
        if provider == "tavily" and settings.tavily_api_key:
            from tavily import TavilyClient  # type: ignore

            client = TavilyClient(api_key=settings.tavily_api_key)
            results = client.search(query, max_results=settings.web_search_max_results)
            items = results.get("results") or []
            if not items:
                return "未获取到相关搜索结果。"
            return "\n".join(
                f"{r.get('title', '')}: {r.get('content', '')}" for r in items
            )
        return _duckduckgo_search(query, settings.web_search_max_results)

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"联网搜索失败：{type(e).__name__}，请基于已有数据回答。"


@tool
def knowledge_search(query: str) -> str:
    """在本地知识库中检索与问题相关的段落；未启用或无内容时返回提示。"""
    if not settings.knowledge_base_enabled:
        return "本地知识库未启用。"
    result = search_knowledge(query)
    return result or "本地知识库暂无相关内容。"


def build_tools(engine: Engine, dialect: str, schema_text: str) -> list:
    """按当前数据源现建一组工具：get_schema / generate_sql / execute_sql，
    并视配置追加 web_search / knowledge_search（可选，静默降级）。"""

    @tool
    def get_schema() -> str:
        """返回当前数据源的表结构（CREATE TABLE 文本），供理解字段与生成 SQL 使用。"""
        return schema_text

    @tool
    def generate_sql(question: str) -> str:
        """把自然语言问题转换为一条只读 SELECT（few-shot + sqlglot 校验 + 自动重试）。"""
        return _generate_sql(question, schema_text, dialect)

    @tool
    async def execute_sql(sql: str) -> dict:
        """对当前数据源执行一条只读 SELECT，返回列名与行数据；失败时返回真实数据库错误。"""
        try:
            validate_sql(sql, dialect)  # 只读守卫：递归拒绝写操作（含 CTE 内的 DML）
        except Exception as e:
            return {"ok": False, "error": f"SQL 未通过只读校验：{e}"}
        try:
            df = await asyncio.to_thread(pd.read_sql_query, sql, engine)
        except Exception as e:
            return {"ok": False, "error": f"数据库执行失败：{type(e).__name__}: {e}"}

        columns = [str(c) for c in df.columns]
        rows = [
            [_to_json_safe(v) for v in row]
            for row in df.itertuples(index=False, name=None)
        ][:MAX_ROWS]
        return {"ok": True, "columns": columns, "rows": rows, "row_count": len(rows)}

    tools = [get_schema, generate_sql, execute_sql]
    # 联网搜索：provider 为 none 时不注册（静默跳过）
    if settings.web_search_provider != "none":
        tools.append(web_search)
    # 知识库检索：启用且目录里确有内容才注册
    if settings.knowledge_base_enabled and load_corpus():
        tools.append(knowledge_search)
    return tools
