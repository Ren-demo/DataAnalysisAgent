"""意图分类：判断用户输入属于「数据查询」「闲聊/元数据咨询」还是「重绘图表」，并识别显式图表类型。

优先用 LLM 输出结构化 JSON；解析或校验失败时用关键词规则兜底，最终默认 query
（查询是核心链路，且已有完整的 SQL 校验与错误兜底）。

图表触发采用严格模式：查询链路中仅当用户明确说出图表类型（柱状图/饼图/折线图等）时才出图，
仅说「可视化/画图」而不指定类型，一律返回 table（不出图）。重绘（redraw）分支复用已有结果，
未指定类型时默认柱状图。
"""
import json
import re
from typing import Literal

from pydantic import BaseModel

from app.llm.client import get_llm
from app.schemas import ChatMessage


class SchemaQuestion(BaseModel):
    """表结构/列名咨询问句的结构化识别结果（纯规则，供确定性 schema 回答）。"""

    kind: Literal["nth_column", "column_by_name", "list_columns", "column_count", "row_count"]
    n: int | None = None      # nth_column：第 n 列（1-based）
    name: str | None = None   # column_by_name：被点名的列名


class IntentResult(BaseModel):
    """意图分类结果。"""

    intent: Literal["query", "chat", "redraw", "retrieval", "analysis", "schema"]
    chart_hint: str | None = None  # "bar" | "line" | "pie" | None，仅显式指定图表类型时非空
    want_sql: bool = False  # 用户是否明确要求输出/展示 SQL 语句（默认不展示）
    answer_format: Literal["list", "json", "single"] | None = None  # 用户要求/隐含的输出格式（列表/JSON/单答案）
    schema_question: SchemaQuestion | None = None  # 表结构/列名咨询（走确定性 schema 回答）


_SYSTEM_PROMPT = (
    "你是一个意图分类器，判断用户这句话属于哪一类：\n"
    "1. `query`：需要查询、统计、汇总数据库中具体数据的简单聚合问题（如数量/合计/平均/占比/排名/分组/条件筛选），需要生成 SQL。\n"
    "2. `analysis`：需要做复杂统计建模或数据分析的问题，例如相关性（正相关/负相关）、环比/同比、趋势/时序、关联关系、预测、异常检测等——"
    "这类问题涉及数学统计计算（相关系数、逐期增长率、窗口比较），应走专门的分析链路，而不是生成单条 SQL。\n"
    "3. `chat`：普通对话、闲聊、概念咨询、自我介绍、询问系统能力，"
    "或询问当前数据源里有哪些表 / 字段 / 数据结构（元数据咨询），不需要查数据。\n"
    "4. `redraw`：用户要求把已有（本会话上一步）的查询结果绘制或重绘成图表，"
    "例如「画成柱状图」「换成饼图」「用折线图展示」「重绘图表」，此时不需要再查询数据库。\n"
    "\n"
    "同时判断 `chart_hint`：\n"
    "- 仅当用户明确说出图表类型（如「柱状图」「条形图」「饼图」「折线图」「趋势图」）时，"
    "对应填 `bar` / `line` / `pie` 之一；\n"
    "- 仅说「图表」「可视化」「画图」但未指定具体类型时，填 null；\n"
    "- 其它情况一律填 null。\n"
    "\n"
    "同时判断 `want_sql`：\n"
    "- 仅当用户明确要求输出/展示 SQL 语句（如「输出 SQL」「显示 SQL」「给我 SQL 语句」"
    "「写出查询语句」）时填 true；\n"
    "- 仅询问数据结果、要求图表、或普通闲聊，一律填 false。\n"
    "\n"
    "只输出一个 JSON 对象，不要任何多余文字，格式严格如下：\n"
    '{"intent": "query", "chart_hint": null, "want_sql": false}'
)

