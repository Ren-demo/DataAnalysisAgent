"""LangGraph 编排图：意图分类 → 查询/闲聊/重绘分流 → SQL 流式生成 → 执行 → 决策报告。

图拓扑：

    START → classify ──chat──────▶ chat ──▶ END
                    ├─retrieval─▶ retrieve ──▶ END
                    ├─schema────▶ schema ──▶ END
                    ├─query─────▶ prepare_query → text2sql → execute → report → END
                    ├─analysis──▶ prepare_query → analysis ──▶ END
                    └─redraw────▶ redraw ──▶ END

- 保留 few-shot text2sql + sqlglot 只读校验（作为专用节点），保障「查询准确性」。
- ``redraw`` 分支复用本会话最近一次查询结果出图，不再生成/执行 SQL。
- 决策报告用 ``create_agent`` 工具调用智能体（多步下钻推理），产出 ``report`` 事件。
- 统一 ``stream_mode="custom"`` + ``get_stream_writer`` 输出 SSE 事件，契约由路由层映射。

**节点内的同步阻塞调用一律经 ``asyncio.to_thread`` 移出事件循环**：意图分类（一次
LLM HTTP 往返，2~5s）、SQL 执行（pandas 读全量结果）、schema 反射、embedding + Milvus
检索、知识库语料加载都是同步实现，直接在图节点里调用会把**整个进程**的事件循环占住，
同进程所有其它请求（含正在逐 token 输出的 SSE 流）全部停摆。
"""
import asyncio
import logging

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.analysis import build_analysis_system_prompt, parse_analysis
from app.agents.report import _REPORT_SYSTEM_PROMPT, _build_report_input, parse_report
from app.agents.state import OrchestratorState
from app.agents.stat_tools import build_stat_tools
from app.agents.tools import build_tools
from app.config import settings
from app.database.engine import (
    build_scoped_schema_text,
    build_source_schema_text,
    extract_block_id,
    get_dialect,
    get_engine,
    locate_doc_tables,
    parse_doc_id,
    resolve_data_source_type,
    resolve_table_names,
)
from app.llm.client import get_llm
from app.llm.intent import (
    IntentResult,
    classify_intent,
    detect_answer_format,
    render_list_answer,
)
from app.llm.text2sql import _response_to_text, extract_sql, generate_sql_stream
from app.schemas import QueryResponse, SourceRef
from app.services.chart_service import suggest_chart_type
from app.services.chat_service import chat_stream, retrieval_stream, schema_consult_stream, summarize_query_result
from app.services.context_service import build_chat_context
from app.services.document_service import (
    get_doc_table_by_block,
    get_doc_tables,
    retrieve_doc,
)
from app.services.memory_service import get_last_query_result
from app.services.query_service import execute_sql
from app.services.schema_service import (
    answer_schema_question,
    answer_schema_question_multi,
    build_schema_overview,
)

logger = logging.getLogger(__name__)


