"""数据分析（统计建模）：结构化结果模型、提示词与解析。

分析 agent 使用 ``create_agent``（带统计算子工具）做多步推理，最终输出 JSON。
由于 DeepSeek「思考模式」不支持 ``response_format``（强制 tool_choice），
这里采用**手动 JSON 解析**（与 ``app/llm/intent.py`` 的 ``_extract_json`` 同思路），
解析失败则降级为友好文案（**不把原文当答案**，那段文本通常是被截断的 JSON 残片），
保证分析能力不阻断主链路。

提示词由 ``build_analysis_system_prompt(schema_text)`` 拼出：规则 + **内联的表结构**
（省掉「先调 get_schema」的整轮 LLM 往返）。规则里最要紧的一条是「先一次性规划、
把能并行的工具调用放在同一轮」—— 早前的提示词是逐步引导（先用 A、再用 B、再用 C），
等于教模型每轮只发一个调用，一道多条件题因此要跑十几轮 LLM 往返（每轮 30~60s），
正是「流式响应长时间无数据」的耗时根因。
"""
import ast
import json
import logging
import re

from pydantic import BaseModel, Field

from app.llm.intent import render_list_answer

logger = logging.getLogger(__name__)


class AnalysisResult(BaseModel):
    """数据分析结构化输出（经 SSE ``result`` 事件映射为 QueryResponse 推送给前端）。"""

    answer: str = Field(description="最终答案：满足条件的项（如品类名称）列表 + 一句话概括")
    explanation: str | None = Field(default=None, description="计算过程 / 依据说明")
    items: list[dict] | None = Field(default=None, description="满足条件项的指标明细，每条含 name 与关键指标")
    names: list[str] | None = Field(default=None, description="满足条件项的名称列表（供列表格式输出）")


_ANALYSIS_SYSTEM_PROMPT = (
    "你是一名资深数据分析师。给定用户的问题，你可以调用一组统计工具在内存中取数与计算。\n"
    "\n"
    "【表结构】已随本提示一并给出（见末尾），**不要再调用 get_schema** —— 那会白白多花一整轮。\n"
    "\n"
    "【最重要的一条：先一次性规划，把能并行的调用放在同一轮】\n"
    "读题后先把整条计算链路想清楚，然后**在同一个回合里把所有互不依赖的工具调用一次性发出来**"
    "（例如同时加载两张表、或同时算「年均增长率」和「占比」）。同一轮的多个调用会被并行执行，"
    "一个来回就能拿到全部结果。**不要**一次只发一个调用、等结果回来再发下一个 —— 每个来回都要"
    "几十秒，逐步试探会把一道题拖成十几分钟。只有确实依赖前一步结果的调用（如 group_aggregate "
    "需要 load_table 返回的 df_id）才拆成两轮。\n"
    "\n"
    "【取数】load_table 读取相关表。\n"
    "  · 宽表（按 region/category/parameter/mode/powertrain 等维度逐行展开的长表）通常要**同时**"
    "固定好几列才能定位到目标数据，用 filters 一次给全：\n"
    '    filters=[{"col": "category", "op": "eq", "value": "Historical"}, '
    '{"col": "powertrain", "op": "eq", "value": "BEV"}]（条件之间是 AND）\n'
    "  · 只有一个条件时也可用 filter_col / filter_value / filter_op。\n"
    "  · 比较符 op 可选 eq/ne/gt/ge/lt/le/contains/not_contains/in；数值比较（「高于 10 万」）用 gt/ge。\n"
    "  · 返回里的 filters_applied 显示每个条件筛完还剩几行 —— 筛空了先看它，就知道是哪个条件筛没的。\n"
    "  · group_aggregate 做分组聚合（销量/笔数用 count，金额用 sum/avg，可按 date_col 按年/季/月聚合）。\n"
    "\n"
    "【术语 → 算子】按题目用词对号入座：\n"
    "  · 年均增长率 / 复合增长率 / CAGR / 年平均增速 → group_cagr（date_freq=\"year\"，"
    "用 start/end 给出区间，如 start=\"2015\", end=\"2020\"）\n"
    "  · 环比增长 / 环比增长最快 / 谁增长最多 → group_mom_growth_rate\n"
    "  · 是否**连续**增长 → group_mom_growth\n"
    "  · 占比 / 份额 / 谁占的比例高 → group_share\n"
    "  · 高于（全体/整体/各组）平均水平 → group_vs_overall_avg\n"
    "  · 相关性 / 是否相关 → group_correlate\n"
    "  · 预测 / 下期是多少 / 未来趋势 → forecast_time_series（给 value_col 与 date_col，可给 horizon 期数）\n"
    "  · 异常 / 离群值 → detect_anomalies；回归 / 趋势斜率 → linear_regression；聚类 / 归类 → cluster_kmeans\n"
    "\n"
    "【多条件交集】问题是「哪些…同时满足 A、B、C」时：先用上面的算子各算一个指标表，"
    "再把它们返回的 **df_id 一起**交给 intersect_groups（配 group_col 与 conditions 列表）求交，"
    "**不要自己逐组心算取交集**。\n"
    "  · group_cagr 的每组结果含 growth_rate、start_value、end_value（区间首/末值）、"
    "rank。**「末年水平超过 X」直接用 end_value 一起求交**"
    '（``{"col": "end_value", "op": ">", "value": 100000}``），不必另跑一次算子。\n'
    "  · **两段区间互比**（「预测期增长率高于历史均值」「增量超过历史总增量 50%」）用"
    "**列引用**表达：value 写成 ``{\"col\": \"另一列\", \"factor\": 0.5}``（可加 offset），"
    '例如 ``{"col": "increment_7", "op": ">", "value": {"col": "increment", "factor": 0.5}}``。'
    "做法是把两段区间各算一张 group_cagr 指标表，再一起求交。\n"
    "  · 跨两张指标表合并时，**第二张表的同名列会带上自己的 df_id 后缀**"
    "（df_id=7 那组的 growth_rate 合并后叫 growth_rate_7）—— 真实列名以返回的 columns 为准，"
    "照着它写条件。\n"
    "  · 若表里本身就有预测口径的行（如 category 含 Projection/预测），**直接 load_table "
    "筛出来算**，比用 forecast_time_series 外推更贴合原始口径。\n"
    "\n"
    "【输出格式】\n"
    "  · 问「哪些/哪类/哪几种…满足条件」（列出多项，含「哪些异常/哪些类」）时，answer 字段只放"
    '满足条件项名称的 Python 列表，例如 ["Latte", "Americano with Milk"]；无匹配则放空列表 []；'
    "不要附加任何解释文字。items 仍保留每个满足条件项的明细。\n"
    "  · 问单答案（「哪个/什么/最高/最多/唯一…」，只取一个项）时，answer 直接写那一个项的名称"
    "（字符串，如 'Latte'），不要用列表。\n"
    "  · 其它情况（「分析趋势」「评估相关性」「预测下期是多少」）answer 用一句话概括结论并给出关键数值"
    "（例如「线性趋势预测下一期约为 1234.5，拟合优度 R²=0.98」），把计算依据放到 explanation。\n"
    "\n"
    "工具之间的中间数据用 df_id 引用；每个算子返回里都标注了 ok，出错就调整参数重试。\n"
    "最后只输出一个 JSON 对象，不要输出任何其它文字、解释或 Markdown 代码块围栏，字段为：\n"
    '{"answer": "满足条件的项（名称列表，或结论一句话）", "explanation": "计算过程简述（可选）", '
    '"items": [{"name": "...", "...": "..."}]}'
)