# 关键词兜底用
_CHAT_WORDS = (
    "你好", "您好", "嗨", "哈喽", "hello", "hi", "在吗", "谢谢", "感谢",
    "你是谁", "你能做什么", "介绍一下", "再见", "拜拜",
    # 元数据咨询（避免「列」等单字过宽匹配）
    "有什么数据", "有哪些数据", "哪些数据", "有哪些表", "哪些表", "什么表",
    "表结构", "有哪些字段", "什么字段", "数据源", "包含什么", "有什么内容",
)
_QUERY_WORDS = (
    "统计", "查询", "销量", "销售", "多少", "排名", "前几", "平均", "总和",
    "总额", "占比", "比例", "趋势", "对比", "分析", "每个", "各", "部门",
    "产品", "员工", "季度", "月份", "客单价",
)
# 复杂统计建模关键词：命中即判定为 analysis（走 pandas 计算兜底，而非单条 SQL）
#
# 年化口径单独列出（「年均增长率 / 复合增长率 / CAGR / 平均年增长率」）：
# 它们对应 `group_cagr` 算子，而**不能**用环比算子凑 —— 拿环比算 5 年的年均增长
# 会把总增速当成一年的增速。只收完整短语、不收裸「增长率」，避免把「环比增长率」
# 之外的一般性提及也拉进来。
_ANALYSIS_WORDS = (
    "相关性", "正相关", "负相关", "相关系数", "环比", "同比", "持续增长",
    "持续下降", "连续增长", "连续下降", "关联分析", "关联关系", "趋势预测",
    "预测", "异常", "回归", "聚类", "协方差", "标准差", "时序",
    "外推", "离群", "异常值", "离群点", "分群", "趋势分析", "下期",
    "年均增长率", "复合增长率", "年平均增长率", "平均年增长率", "CAGR",
)
_CHART_HINT_MAP = {"柱状图": "bar", "条形图": "bar", "饼图": "pie", "折线图": "line"}
# 仅表达「绘制/重绘图表」但未指定具体类型的词，配合 _CHART_HINT_MAP 判定 redraw
_REDRAW_WORDS = (
    "图表", "可视化", "画图", "绘制", "重绘", "画成", "画个", "画一下",
    "换成", "改成", "展示为", "展示成",
)

# 文档数据源：问句指向**文档正文** → retrieval（走检索而非 SQL）。
#
# **不要收「内容」**：它太泛，任何提及「内容」二字的问句都会命中 ——
# 赛题 Table_Query 的「表格中内容为 X 的单元格上面单元格的内容是什么？」
# 里的「内容」指的是**单元格取值**，与文档正文无关。实测 500 道题里 287 道
# （57.4%）都含「内容」，收进来会把过半的按单元格查找题赶去 RAG，
# 而 Milvus 里表格块只索引「列名 + 前 5 行样本」，这些题必然答不出。
# 「文档/报告/资料」已足以承载正文指向，「这份文档的内容是什么」照样命中。
#
# 信号分三类，判定顺序是「表格信号一票否决 → 文档名词 → 正文短语」：
_DOC_NOUN_WORDS = (
    "文档", "这份资料", "该资料", "资料里", "资料中", "原文", "正文", "附件",
    # 只收「报告里/报告中/这份报告」这类**明确指代**，不收裸「报告」——
    # 财报语境里它是动词（「被报告为当前负债」「报告的净亏损」），实测赛题
    # Domain-specific 239 题里正是这 2 题被裸「报告」误判进 RAG。
    "报告里", "报告中", "这份报告", "该报告",
)
# 正文指向短语：是「对正文做什么」的说法，不能是裸动词 ——
# 早前词表收了「查找/找一下/描述/介绍」，于是「查找 ANON-0000-0000-0001 这张卡
# 2024-03-01 的消费金额」因为一个「查找」被赶去 RAG，而答案在文档抽出的表里。
_DOC_PHRASE_WORDS = (
    "说了什么", "讲了什么", "讲的什么", "提到了", "提到哪些", "里面写",
    "写的什么", "主要内容", "概括一下", "总结一下", "有什么信息",
)
# 表格**内部**定位/取值信号：命中即否决 retrieval，无论同时命中了什么文档名词。
# 「这份文档的表格中第三列的列名」问的是表结构，不是正文。
_TABLE_SCAN_WORDS = (
    "单元格", "表格中", "表格里", "表中", "表里", "这张表", "该表", "这个表",
    "第几行", "行第", "列名", "哪一列", "哪一栏", "这张卡", "该卡",
)