async def _classify_node(state: OrchestratorState) -> dict:
    """意图分类：query / chat / redraw / retrieval / schema + 显式图表类型。"""
    w = get_stream_writer()
    data_source_id = state["data_source_id"]
    # 本节点是每条请求的第一站：分类内部是一次**同步** LLM 往返（2~5s），
    # 不移出事件循环就会让并发请求彻底退化为串行。
    #
    # 数据源解析失败（连接在弹窗里被删掉、顶栏仍停在失效 id、Mongo 抖动导致注册表没播种上）
    # **不在此处抛**：本节点是闲聊/检索/查询的共同入口，在这里抛会让「你好」这种
    # 根本不需要数据源的提问也一并失败。降级为「类型未知」（只影响 doc 源的 retrieval 细分），
    # 真正需要数据源的分支（_prepare_query_node → get_engine）会在那里抛，
    # 由路由层映射成 DATASOURCE_UNAVAILABLE。
    try:
        data_source_type = await asyncio.to_thread(resolve_data_source_type, data_source_id)
    except ValueError:
        logger.info("数据源 %s 无法解析，本节点按类型未知处理。", data_source_id, exc_info=True)
        data_source_type = None
    intent = await asyncio.to_thread(
        classify_intent, state["question"], state.get("history"), data_source_type
    )

    # 文档源且无提取出的表格时，聚合/分析也无法走 SQL 或 pandas，强制降级为检索
    if data_source_type == "doc" and intent.intent in ("query", "analysis"):
        doc_id = parse_doc_id(data_source_id)[0]
        if not await asyncio.to_thread(get_doc_tables, doc_id):
            intent = IntentResult(intent="retrieval")

    # 前端据此前置等待文案（检索文档 / 读取表结构 / 生成 SQL / 数据分析 / 思考）。
    # 早前把 retrieval/schema 压平成 chat，前端无从区分「文档检索」与「闲聊」，
    # 而占位文案的兜底又是「非 chat 即 SQL」—— 文档问答的前几秒会显示「正在生成 SQL…」。
    # analysis 同理必须原样透传：它一次 SQL 都不生成（走 pandas 统计算子），
    # 压成 query 会让分析问题全程显示「正在生成 SQL…」。redraw 仍是 SQL/表格家族。
    # 注意：``QueryResponse.kind``（result 事件的渲染家族）不受影响，仍只发 query/chat。
    frontend_intent = (
        intent.intent
        if intent.intent in ("chat", "retrieval", "schema", "analysis")
        else "query"
    )
    w({"type": "intent", "intent": frontend_intent, "chart_hint": intent.chart_hint})
    return {
        "intent": intent.intent,
        "chart_hint": intent.chart_hint,
        "want_sql": intent.want_sql,
        "answer_format": detect_answer_format(state["question"]),
        "data_source_type": data_source_type,
        "schema_question": intent.schema_question,
    }


#: classify 之后的分支表：**键是意图名，值是节点名**（与 ``_route`` 的返回值一一对应，
#: ``_route_after_prepare`` 的表同样如此）。
#: 抽成模块常量是为了让 ``scripts/test_intent_routing.py`` 能断言「_route 返回的每个键
#: 都在表里」—— 早前 retrieval 分支返回的是节点名 ``"retrieve"`` 而键是 ``"retrieval"``，
#: LangGraph 在 ``_branch._finish`` 里查表直接 ``KeyError: 'retrieve'``，整条 RAG 链路
#: （文档问答）每次都崩成 SYSTEM_ERROR，而类型检查与单测都看不见。
_CLASSIFY_BRANCHES = {
    "query": "prepare_query",
    "chat": "chat",
    "redraw": "redraw",
    "retrieval": "retrieve",
    "schema": "schema",
    "analysis": "prepare_query",
}


def _route(state: OrchestratorState) -> str:
    """按意图分流，返回 ``_CLASSIFY_BRANCHES`` 的键（**不是**节点名）。"""
    intent = state.get("intent")
    if intent == "chat":
        return "chat"
    if intent == "retrieval":
        return "retrieval"
    if intent == "redraw":
        return "redraw"
    if intent == "schema":
        return "schema"
    if intent == "analysis":
        return "analysis"
    return "query"


def _route_after_prepare(state: OrchestratorState) -> str:
    """prepare_query 之后分流：analysis 走数据分析节点，其余走 text2sql。"""
    return "analysis" if state.get("intent") == "analysis" else "query"


async def _prepare_query_node(state: OrchestratorState) -> dict:
    """解析数据源：引擎 / 方言 / 表结构（未知数据源或连接失败时抛异常，由路由层映射 error）。

    schema 文本统一由 ``engine.build_source_schema_text`` 产出（全链路唯一入口）：
    文档源多表时先经 ``locate_doc_tables`` 收窄到候选表，再在表数有界时**附上取值区**。
    这是 query 与 analysis 两条分支的共同前置，二者都靠这份 schema 选表
    （text2sql 生成 FROM、agent 决定 load_table 哪张）。
    """
    ds = state["data_source_id"]
    # get_engine 会真的读库（uploads 源要列表名、doc 源要查 doc_meta），
    # build_source_schema_text 更是一轮全库反射 + 可能的 embedding/Milvus 检索
    # —— 都必须移出事件循环。
    engine = await asyncio.to_thread(get_engine, ds)
    dialect = get_dialect(ds)
    schema_text = await asyncio.to_thread(build_source_schema_text, engine, ds, state["question"])
    return {"engine": engine, "dialect": dialect, "schema_text": schema_text}