def build_analysis_system_prompt(schema_text: str) -> str:
    """拼出分析 agent 的 system prompt：规则 + **直接内联**的表结构。

    表结构内联是为了省掉「先调一次 get_schema 看看有哪些表」的整轮 LLM 往返。
    文档源经多表收窄后候选表已经很少，prompt 不会因此膨胀；upload / 外部源本来
    就只有一两张表。``schema_text`` 为空时退化为规则（模型仍可自行调 get_schema）。
    """
    if not (schema_text or "").strip():
        return _ANALYSIS_SYSTEM_PROMPT
    return (
        f"{_ANALYSIS_SYSTEM_PROMPT}\n\n"
        f"===== 当前数据源的表结构（已给出，无需再调 get_schema）=====\n{schema_text}\n"
    )


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


def _parse_list_literal(s: str) -> list[str] | None:
    """安全解析纯列表字符串（JSON 数组或 Python 列表字面量），元素全为 str 才采纳。"""
    s = (s or "").strip()
    if not s:
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            v = loader(s)
        except Exception:
            continue
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return v
    return None


def _extract_names(items: list[dict] | None, answer: str) -> list[str] | None:
    """从 items 的 ``name`` 键提取名称列表；为空时回退解析 ``answer`` 的列表字面量。"""
    if items:
        names = [str(it["name"]).strip() for it in items if it.get("name")]
        if names:
            return names
    return _parse_list_literal(answer)


# 解析失败时的友好降级文案。**不把模型原文当答案**：这段文本通常是被截断的 JSON
# 残片（`{"answer": "线性趋势..."`），直接回给用户既不是答案也不是解释。
_FALLBACK_ANSWER = "分析已完成，但结果格式未能识别。请换一种说法重新提问，或改用更具体的问题（如「按品类统计销售额」）。"


def parse_analysis(text: str, question: str) -> AnalysisResult:
    """解析 agent 输出的 JSON 结果；失败时降级为固定友好文案。

    ``answer`` 可能是字符串或列表（模型把「Python 列表」直接输出成 JSON 数组，
    空列表 [] 表示无匹配）；统一归一化为字符串（``[]`` / ``['a', 'b']``）并提取 ``names``，
    避免空列表被 ``or`` 当作 falsy 而误入降级、把原始 JSON 泄漏给用户。
    """
    raw = _extract_json(text)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                items = data.get("items")
                if isinstance(items, list):
                    items = [x for x in items if isinstance(x, dict)]
                else:
                    items = None

                raw_answer = data.get("answer")
                if isinstance(raw_answer, list):
                    # 列表型 answer（含空列表 = 无匹配）
                    names = [str(x).strip() for x in raw_answer]
                    names = [n for n in names if n]
                    answer = render_list_answer(names, "list")
                else:
                    answer = str(raw_answer or "").strip()
                    names = _extract_names(items, answer)

                if answer or items:
                    return AnalysisResult(
                        answer=answer,
                        explanation=data.get("explanation"),
                        items=items or None,
                        names=names,
                    )
        except Exception:
            logger.warning("分析结果 JSON 解析失败，原文前 500 字：%s", (text or "")[:500])
    # 降级：固定文案（原文只进日志，不给用户看）
    return AnalysisResult(
        answer=_FALLBACK_ANSWER,
        explanation=None,
        items=None,
        names=None,
    )