# 用户明确要求输出/展示 SQL 的表达（供关键词兜底与 /query、/query/stream 复用）
_WANT_SQL_WORDS = (
    "sql语句", "查询语句", "输出sql", "显示sql", "展示sql", "给我sql",
    "写出sql", "生成sql", "打印sql", "查看sql", "sql给我", "sql是什么",
    "把sql", "贴出sql", "sql脚本",
)

# 用户明确要求以列表形式返回答案的表达（供 detect_answer_format 关键词识别）
# 仅保留「列表/名称」等明确信号，避免「列出/只返回」等宽泛词误触发（如「列出各分类销量」应出表格而非名称列表）
_FORMAT_LIST_WORDS = (
    "列表", "list", "返回名称", "名称列表", "以列表",
)
# 用户明确要求以 JSON 形式返回答案的表达
_FORMAT_JSON_WORDS = ("json", "以json", "json格式", "json输出")
# 复数「哪些」类提问隐含「列出满足条件的项」；单数「哪个」不自动触发（避免误判单值查询）
_WHICH_PLURAL_WORDS = ("哪些", "哪类", "哪几种")
# 明确「多个」数量信号（前三/top/几个…）：优先于极值词，避免「销量最高的前三个产品」被误判为单答案。
# 中文数字量词要成组收录（三种/两家/三类…），只收「三个/几个/几种」会漏掉大半写法；
# 用正则而非枚举，覆盖「两/二/三/四/五/六/七/八/九/十/几/数/若干 + 个/种/类/家/款/名/条/项/位」。
_PLURAL_COUNT_RE = re.compile(
    r"(?:前\s*(?:\d+|[一二三四五六七八九十几数两]+)"
    r"|(?:[两二三四五六七八九十几数]|若干)\s*(?:个|种|类|家|款|名|条|项|位|支|台)"
    r"|\btop\s*\d*|排名前|多少种|多少类)"
)
# 单答案信号：极值/唯一 + 单数疑问词「哪个/哪家/哪位…」，命中即只给一个名称。
# 注意「第一」必须收紧：它是「第一季度/第一批/第一阶段」的前缀，
# 早前裸收「第一」会把「第一季度各地区销售额」这类多行查询判成单答案。
_SINGLE_ANSWER_WORDS = (
    "最高", "最低", "最多", "最少", "最大", "最小", "唯一",
    "最佳", "最优", "最快", "最慢", "最贵", "最便宜", "最好", "最差",
    "冠军", "哪个", "哪家", "哪位", "哪只", "哪台", "哪笔", "谁是",
)
# 「第一」的安全形态：后接量词/名次含义时才算单答案（第一名/第一个/第一次），
# 后接季度/月/周/阶段/批/年/期 等时间量词时不算（那是分组维度而非极值）。
_SINGLE_FIRST_RE = re.compile(r"第\s*一\s*(?:名|个|次|位|款|种|台|笔|只)")
_FIRST_TIME_DIM_RE = re.compile(r"第\s*一\s*(?:季度|月|周|年|期|阶段|批|轮|届|天|日|号)")


def detect_want_sql(question: str) -> bool:
    """关键词规则：判断用户是否明确要求输出/展示 SQL 语句。"""
    q = (question or "").lower()
    if any(w in q for w in _WANT_SQL_WORDS):
        return True
    # 去掉空白后再次匹配，覆盖「输出 SQL」「给我 SQL 语句」等带空格写法
    compact = "".join(q.split())
    return any(w in compact for w in _WANT_SQL_WORDS)