async def _text2sql_node(state: OrchestratorState) -> dict:
    """流式生成 SQL：完成后返回完整 SQL；仅在用户要求时逐 token 输出 sql_token。"""
    w = get_stream_writer()
    parts: list[str] = []
    async for token in generate_sql_stream(state["question"], state["schema_text"], state["dialect"]):
        parts.append(token)
        if state.get("want_sql"):
            w({"type": "sql_token", "content": token})
    return {"sql": extract_sql("".join(parts))}


#: progress 事件上限：一次分析最多播报这么多行。
#: 超限后不再下发 —— 进度提示是给人看的，刷屏反而看不清，也避免事件风暴。
_MAX_PROGRESS = 40

#: 工具名 → 中文动作，供 progress 文案使用（未登记的工具名原样展示）
_TOOL_LABELS = {
    "get_schema": "读取表结构",
    "load_table": "加载数据",
    "group_aggregate": "分组聚合",
    "group_correlate": "分组相关性",
    "group_mom_growth": "环比增长量",
    "group_mom_growth_rate": "环比增长率",
    "group_cagr": "年均增长率",
    "group_share": "占比",
    "group_vs_overall_avg": "与整体均值对比",
    "intersect_groups": "多条件求交",
    "linear_regression": "线性回归",
    "forecast_time_series": "时序预测",
    "detect_anomalies": "异常检测",
    "cluster_kmeans": "K-Means 聚类",
    "generate_sql": "生成 SQL",
    "execute_sql": "执行 SQL",
    "web_search": "联网检索",
    "knowledge_search": "知识库检索",
}


def _tool_label(name: str) -> str:
    """工具名的中文展示名（未登记的原样返回，不编造）。"""
    return _TOOL_LABELS.get(name, name or "工具")


def _ai_text(msg) -> str:
    """取 AI 消息的文本内容；带工具调用的消息不是最终答案，返回空串。"""
    if not isinstance(msg, AIMessage) or getattr(msg, "tool_calls", None):
        return ""
    return _response_to_text(msg).strip()


def _progress_text(msg) -> str:
    """由一条消息推出进度文案；非 AI/工具消息返回空串（不播报）。"""
    calls = getattr(msg, "tool_calls", None)
    if calls:
        # 无名调用直接跳过：`_tool_label("")` 会兜成「工具」，播出来就是
        # 「正在调用 工具…」这种零信息量的行 —— 形状不符时宁可静默
        names = [
            _tool_label(c.get("name", ""))
            for c in calls
            if isinstance(c, dict) and c.get("name")
        ]
        if not names:
            return ""
        if len(names) == 1:
            return f"正在调用 {names[0]}…"
        return f"正在并行调用 {('、'.join(names))}…"
    if isinstance(msg, ToolMessage):
        return f"{_tool_label(getattr(msg, 'name', '') or '')} 已完成"
    return ""


