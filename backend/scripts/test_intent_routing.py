"""意图分流与输出格式的纯规则自检：不依赖 LLM。

覆盖 ``app/llm/intent.py`` 的 ``detect_schema_question`` / ``detect_answer_format``，
重点回归三类**主链路误伤**：

1. 含「多少条」的聚合查询被表结构咨询劫持 —— ``detect_schema_question`` 会**无条件**
   把非 redraw 意图锁成 ``schema``，于是「统计各地区有多少条订单」永远不生成 SQL，
   用户只拿到「该表共有 N 行」。数据查询是赛题必选项，此路径被大面积误伤。
2. 「第一」是「第一季度 / 第一批」的前缀，裸收会把「第一季度各地区销售额」这类
   多行查询判成单答案，只渲染一个名称。
3. 数量词只收「三个/几个/几种」会漏掉「三种/两家/三类」，「销量最高的三种商品」被误判为单答案。
4. 文档数据源上「聚合词否决检索意图」—— 「报告里提到的退款金额是多少」因为一个「多少」
   被回落到 SQL，去查从文档抽出的表格，问正文却答表格。
5. 文档分流的**反向**误伤 —— 裸动词（查找 / 描述 / 介绍）与裸「报告」把表格问句赶去 RAG，
   而 Milvus 里表格块只索引「列名 + 前 5 行样本」，按行/按单元格查找必然答不出。

第 8~10 段为文档分流；第 12 段是赛题全量回归（题目文件不存在时跳过）。

运行：``cd backend && python scripts/test_intent_routing.py``
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.agents.graph import _CLASSIFY_BRANCHES, _route, build_graph
from app.llm.intent import IntentResult, _route_doc_intent, detect_answer_format, detect_schema_question


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def main() -> None:
    # 1) 聚合查询不再被 schema 劫持（回归 F1）
    for q in (
        "统计各地区有多少条订单",
        "统计各分类有多少条记录",
        "按城市分组，每个城市有多少条数据",
        "每个月有多少条交易",
        "各门店共多少条销售记录",
    ):
        _check(f"聚合查询不判为 schema：{q}", detect_schema_question(q) is None, str(detect_schema_question(q)))

    # 2) 纯「整表有多少行」问句仍走确定性行数回答（不能被上面的收紧误杀）
    for q in ("这个表有多少行？", "表中有多少行数据", "该表的行数是多少", "这张表有几行"):
        sq = detect_schema_question(q)
        _check(f"整表行数仍判为 row_count：{q}", sq is not None and sq.kind == "row_count", str(sq))

    # 3) 其余 schema 问句不受影响
    _check("列名仍判为 list_columns", detect_schema_question("这个表有哪些列名？").kind == "list_columns")
    _check("有几列仍判为 column_count", detect_schema_question("这个表有几列？").kind == "column_count")
    _check("第2列仍判为 nth_column", detect_schema_question("表中第2列的列名是什么？").kind == "nth_column")

    # 4) 普通数据查询不应命中 schema（对照组）
    for q in ("各分类的数量是多少", "统计各列的总和", "销量第一的产品是哪个"):
        _check(f"普通查询不判为 schema：{q}", detect_schema_question(q) is None)

    # 5) answer_format：「第一」后接时间量词时不是单答案（回归 F2）
    _check("第一季度各地区销售额 → 非 single", detect_answer_format("第一季度各地区销售额") != "single")
    _check("第一批订单的金额合计 → 非 single", detect_answer_format("第一批订单的金额合计") != "single")
    _check("每个月的销售额趋势 → 非 single", detect_answer_format("每个月的销售额趋势") != "single")
    # 「第一名」仍应是单答案
    _check("销量第一名 → single", detect_answer_format("销量第一名是哪个产品") == "single")
    _check("第一个下单的用户 → single", detect_answer_format("第一个下单的用户是谁") == "single")
    _check("最高 → single", detect_answer_format("销量最高的产品") == "single")

    # 6) 中文数量词成组覆盖（回归 F3）
    for q in (
        "销量最高的三种商品",
        "销量最高的两家门店",
        "占比最高的三类产品",
        "销售额最高的两款咖啡",
        "销量最高的前三个产品",
        "top 5 的产品",
    ):
        _check(f"多数量词 → list：{q}", detect_answer_format(q) == "list", str(detect_answer_format(q)))

    # 7) 显式格式优先
    _check("显式 JSON 优先", detect_answer_format("用 JSON 返回销量最高的产品") == "json")
    _check("显式列表", detect_answer_format("以列表返回各分类名称") == "list")
    _check("「哪些」隐含列表", detect_answer_format("哪些城市有订单") == "list")
    _check("无信号 → None", detect_answer_format("各分类的数量是多少") is None)

    # 8) 文档数据源：指向文档内容的问句一律走 RAG（回归 F4）
    #    早前要求「命中检索词 **且** 不命中聚合词」，于是下面这三条因为「多少/统计」
    #    被否决成 query，去 docs.db 查从文档抽出的表格 —— 问正文、答表格，语义错配。
    for q in (
        "报告里提到的退款金额是多少？",
        "文档中统计的客户数量是多少",
        "这份资料里提到了多少次退款",
        "文档里介绍了哪些产品",
    ):
        r = _route_doc_intent(q, IntentResult(intent="query"))
        _check(f"文档内容提问 → retrieval：{q}", r.intent == "retrieval", r.intent)

    # 9) 对照组：不含检索词的表格问句仍走 SQL —— 护住赛题 Table_Query 的「按块查某行某值」，
    #    Milvus 里表格块只索引「列名 + 前 5 行样本」，纯 RAG 找不到具体行。
    #    「表格中内容为 X 的单元格…」这类问句里的「内容」指**单元格取值**，不是文档正文：
    #    检索词表一旦收「内容」，500 道赛题里 287 道（57.4%）会被赶去 RAG。
    for q in (
        "ANON-0000-0000-0001 这张卡 2024-03-01 的消费金额是多少？",
        "统计各分类的销量",
        "Boron 项目 2030 年的最高出货量是多少",
        "表格中内容为“French”的单元格上面单元格的内容是什么？",
        "第2行第2列的单元格内容是什么？",
        "在表格中，内容为“standingsGames”的单元格上方的单元格里写的是什么？",
    ):
        r = _route_doc_intent(q, IntentResult(intent="query"))
        _check(f"非文档正文提问保持 query：{q[:24]}…", r.intent == "query", r.intent)

    # 10) 文档源上的闲聊不被劫持为 retrieval（留作 chat，走对话链路）
    _check("文档源闲聊保持 chat", _route_doc_intent("你好", IntentResult(intent="chat")).intent == "chat")

    # 10a) 词表收紧：裸动词不再把表格问句赶去 RAG（回归 F5）
    #      「查找/描述/介绍」早在词表里，于是「查找 ANON-…-0001 这张卡 2024-03-01 的消费金额」
    #      因为一个「查找」被送去 RAG —— 答案其实在文档抽出的表里，RAG 只有前 5 行样本。
    for q in (
        "描述一下各分类的销量",
        "介绍一下这两个字段的含义",
        "查找 2024-03-01 的消费金额",
        "帮我找一下最高出货量",
    ):
        _check(f"裸动词不触发 retrieval：{q}", _route_doc_intent(q, IntentResult(intent="query")).intent == "query")

    # 10b) 裸「报告」不收：财报语境里它是动词。赛题 Domain-specific 239 题里
    #      正是这两道被裸「报告」误判进 RAG（「被报告为当前负债」「报告的净亏损」）。
    for q in (
        "截至2007年12月31日，长期债务中有多少部分被报告为当前负债？",
        "公司在2008年报告的净亏损是多少？",
    ):
        _check(f"动词「报告」不触发 retrieval：{q[:20]}…", _route_doc_intent(q, IntentResult(intent="query")).intent == "query")

    # 10c) 表格内部定位信号**一票否决** —— 哪怕句子里有「这份文档」。
    #      「这份文档的表格中第三列的列名」问的是表结构，不是正文。
    for q in (
        "这份文档的表格中第三列的列名是什么？",
        "文档里这张表的第二行第三列单元格是什么？",
        "该文档抽取的表中「金额」这一列的平均值是多少？",
    ):
        _check(f"表格信号否决 retrieval：{q[:20]}…", _route_doc_intent(q, IntentResult(intent="query")).intent == "query")

    # 10d) 就地改判：其余字段必须原样保留（早前返回新建的 IntentResult 会把它们丢光）
    r = _route_doc_intent(
        "文档里提到了哪些产品？",
        IntentResult(intent="query", chart_hint="bar", want_sql=True, answer_format="list"),
    )
    _check("改判后 intent=retrieval", r.intent == "retrieval", r.intent)
    _check("改判后 chart_hint 保留", r.chart_hint == "bar", str(r.chart_hint))
    _check("改判后 want_sql 保留", r.want_sql is True, str(r.want_sql))
    _check("改判后 answer_format 保留", r.answer_format == "list", str(r.answer_format))

    # 10e) 非 query/chat 意图不参与文档改判（analysis 的 pandas 链路只吃文档表格）
    for intent in ("analysis", "schema", "redraw", "retrieval"):
        r = _route_doc_intent("文档里提到了什么", IntentResult(intent=intent))
        _check(f"{intent} 不被文档改判", r.intent == intent, r.intent)

    # 11) 编排图分支闭合性（回归 F5）
    #     LangGraph 用 _route 的返回值去 _CLASSIFY_BRANCHES 查节点，查不到就抛
    #     KeyError: 'retrieve' —— 早前 retrieval 分支返回的是**节点名**而非键名，
    #     整条 RAG（文档问答）链路每次都以 SYSTEM_ERROR 收场，而类型检查看不见。
    #     覆盖全部意图，逐一断言返回值确实在表里。
    for intent in ("query", "chat", "redraw", "retrieval", "schema", "analysis"):
        key = _route({"intent": intent})
        _check(f"分支键在表内：{intent} → {key}", key in _CLASSIFY_BRANCHES, key)
    # 未知/缺失意图走 query 兜底，同样必须在表内
    for state in ({"intent": None}, {}, {"intent": "unknown"}):
        key = _route(state)
        _check(f"未知识图兜底到 query（{state}）", key == "query" and key in _CLASSIFY_BRANCHES, key)
    # 分支表的值必须都是**编出来的图里真实存在**的节点（防节点名写错）
    nodes = set(build_graph().nodes)
    for node in sorted(set(_CLASSIFY_BRANCHES.values())):
        _check(f"分支表指向已知节点：{node}", node in nodes, node)

    # 12) 赛题全量回归：三份题目集里**不该有任何一题**被改判为 retrieval。
    #     它们是表格题（列名/列序/单元格查找、多条件交集、金融计算），答案全在文档抽出的
    #     表格里；而 Milvus 里表格块只索引「列名 + 前 5 行样本」，一旦改判，这些题必然答不出。
    #     这是词表改动的**唯一**验收口径（CLAUDE.md 记的方法，在此固化成脚本）。
    #     数据在仓库外，缺失时跳过并明确打印 —— 静默通过等于没测。
    _competition_routing_regression()

    print("\n全部通过 ✓")


# 赛题数据目录（仓库外，仅供回归）：先看环境变量，再看几个约定位置
_COMPETITION_CANDIDATES = (
    Path(r"E:\ChongqingAI\赛题一数据\非结构化数据"),
    Path(__file__).resolve().parent.parent.parent / "赛题一数据" / "非结构化数据",
)
_COMPETITION_FILES = (
    "Table_Query_task.json",
    "Multi-step_Retrieval_task.json",
    "Table_Domain-specific_Operations_task.json",
)


def _competition_routing_regression() -> None:
    """遍历赛题题目集，断言被改判为 retrieval 的题数为 0。"""
    root = None
    env_dir = os.environ.get("COMPETITION_DATA_DIR")
    for cand in ([Path(env_dir)] if env_dir else []) + list(_COMPETITION_CANDIDATES):
        if cand.is_dir():
            root = cand
            break
    if root is None:
        print("[skip] 赛题题目集不存在，跳过全量分流回归（设 COMPETITION_DATA_DIR 可指定目录）")
        return

    total = 0
    for name in _COMPETITION_FILES:
        path = root / name
        if not path.is_file():
            print(f"[skip] 缺少 {name}，跳过")
            continue
        cases = json.loads(path.read_text(encoding="utf-8"))
        hijacked = [
            c.get("question", "")
            for c in cases
            if _route_doc_intent(str(c.get("question") or ""), IntentResult(intent="query")).intent == "retrieval"
        ]
        _check(
            f"赛题 {name}：{len(cases)} 题改判 retrieval 数为 0",
            not hijacked,
            f"{len(hijacked)} 题误判，例如：{hijacked[:3]}",
        )
        total += len(cases)
    print(f"[info] 赛题分流回归共覆盖 {total} 题")


if __name__ == "__main__":
    main()