def detect_answer_format(question: str) -> str | None:
    """关键词规则：识别用户要求（或隐含）的输出格式。

    返回 ``"json"`` / ``"list"`` / ``"single"`` / ``None``，优先级从高到低：
    显式 JSON > 显式列表 > 明确「多个」数量信号（前N/top/几个，列表）>
    单答案极值/单数信号（``"single"``，只给一个名称）> 「哪些/哪类/哪几种」隐含列表。
    纯规则、不依赖 LLM，供编排图在 analysis/query 链路按需渲染答案。
    """
    q = (question or "").lower()
    compact = "".join(q.split())
    if any(w in compact for w in _FORMAT_JSON_WORDS):
        return "json"
    if any(w in compact for w in _FORMAT_LIST_WORDS):
        return "list"
    # 明确「多个」数量信号优先于极值词：如「销量最高的前三个产品」是列表而非单答案
    if _PLURAL_COUNT_RE.search(compact):
        return "list"
    if any(w in compact for w in _SINGLE_ANSWER_WORDS):
        return "single"
    # 「第一」：仅在非时间维度语境下算单答案（「第一名」是，「第一季度」不是）
    if _SINGLE_FIRST_RE.search(compact) and not _FIRST_TIME_DIM_RE.search(compact):
        return "single"
    if any(w in q for w in _WHICH_PLURAL_WORDS):
        return "list"
    return None


def render_list_answer(names: list[str], fmt: str) -> str:
    """把名称列表渲染为答案字符串：``json`` → JSON 数组（双引号），否则 Python 列表（单引号）。"""
    if not names:
        return "[]"
    if fmt == "json":
        return json.dumps(names, ensure_ascii=False)
    return repr(names)


def _extract_json(text: str) -> str:
    """从模型输出中提取 JSON 片段（剥离 markdown 围栏、定位首个 { 到末个 }）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _normalize_chart_hint(hint) -> str | None:
    """把可能的图表类型归一化为 bar/line/pie；无效则返回 None。"""
    if not hint:
        return None
    h = str(hint).strip().lower()
    if h in ("bar", "柱状图", "条形图"):
        return "bar"
    if h in ("line", "折线图", "趋势图"):
        return "line"
    if h in ("pie", "饼图", "占比图"):
        return "pie"
    return None


def _rule_fallback(question: str) -> IntentResult:
    """关键词规则兜底：明显闲聊/元数据咨询 → chat；纯图表/重绘表达 → redraw；
    复杂统计建模（相关性/环比/同比等）→ analysis；其余 → query。"""
    q = question or ""
    want_sql = detect_want_sql(q)
    # 元数据咨询兜底需让位于统计建模词：如「哪些数据是异常值」应走 analysis 而非 chat
    if (
        any(w in q for w in _CHAT_WORDS)
        and not any(w in q for w in _QUERY_WORDS)
        and not any(w in q for w in _ANALYSIS_WORDS)
    ):
        return IntentResult(intent="chat")

    hint = None
    for word, typ in _CHART_HINT_MAP.items():
        if word in q:
            hint = typ
            break

    # 重绘：仅出现图表/绘图类表达、且无数据查询意图 → 复用已有结果出图
    if (hint is not None or any(w in q for w in _REDRAW_WORDS)) and not any(
        w in q for w in _QUERY_WORDS
    ):
        return IntentResult(intent="redraw", chart_hint=hint)

    # 复杂统计建模：命中相关性/环比/同比等词 → 走 pandas 计算兜底（而非单条 SQL）
    if any(w in q for w in _ANALYSIS_WORDS):
        return IntentResult(intent="analysis", want_sql=want_sql)

    return IntentResult(intent="query", chart_hint=hint, want_sql=want_sql)


def _is_doc_body_question(question: str) -> bool:
    """问句是否指向文档**正文**（而非文档里抽出来的表格）。判定顺序即优先级。

    先看表格内部定位/取值信号 —— 命中就否决，哪怕同时说了「这份文档的表格中…」；
    再看文档名词（「文档」「报告里」），最后才看正文指向短语。
    """
    q = question or ""
    if any(w in q for w in _TABLE_SCAN_WORDS):
        return False
    if any(w in q for w in _DOC_NOUN_WORDS):
        return True
    return any(w in q for w in _DOC_PHRASE_WORDS)


def _route_doc_intent(question: str, result: IntentResult) -> IntentResult:
    """文档数据源：问句指向文档正文 → retrieval（RAG 检索回答），**就地改判**。

    早前这里是「命中检索词 **且** 不命中聚合词才转 retrieval」，聚合词表（统计/多少/排名…）
    会把「报告里提到的退款金额是多少」否决成 query —— 于是问的是文档正文，答的却是从文档里
    抽出的表格，语义错配。聚合词只描述**答案的形态**（是个数），并不指示**答案在哪里**，
    不能用来否决检索意图。

    不含正文指向的问句（如「ANON-…-0001 这张卡 2024-03-01 的消费金额是多少」）仍走 query
    查文档表格：Milvus 里表格块只索引「列名 + 前 5 行样本」，纯 RAG 找不到具体行，
    按行查找必须由 SQL 覆盖全量数据。

    **只改 ``intent``，其余字段原样保留**：早前返回的是新建的
    ``IntentResult(intent="retrieval")``，等于把 LLM 那一轮判出来的 ``chart_hint`` /
    ``want_sql`` / ``answer_format`` 全丢了 —— 一次多余的分类往返白做。
    """
    q = question or ""
    # 只作用于 query/chat：schema 问句在 detect_schema_question 那步已锁定；
    # analysis 的 pandas 链路只吃文档表格，改判成 RAG 反而丢了全量计算能力；
    # redraw/retrieval 与本次判定无关。
    if result.intent not in ("query", "chat"):
        return result
    if not _is_doc_body_question(q):
        return result
    result.intent = "retrieval"
    result.schema_question = None
    return result


# 中文数字映射（列位置「第一列 / 第二十列」等，仅需到百以内）
_CN_NUM_MAP = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 「第 N 列」后必须跟问句后缀才算「第 N 列叫什么」；避免命中前置语境里的
# 「第一行和第一列标记/开始/指的是…」等陈述句（如赛题「第一行和第一列标记数据编码的起点」）。
_NTH_COLUMN_RE = re.compile(
    r"第\s*([一二两三四五六七八九十百\d]+)\s*列"
    r"\s*(?:的(?:列名|名称|名字)|叫什么|是什么|名为|叫做|是哪一个)"
)
_COLUMN_BY_NAME_RE = re.compile(
    r"哪一(?:列|栏)\s*(?:被称为|的名称为|的名称是|的名字是|叫做|称为|名为|叫|是)"
)
# 反向句式：「名为 X 的列（是第几列/是哪一列）」→ 按列名定位
_COLUMN_BY_NAME_REV_RE = re.compile(r"名为\s*[“\"']?([^”\"'\s，。？?、,；;]+?)[”\"']?\s*的列")


def _cn_to_int(s: str) -> int | None:
    """把中文数字（一到九十九）转 int；阿拉伯数字直接转；其它返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    n = 0
    for ch in s:
        if ch == "十":
            n = n * 10 if n else 10
        elif ch in _CN_NUM_MAP:
            n += _CN_NUM_MAP[ch]
        else:
            return None
    return n or None