async def _run_agent_streamed(agent, content: str, w, scope: str) -> str:
    """流式跑 agent，边跑边下发 progress 事件；返回最终 AI 文本。

    等价于原先的 ``out = await agent.ainvoke(...); text = str(out["messages"][-1].content)``，
    但把 ReAct 循环**边跑边播报** —— 分析与报告 agent 会连跑十几轮 LLM 往返（每轮
    30~60s），全程零事件时前端只看到一片空白，正是被空闲超时掐断的那一段。

    进度事件形如 ``{"type": "progress", "scope": scope, "text": "正在调用 group_cagr…"}``；
    ``scope`` 取 ``analysis`` / ``report``，供前端决定显示位置。

    两条稳健性约定：
    - ``create_agent`` 编译出的内部节点名（``model`` / ``tools``）属实现细节，故按
      ``chunk.values()`` 泛化遍历、按**消息类型**判分支，不硬编码节点名；
    - 分片形状不符时**静默跳过**（只记 debug 日志），绝不打断执行 —— 进度提示是
      锦上添花，任何解析异常都不该影响结果。同理 event 下发本身失败也吞掉。
    """
    final_text = ""
    emitted = 0
    seen_ids: set[str] = set()

    def _emit(text: str) -> None:
        nonlocal emitted
        if not text or emitted >= _MAX_PROGRESS:
            return
        emitted += 1
        try:
            w({"type": "progress", "scope": scope, "text": text})
        except Exception:
            logger.debug("progress 事件下发失败，已忽略。", exc_info=True)

    # 首条：intent 之后到第一次工具调用之间还有一整轮 LLM（约 30s），不报就仍是静默
    _emit("正在规划分析步骤…" if scope == "analysis" else "正在准备决策报告…")

    async for chunk in agent.astream(
        {"messages": [HumanMessage(content=content)]},
        # 步数兜底（不是时间预算）：langgraph 的默认是 10000 等于不设限，
        # 心跳保活把「被杀掉」这条天然的止损路径去掉之后，跑飞的 agent 会无限期烧 token。
        config={"recursion_limit": settings.agent_recursion_limit},
        stream_mode="updates",
    ):
        try:
            updates = chunk.values() if isinstance(chunk, dict) else []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                for msg in update.get("messages") or []:
                    mid = getattr(msg, "id", None)
                    if mid:
                        # 同一条消息可能在多个节点的 update 里重复出现，只播报一次
                        if mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                    text = _progress_text(msg)
                    if text:
                        _emit(text)
                    ai_text = _ai_text(msg)
                    if ai_text:
                        final_text = ai_text
        except Exception:
            logger.debug("解析 agent 流式分片失败，跳过该分片。", exc_info=True)

    if not final_text:
        # 拿不到最终文本属异常（正常 ReAct 收尾必有一条无工具调用的 AI 消息）：
        # 抛出让调用方回退 —— 静默返回空串会被 parse_analysis 解析成空答案，
        # 表现为「模型算了半天最后给了个空结果」，比明确回退更难查。
        raise RuntimeError("agent 未返回可解析的文本结果。")
    return final_text


async def _analysis_node(state: OrchestratorState) -> dict:
    """数据分析：create_agent + 统计算子工具多步计算（相关性/环比/占比等），失败回退 text2sql。

    统计建模问题（相关性/环比/同比/时序等）SQL 难以表达，改由 pandas 计算；
    analysis 是增强路径：任何失败都回退到既有 text2sql 查询链路，不阻断。
    """
    w = get_stream_writer()
    question = state["question"]
    engine = state["engine"]
    dialect = state["dialect"]
    schema_text = state["schema_text"]
    fmt = state.get("answer_format")

    try:
        tools = build_stat_tools(engine, dialect, schema_text)
        agent = create_agent(
            model=get_llm(temperature=0.0),
            tools=tools,
            # 表结构直接内联进 system prompt，省掉「先调 get_schema」的整轮往返
            system_prompt=build_analysis_system_prompt(schema_text),
        )

        # 用户要求/隐含列表或 JSON 格式时，显式追加输出要求，引导 agent 只输出名称列表
        content = question
        if fmt == "list":
            content += (
                "\n\n【输出要求】请把 answer 字段只设为满足条件项名称的 Python 列表"
                "（如 ['Latte', 'Americano with Milk']），无匹配则为空列表 []，"
                "不要附加任何解释文字；items 仍保留明细。"
            )
        elif fmt == "json":
            content += (
                "\n\n【输出要求】请把 answer 字段只设为满足条件项名称的 JSON 数组"
                '（如 ["Latte", "Americano with Milk"]），无匹配则为 []；items 仍保留明细。'
            )
        elif fmt == "single":
            content += (
                "\n\n【输出要求】这是单答案问题，请把 answer 字段只设为唯一满足条件项的名称"
                "（字符串，如 'Latte'），不要用列表，不要附加解释；items 仍保留明细。"
            )

        text = await _run_agent_streamed(agent, content, w, "analysis")
        analysis = parse_analysis(text, question)

        # 确定性兜底：列表/JSON 格式且能确定名称列表时，覆盖为纯列表字符串
        if fmt in ("list", "json") and analysis.names is not None:
            analysis.answer = render_list_answer(analysis.names, fmt)
        elif fmt == "single" and analysis.names:
            analysis.answer = analysis.names[0]

        if analysis.items:
            # 明细以统一键作为列，渲染成表格
            keys: list[str] = []
            for it in analysis.items:
                for k in it:
                    if k not in keys:
                        keys.append(k)
            columns = keys
            rows = [[it.get(k) for k in keys] for it in analysis.items]
        else:
            columns = ["结果"]
            rows = [[analysis.answer]]

        # 分析说明：优先 agent 计算推理；为空则回退到对明细表的简短分析（静默降级）
        reasoning = analysis.explanation
        if not reasoning:
            try:
                reasoning = await summarize_query_result(question, columns, rows, len(rows))
            except Exception:
                reasoning = None

        result = QueryResponse(
            question=question,
            kind="query",
            columns=columns,
            rows=rows,
            row_count=len(rows),
            # 用户显式指定的图表类型在这里同样要生效：早前 analysis 分支整条丢掉
            # chart_hint，导致「用柱状图看各品类环比」在统计链路下永不出图，
            # 与「严格图表模式」的契约不符（query 分支会照常出图）。
            chart_type=suggest_chart_type(columns, rows, question, state.get("chart_hint")),
            explanation=analysis.answer,
            analysis=reasoning,
        )
        w({"type": "result", "data": result.model_dump()})
        return {"result": result}
    except Exception:
        # 统计路径失败 → 回退到 text2sql（不抛，保底）。
        # 必须留日志：这条回退会吞掉**代码 bug**（不只是算子算不出），
        # 静默时表现为「模型算了半天最后给了个 SQL 结果」，无从排查。
        logger.warning("analysis 分支失败，回退 text2sql 链路。", exc_info=True)

    # 回退：既有 text2sql 流式生成 + 执行（与 query 链路一致）
    parts: list[str] = []
    async for token in generate_sql_stream(question, schema_text, dialect):
        parts.append(token)
        if state.get("want_sql"):
            w({"type": "sql_token", "content": token})
    sql = extract_sql("".join(parts))
    result = await asyncio.to_thread(
        execute_sql,
        question,
        sql,
        engine,
        state.get("chart_hint"),
        state.get("want_sql", False),
    )
    if result.error:
        w({"type": "error", "message": result.error, "error_code": result.error_code})
    else:
        w({"type": "result", "data": result.model_dump()})
    return {"result": result, "sql": sql}


