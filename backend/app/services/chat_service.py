"""闲聊服务：用自然语言与用户对话（非数据查询），支持流式输出与多轮上下文。

当传入 schema_text 时，会把当前数据源的表结构附在 system prompt 里，使模型能够
用自然语言回答「数据源里有什么数据 / 有哪些表 / 有哪些字段」等元数据咨询。
"""
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.llm.client import get_llm
from app.schemas import ChatMessage

_SYSTEM_PROMPT = (
    "你是一名智能数据分析助手，既能帮助用户查询和分析数据，也能用自然语言与用户交流。\n"
    "请用简洁、友好的中文回答，可用 Markdown 排版（列表、表格、代码块）。\n"
    "当用户想查询数据时，请引导其用自然语言描述需求，例如「统计各分类的数量」。"
)

_SCHEMA_HINT = (
    "\n\n当前数据源的表结构如下（当用户询问数据源里有什么数据 / 有哪些表 / 有哪些字段时，"
    "请据此用自然语言回答）：\n{schema}"
)

_SUMMARY_HINT = (
    "以下是此前对话的历史摘要（供你延续上下文，忽略其中已过时的细节）：\n{summary}"
)

_RETRIEVAL_SYSTEM_PROMPT = (
    "你是一名文档问答助手。请仅依据下面提供的文档片段回答用户问题，"
    "不要编造片段之外的事实；若片段不足以回答，请如实说明。"
    "每个片段都以「[来源：文档名 · 片段 编号]」标注，回答时请在相应结论处"
    "自然地引用出处（如「根据《文档名》片段 X」），不要臆造未在片段中出现的来源。"
    "用简洁、友好的中文回答，可用 Markdown 排版。"
    # 表格摘录（Milvus 里表格块只有「列名 + 前 5 行样本」）最容易被当成全量数据用。
    # 不做这个交代，模型会对 5 行样本做计数/合计，一本正经地报出一个假统计量 ——
    # 比「答不出」更糟，因为它看起来像答案。
    "片段中若出现以「表格摘录」标注的内容，那是该表的**部分样本**（并非全部数据），"
    "你可以据此说明该表有哪些列、举出其中几行的取值，但**不得**基于它给出计数、合计、"
    "平均值、最大最小值、占比等需要全量数据的统计结论；遇到这类问题请如实说明"
    "「检索到的只是表格样本，无法据此统计」，并提示用户改为针对该数据源直接提问，"
    "由系统对全表执行查询。"
    "在回答末尾用一小段「简要分析」概括关键结论或主旨，避免仅罗列原文。"
)

_RETRIEVAL_HINT = "===== 检索到的文档片段 =====\n{context}"

_SCHEMA_CONSULT_SYSTEM_PROMPT = (
    "你是一名数据源结构咨询助手。下面提供的是当前数据源的表结构总览"
    "（列按真实顺序编号，并附样本行）。\n"
    "请仅依据这份总览回答用户关于表结构、列名、字段、列序（第几列）、行数的问题：\n"
    "- 用户用中文指代某列时（如「谓词」「主题」「对象」「日期」「年份」），请把它对应到"
    "总览里真实存在的列名（如 predicate / subject / object / date / year），并给出其列序；\n"
    "- 用户问「第 N 列」「名为 X 的列」时，请按总览里的编号与真实列名回答；\n"
    "- 绝不编造总览里不存在的列名；若无法确定，请如实说明。\n"
    "用简洁、友好的中文回答，可用 Markdown 排版。"
)

_SCHEMA_CONSULT_HINT = "===== 表结构总览 =====\n{overview}"


async def schema_consult_stream(question: str, overview: str):
    """schema 咨询流式回答：基于全局 schema 总览回答表结构/列名/列序问题（低温，反编造）。

    与确定性反射不同，这里把理解题意（如中文「谓词」→ 英文 predicate）交给模型，
    但用真实表结构总览兜底，杜绝凭空编造列名。
    """
    llm = get_llm(temperature=0.0)
    messages = [
        SystemMessage(content=_SCHEMA_CONSULT_SYSTEM_PROMPT),
        SystemMessage(content=_SCHEMA_CONSULT_HINT.format(overview=overview)),
        HumanMessage(content=question),
    ]
    async for chunk in llm.astream(messages):
        token = chunk.content
        if token:
            yield str(token)


async def chat_stream(
    question: str,
    history: list[ChatMessage] | None = None,
    schema_text: str | None = None,
    summary: str | None = None,
):
    """流式闲聊：逐 token 输出文本。NO_API_KEY 等 AppError 由调用方处理。

    ``history`` 应为已裁剪的近期窗口；``summary`` 为窗口之前旧对话的滚动摘要，
    二者由 ``context_service.build_chat_context`` 组装后传入。
    """
    llm = get_llm(temperature=0.7)

    system = _SYSTEM_PROMPT
    if schema_text:
        system += _SCHEMA_HINT.format(schema=schema_text)

    messages = [SystemMessage(content=system)]
    if summary:
        messages.append(SystemMessage(content=_SUMMARY_HINT.format(summary=summary)))
    for m in history or []:
        if m.role == "user":
            messages.append(HumanMessage(content=m.content))
        else:
            messages.append(AIMessage(content=m.content))
    messages.append(HumanMessage(content=question))

    async for chunk in llm.astream(messages):
        token = chunk.content
        if token:
            yield str(token)


async def retrieval_stream(question: str, context: str):
    """文档问答：基于检索片段逐 token 流式回答（供非结构化数据源使用）。"""
    llm = get_llm(temperature=0.3)
    messages = [
        SystemMessage(content=_RETRIEVAL_SYSTEM_PROMPT),
        SystemMessage(content=_RETRIEVAL_HINT.format(context=context)),
        HumanMessage(content=question),
    ]
    async for chunk in llm.astream(messages):
        token = chunk.content
        if token:
            yield str(token)


async def summarize_query_result(
    question: str,
    columns: list[str],
    rows: list[list],
    row_count: int,
) -> str:
    """对数据查询结果生成 1~3 句简短中文分析（供数据查询结果的 explanation 附加）。

    一次性生成（非流式）。失败（如 NO_API_KEY）会抛出异常，由调用方静默降级为规则文案。
    """
    sample = rows[:20]
    lines = [" | ".join(str(c) for c in columns)]
    for row in sample:
        cells = ["" if v is None else str(v) for v in row]
        cells = [(c[:40] + "…") if len(c) > 40 else c for c in cells]
        lines.append(" | ".join(cells))
    table_text = "\n".join(lines)

    system = (
        "你是一名数据分析助手。系统已根据用户问题执行查询并得到结构化结果，"
        "请用 1~3 句简洁的中文对结果给出简短分析，聚焦关键结论、数量对比、趋势或异常。"
        "不要重复罗列全部数据，不要输出 SQL，不要编造数据之外的事实。"
    )
    prompt = (
        f"用户问题：{question}\n"
        f"结果共 {row_count} 行，列：{', '.join(str(c) for c in columns)}\n"
        f"数据样本（最多 20 行）：\n{table_text}"
    )
    llm = get_llm(temperature=0.3)
    resp = await llm.ainvoke(
        [SystemMessage(content=system), HumanMessage(content=prompt)]
    )
    content = resp.content
    if not content:
        return ""
    return str(content).strip()