def _extract_col_name(question: str, verb: re.Match) -> str | None:
    """从「哪一列被称为 X」中提取被点名的列名（优先引号内，否则取动词后首个词）。"""
    after = question[verb.end():].strip()
    m = re.search(r"[“\"']([^”\"']+)[”\"']", after)
    if m:
        return m.group(1).strip()
    m = re.match(r"([^\s，。？?、,；;]+)", after)
    if m:
        return m.group(1).strip()
    return None


# 聚合/分组语境词：出现即说明用户要的是**数据**（走 SQL 查询），而不是表结构说明。
# 用于拦住「统计各地区有多少条订单」这类被行数问句误吞的聚合查询。
_AGG_CONTEXT_WORDS = (
    "统计", "合计", "汇总", "求和", "总和", "平均", "均值", "总计", "总额",
    "分组", "按", "各", "每", "占比", "比例", "排名", "排序", "最大", "最小",
    "最多", "最少", "top", "前几", "增长", "环比", "同比", "趋势",
)


def _has_aggregate_context(q: str) -> bool:
    """问句是否含聚合/分组语境（有则不应被判成「表结构咨询」）。"""
    return any(w in q for w in _AGG_CONTEXT_WORDS)


def detect_schema_question(question: str) -> SchemaQuestion | None:
    """纯规则识别「表结构/列名/字段」咨询问句，返回结构化 SchemaQuestion 或 None。

    覆盖赛题常见问法：第 N 列、哪一列叫 X、列名/字段/表结构、列数、行数。
    刻意不做单字「列」匹配，避免把「统计各列的总和」这类数据查询误判为 schema。
    """
    q = (question or "").strip()
    if not q:
        return None

    # 1) 哪一列被称为/的名称为/的名称是/名为/叫/是 X（问句本身即疑问句，优先于「第 N 列」，
    #    避免前置语境里的「第一列」抢答）
    m = _COLUMN_BY_NAME_RE.search(q)
    if m:
        name = _extract_col_name(q, m)
        if name:
            return SchemaQuestion(kind="column_by_name", name=name)

    # 2) 反向句式：名为 X 的列（是第几列/是哪一列）→ 按列名定位
    m = _COLUMN_BY_NAME_REV_RE.search(q)
    if m:
        name = m.group(1).strip()
        if name:
            return SchemaQuestion(kind="column_by_name", name=name)

    # 3) 第 N 列（位置取列名；后缀已收紧，见 _NTH_COLUMN_RE）
    m = _NTH_COLUMN_RE.search(q)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None:
            return SchemaQuestion(kind="nth_column", n=n)

    # 4) 列数量
    if any(w in q for w in ("有几列", "多少列", "几个字段", "多少个字段", "多少字段")):
        return SchemaQuestion(kind="column_count")

    # 5) 列名 / 字段 / 表结构（列出列）
    if any(w in q for w in ("列名", "字段名", "有哪些列", "有哪些字段", "什么字段", "表结构", "有哪些栏")):
        return SchemaQuestion(kind="list_columns")

    # 6) 行数
    #
    # 只认「整表有多少行」的纯问句，且**不得含聚合/分组语境**。
    # 早前把「多少条」「共多少条」也收进来，而第 3 步的 schema 判定会无条件覆盖
    # 任何非 redraw 意图 —— 于是「统计各地区有多少条订单」命中「多少条」被锁成 schema，
    # 用户拿到「该表共有 N 行」，**聚合查询从未生成**（数据查询是赛题必选项）。
    if any(w in q for w in ("行数", "多少行", "有几行")):
        if not _has_aggregate_context(q):
            return SchemaQuestion(kind="row_count")

    return None