async def _execute_node(state: OrchestratorState) -> dict:
    """执行 SQL，输出 result（软错误则发 error 事件）。"""
    w = get_stream_writer()
    # pandas 读结果集是同步阻塞的（大表聚合可能数十秒），必须移出事件循环
    result = await asyncio.to_thread(
        execute_sql,
        state["question"],
        state["sql"],
        state["engine"],
        state.get("chart_hint"),
        state.get("want_sql", False),
    )
    if result.error:
        w({"type": "error", "message": result.error, "error_code": result.error_code})
    else:
        fmt = state.get("answer_format")
        if fmt in ("list", "json"):
            # 列表/JSON 格式：取首列作为满足条件的项名称，渲染为列表（0 行自然得 []）
            names = [
                str(row[0])
                for row in result.rows
                if row and row[0] is not None
            ]
            result.explanation = render_list_answer(names, fmt)
            # 答案之外补一段分析说明（降级：LLM 不可用时跳过，但要留日志）
            try:
                result.analysis = await summarize_query_result(
                    result.question, result.columns, result.rows, result.row_count
                )
            except Exception:
                logger.warning("查询结果分析说明生成失败，已跳过。", exc_info=True)
        elif fmt == "single":
            # 单答案：只给名称（首列首个非空值），不带括号引号，也不追加 LLM 分析说明
            names = [
                str(row[0])
                for row in result.rows
                if row and row[0] is not None
            ]
            result.explanation = names[0] if names else "未查到匹配数据，请调整提问。"
        elif result.row_count > 0:
            try:
                analysis = await summarize_query_result(
                    result.question, result.columns, result.rows, result.row_count
                )
                if analysis:
                    result.explanation = analysis
            except Exception:
                pass  # 分析为增强能力：失败静默降级为原规则文案
        w({"type": "result", "data": result.model_dump()})
    return {"result": result}


async def _report_node(state: OrchestratorState) -> dict:
    """决策报告：create_agent 多步下钻推理，输出结构化 report 事件（可选能力，失败不阻断）。

    ``report_enabled=False`` 时整体跳过。默认开启，但这条链路在 SSE 内是**串行**的：
    报告 agent 持有 ``generate_sql``/``execute_sql`` 会再跑一轮 SQL，单次查询因此
    多付出若干次 LLM 往返（叠加 ``summarize_query_result`` 后更明显）。要压首字延迟
    或成本时把它关掉即可（report 事件虽在 result 之后到达，但仍在同一响应内）。
    """
    if not settings.report_enabled:
        return {}
    result = state.get("result")
    if not result or result.error:
        return {}

    w = get_stream_writer()
    try:
        # build_tools 会同步加载本地知识库语料（必要时全量重嵌入），必须移出事件循环
        tools = await asyncio.to_thread(
            build_tools, state["engine"], state["dialect"], state["schema_text"]
        )
        agent = create_agent(
            model=get_llm(temperature=0.0),
            tools=tools,
            system_prompt=_REPORT_SYSTEM_PROMPT,
        )
        text = await _run_agent_streamed(
            agent,
            _build_report_input(result, sql=state.get("sql")),
            w,
            "report",
        )
        report = parse_report(text, result.question)
    except Exception:
        # 报告为可选能力：任何失败都静默跳过，不影响已产出的主结果
        logger.warning("决策报告生成失败，已跳过。", exc_info=True)
        return {}

    w({"type": "report", "data": report.model_dump()})
    return {"report": report}


async def _redraw_node(state: OrchestratorState) -> dict:
    """重绘图表：复用本会话最近一次查询结果，仅切换图表类型，不再生成/执行 SQL。"""
    w = get_stream_writer()
    prev = await get_last_query_result(state.get("session_id"))

    if prev is None:
        err = QueryResponse(
            question=state["question"],
            kind="query",
            error="当前会话还没有可复用的查询结果，请先进行一次数据查询。",
            error_code="NO_PRIOR_RESULT",
        )
        # 与 query/analysis 分支的失败口径一致：发 `result`（错误信息在 result.error 内），
        # 而不是只发 `error`。前端 onResult 已按 result.error 弹出同样的错误提示，
        # 两者都发反而会重复提示一次。注意：带 error 的 result 依设计**不写入会话记忆**
        # （见 routers/chat.py 的 `not final_result.get("error")`），失败轮次本就不该进历史。
        w({"type": "result", "data": err.model_dump()})
        return {"result": err}

    chart_type = state.get("chart_hint")
    if chart_type not in ("bar", "line", "pie"):
        chart_type = "bar"  # 用户只想「画个图」未指定类型时，默认柱状图
    result = QueryResponse(
        question=state["question"],
        kind="query",
        sql=None,
        columns=prev.get("columns") or [],
        rows=prev.get("rows") or [],
        row_count=prev.get("row_count") or 0,
        chart_type=chart_type,
        explanation=f"已根据上一步查询结果绘制图表（共 {prev.get('row_count') or 0} 条数据）。",
    )
    w({"type": "result", "data": result.model_dump()})
    return {"result": result}


async def _locate_doc_tables(ds: str, question: str, table_names: list[str]) -> list[str]:
    """``engine.locate_doc_tables`` 的**异步薄壳**：只为把同步阻塞检索移出事件循环。

    定位逻辑（四条硬约束：块 id 精确优先、本数据源白名单、L2 上界阈值 + margin
    成组保留、按块内容去重）全部在 ``engine.locate_doc_tables`` 里，改动请去那里
    —— 它同时被 ``build_source_schema_text`` 复用（``/api/query`` 等同步链路）。
    这里保留薄壳是因为 ``_schema_node`` 与 ``scripts/test_doc_locator.py`` 都从
    本模块引用这个符号。
    """
    return await asyncio.to_thread(locate_doc_tables, ds, question, table_names)


def _deterministic_schema_answer(
    question: str,
    sq,
    engine,
    table_names: list[str],
    ds: str,
) -> str:
    """LLM 不可用时的确定性反射兜底（按真实列序读列名回答，保持原有能力）。"""
    target: str | None = None
    if len(table_names) == 1:
        target = table_names[0]
    elif len(table_names) > 1 and ds.startswith("doc:"):
        # 多表文档：尝试按问句中的块 id 定位到具体表格
        doc_id, _ = parse_doc_id(ds)
        block_id = extract_block_id(question, doc_id)
        if block_id:
            target = get_doc_table_by_block(doc_id, block_id)

    if target is None:
        if len(table_names) > 1 and ds.startswith("doc:") and sq is not None:
            # 多表文档且未按块定位：自动逐表回答，不再让用户选择具体表格
            return answer_schema_question_multi(sq, engine, table_names)
        if len(table_names) > 1:
            return f"该数据源包含 {len(table_names)} 张表，请换一种更具体的问法。"
        return "未找到可回答的表结构信息，请确认已选择数据源。"
    if sq is None:
        return "未识别到表结构问题，请换一种方式描述。"
    return answer_schema_question(sq, engine, target)