def classify_intent(
    question: str,
    history: list[ChatMessage] | None = None,
    data_source_type: str | None = None,
) -> IntentResult:
    """判断用户意图：intent + 显式指定的图表类型。

    ``data_source_type == "doc"`` 时，额外在 query/chat 之上区分 retrieval
    （问句指向文档内容 → 走 RAG 检索回答而非 SQL，见 ``_route_doc_intent``）。
    """
    # 1) LLM 结构化分类
    try:
        llm = get_llm(temperature=0.0)
        history_text = ""
        if history:
            lines = [
                f"{'用户' if m.role == 'user' else '助手'}：{m.content}"
                for m in history[-6:]
            ]
            history_text = "===== 对话历史 =====\n" + "\n".join(lines) + "\n\n"
        prompt = f"{_SYSTEM_PROMPT}\n\n{history_text}===== 当前问题 =====\n{question}"
        response = llm.invoke(prompt)
        data = json.loads(_extract_json(str(response.content)))
        intent = data.get("intent")
        if intent not in ("query", "chat", "redraw", "analysis"):
            intent = "query"
        result = IntentResult(
            intent=intent,
            chart_hint=_normalize_chart_hint(data.get("chart_hint")),
            want_sql=bool(data.get("want_sql")),
        )
    except Exception:
        # 2) 规则兜底
        result = _rule_fallback(question)

    # 3) schema 问句优先：表结构/列名咨询需确定性回答，优先于 chat/query 与 doc-retrieval
    sq = detect_schema_question(question)
    if sq is not None and result.intent != "redraw":
        result.intent = "schema"
        result.schema_question = sq

    # 4) 文档数据源：指向文档正文的问句在 query/chat 之上改判为 retrieval
    #    就地改判（函数内部自查意图），chart_hint / want_sql / answer_format 原样带走。
    if data_source_type == "doc":
        _route_doc_intent(question, result)
    return result