async def _schema_node(state: OrchestratorState) -> dict:
    """表结构/列名咨询：LLM + 全局 schema 视角为主，确定性反射兜底（LLM 不可用）。

    给模型一份按列序编号、含样本行的表结构总览，让模型自行把中文列名（谓词→predicate、
    主题→subject、日期→date 等）映射到真实英文字段，而非硬编码词典；LLM 失败时回退到
    确定性反射，避免凭空编造列名。
    """
    w = get_stream_writer()
    question = state["question"]
    sq = state.get("schema_question")
    ds = state["data_source_id"]

    try:
        engine = await asyncio.to_thread(get_engine, ds)
        table_names = await asyncio.to_thread(resolve_table_names, ds)
    except Exception:
        # 数据源不存在 / 连接失败：engine 留空走「无法读取表结构」文案。
        # 早前这里也是 `except: pass`，但把「数据源不存在」静默说成「表结构为空」，
        # 排查时看不到任何线索 —— 至少留一条日志。
        logger.warning("schema 分支读取数据源失败：%s", ds, exc_info=True)
        engine = None
        table_names = []

    # 块级定位：多表文档先把目标表收窄（块级数据源已在上一步返回单表）。
    # 与查询/分析分支共用 _locate_doc_tables —— 早前这里是内联实现，只在
    # column_by_name 问句上走 RAG，且**没有按内容去重**（同表副本会各占一个候选），
    # 于是「第 N 列的列名是什么」这类没有语义信号的问句一律落到全量总览。
    if engine is not None and len(table_names) > 1 and ds.startswith("doc:"):
        located = await _locate_doc_tables(ds, question, table_names)
        if located:
            table_names = located
        # 定位不到 → 保留全量总览兜底：位置问句（第 N 列）对向量检索本就没有信号，
        # 会被阈值拒掉。这是题干信息不足导致的本质歧义，需靠块 id 寻址解决。

    answer: str | None = None
    if engine is not None and table_names:
        try:
            # 逐表 SELECT * LIMIT 3 采样，表多时是很实在的阻塞
            overview = await asyncio.to_thread(build_schema_overview, engine, table_names)
            parts: list[str] = []
            async for token in schema_consult_stream(question, overview):
                parts.append(token)
                w({"type": "answer_token", "content": token})
            answer = "".join(parts)
        except Exception:
            # LLM 不可用/失败 → 走确定性兜底。
            # 注意：若模型已流出部分 token 后失败，前端会留下**截断的模型输出**且
            # 永远看不到兜底答案 —— 日志是唯一线索。
            logger.warning("schema 咨询流式回答失败，改用确定性兜底。", exc_info=True)
            answer = None

    if not answer:
        if engine is None:
            answer = "无法读取表结构，请检查数据源是否可用。"
        elif not table_names:
            answer = "未找到可回答的表结构信息，请确认已选择数据源。"
        else:
            answer = await asyncio.to_thread(
                _deterministic_schema_answer, question, sq, engine, table_names, ds
            )

    result = QueryResponse(question=question, kind="chat", answer=answer)
    w({"type": "result", "data": result.model_dump()})
    return {"result": result}


async def _chat_node(state: OrchestratorState) -> dict:
    """闲聊 / 元数据咨询：流式 answer_token + 最终 result。"""
    w = get_stream_writer()

    ds = state["data_source_id"]
    schema_text = None
    try:
        engine = await asyncio.to_thread(get_engine, ds)
        # 仅暴露本数据源的表，避免串表。**这里刻意停在 build_scoped_schema_text**：
        # 闲聊分支不生成 SQL，用不到取值区，而取值区对小表是整表 dump（单表可达数十 KB）、
        # 还会触发 doc 源的 embedding + Milvus 定位 —— 每句闲聊都付这个代价不合理。
        # 查询链路（_prepare_query_node / /api/query / /query/stream）才走
        # build_source_schema_text，那条链路需要真实取值来写对过滤条件。
        schema_text = await asyncio.to_thread(build_scoped_schema_text, engine, ds)
    except Exception:
        # 数据源不可用或未知时降级为纯闲聊，不阻断对话
        logger.info("闲聊分支未取到表结构（数据源不可用或未选），按纯闲聊处理：%s", ds, exc_info=True)
        schema_text = None

    # 记忆治理：近期窗口截断 + 旧对话滚动摘要（失败静默降级为纯截断）
    ctx = await build_chat_context(state.get("session_id"), state.get("history") or [])
    parts: list[str] = []
    async for token in chat_stream(
        state["question"],
        ctx["history"],
        schema_text,
        summary=ctx["summary"],
    ):
        parts.append(token)
        w({"type": "answer_token", "content": token})

    result = QueryResponse(question=state["question"], kind="chat", answer="".join(parts))
    w({"type": "result", "data": result.model_dump()})
    return {"result": result}


def _chunk_label(c: dict) -> str:
    """片段编号：优先用块 id，没有则退回「#顺位」。"""
    bid = str(c.get("block_id") or "").strip()
    return bid if bid and bid != "0" else f"#{int(c.get('block_index') or 0) + 1}"


def _chunk_header(c: dict) -> str:
    """片段抬头。**表格块必须如实交代「这只是样本」**。

    Milvus 里表格块只有「列名 + 前 5 行样本」，而 docs.db 里存的是全量数据。
    不标注的话，模型会拿 5 行样本算计数/合计/平均值，报出一个形态完好的**假统计量**
    —— 比「答不出」更糟，因为它看起来像答案。故这里带上全表行数，
    并由 ``chat_service._RETRIEVAL_SYSTEM_PROMPT`` 配套要求「不得据此下统计结论」。
    """
    head = f"[来源：{c['title']} · 片段 {_chunk_label(c)}]"
    if c.get("kind") != "table":
        return head
    n = c.get("row_count")
    total = f"全表共 {n} 行" if isinstance(n, int) else "全表行数未知"
    return f"{head}【表格摘录：以下仅为该表的部分样本行，{total}，非全部数据】"


async def _retrieve_node(state: OrchestratorState) -> dict:
    """文档内容问答：检索 top-k 片段 → 流式 answer_token + 最终 result（附出处 sources）。"""
    w = get_stream_writer()
    doc_id = parse_doc_id(state["data_source_id"])[0]
    # embedding HTTP + Milvus 检索都是同步阻塞，必须移出事件循环
    chunks = await asyncio.to_thread(retrieve_doc, doc_id, state["question"], settings.doc_top_k)

    context = "\n\n".join(
        f"{_chunk_header(c)}\n{c['content']}" for c in chunks
    ) or "（未检索到相关内容，请如实告知用户。）"

    sources = [SourceRef(title=c["title"], block_id=_chunk_label(c)) for c in chunks]

    parts: list[str] = []
    async for token in retrieval_stream(state["question"], context):
        parts.append(token)
        w({"type": "answer_token", "content": token})

    result = QueryResponse(
        question=state["question"], kind="chat", answer="".join(parts), sources=sources
    )
    w({"type": "result", "data": result.model_dump()})
    return {"result": result}


def build_graph():
    """组装并编译编排图。"""
    g = StateGraph(OrchestratorState)
    g.add_node("classify", _classify_node)
    g.add_node("prepare_query", _prepare_query_node)
    g.add_node("text2sql", _text2sql_node)
    g.add_node("execute", _execute_node)
    g.add_node("report", _report_node)
    g.add_node("redraw", _redraw_node)
    g.add_node("chat", _chat_node)
    g.add_node("retrieve", _retrieve_node)
    g.add_node("schema", _schema_node)
    g.add_node("analysis", _analysis_node)

    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", _route, _CLASSIFY_BRANCHES)
    g.add_conditional_edges(
        "prepare_query",
        _route_after_prepare,
        {"query": "text2sql", "analysis": "analysis"},
    )
    g.add_edge("text2sql", "execute")
    g.add_edge("execute", "report")
    g.add_edge("report", END)
    g.add_edge("redraw", END)
    g.add_edge("chat", END)
    g.add_edge("retrieve", END)
    g.add_edge("schema", END)
    g.add_edge("analysis", END)

    return g.compile()


# 模块级单例：编译一次，所有请求复用
graph = build_graph()
