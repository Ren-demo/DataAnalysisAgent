"""决策报告：结构化报告模型与解析。

报告 agent 使用 ``create_agent``（带工具）做多步下钻推理，最终输出 JSON 报告。
由于 DeepSeek「思考模式」不支持 ``response_format``（强制 tool_choice），
这里采用**手动 JSON 解析**，流程为：

    配对花括号扫描 → 宽松载入（json / Python 字面量 / 截断补全）
    → 字段归一化（dict / 嵌套 JSON 转可读文本）→ 正则抢救 → 固定友好文案

解析失败时**绝不把模型原文当摘要回显** —— 那段文本通常是被截断的 JSON 残片，
用户看到的是 ``"title":`` / ``"key_findings":`` 这类字段名与标点，既不是结论也不可读。
原文只进日志，用户看到固定文案；这与 ``app/agents/analysis.py`` 的
``_FALLBACK_ANSWER`` 约定一致。
"""
import ast
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas import QueryResponse

logger = logging.getLogger(__name__)


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
    "可在需要外部事实或领域知识时调用它们补充信息；工具不可用不影响产出报告。\n\n"
    "【输出格式】最终只输出一个 JSON 对象，不要输出任何解释、前后缀或 Markdown 代码块围栏。字段：\n"
    "1. title：报告标题，20 字以内，概括分析主题；不要直接照抄用户问题。\n"
    "2. summary：核心结论，纯文本、一段话、不超过 120 字。只写最重要的事实与判断，"
    "尽量带关键数字和单位；不要使用 Markdown、列表、换行、引号包裹或 JSON 片段。\n"
    "3. key_findings：3~5 条关键发现的字符串数组。每条只写一个事实、40 字以内，"
    "尽量带数字与对比；不要写成一整段，也不要在元素里嵌套对象或 JSON。\n"
    "4. analysis：可选，详细分析。使用 Markdown，建议用 2~4 个 ### 小标题分段，"
    "配合短段落、列表或表格说明依据、对比与推理过程；不要放 JSON、代码块或键值对碎片。\n"
    "5. recommendations：可选，2~4 条可直接执行的建议字符串数组，每条 40 字以内。\n"
    "6. confidence：可选，只能是 \"high\" / \"medium\" / \"low\" 之一。\n\n"
    "【硬性约束】\n"
    "- 整个 JSON 序列化后控制在 1800 字以内，避免因过长被截断。\n"
    "- JSON 必须完整闭合：引号、方括号、花括号成对出现；不要输出尾逗号；"
    "字符串内不要出现未转义的换行。\n"
    "- 只使用查询结果与工具返回的事实，不得编造数字；数据不足时在 summary 中说明局限。\n"
    "- 即使无法形成结论，也必须输出合法 JSON（summary 说明数据不足），不要输出半截 JSON。\n"
    "- 不要重复 SQL、字段名清单或原始数据行，用业务语言解释它们的含义。\n\n"
    "结构示例（仅示意，不要照抄内容）：\n"
    '{"title":"近三个月销售趋势","summary":"近三个月销售额合计 30592.94 元，环比增长 12%，3 月创新高。",'
    '"key_findings":["3 月销售额最高，为 12000 元","2 月环比下降 8%"],'
    '"analysis":"### 趋势\\n销售额整体上升。\\n\\n### 原因\\n- 促销带动",'
    '"recommendations":["加大 3 月促销力度"],"confidence":"medium"}'
)


# 报告 agent 输入中最多携带的结果行数，防止大结果撑爆上下文
MAX_REPORT_ROWS = 20

# 解析防御上限：异常输入（超长文本、大量花括号）不应让扫描爆炸
_MAX_SCAN_CHARS = 20000
_MAX_CANDIDATES = 50
_MAX_FINDINGS = 6

_REPORT_FIELD_KEYS = (
    "title",
    "summary",
    "key_findings",
    "analysis",
    "recommendations",
    "confidence",
)

_SUMMARY_KEYS = (
    "summary",
    "conclusion",
    "core_conclusion",
    "core",
    "answer",
    "text",
    "content",
    "结论",
    "核心结论",
    "摘要",
)

# summary 归一化后的硬上限（提示词只要求 120 字，这里防历史脏数据/模型失控）
_SUMMARY_MAX_CHARS = 600
_ANALYSIS_MAX_CHARS = 6000

_REPORT_FALLBACK_TITLE = "数据分析报告"
_REPORT_FALLBACK_SUMMARY = (
    "本次分析已完成，但报告摘要暂时无法生成。"
    "请结合上方的查询结果与图表理解数据；如需更深入的结论，可换一种更具体的问法重新提问。"
)

# 常见英文键的中文标签，让 dict → 文本时读起来像人话
_FIELD_LABELS = {
    "summary": "摘要",
    "title": "标题",
    "conclusion": "结论",
    "core": "核心结论",
    "trend": "趋势",
    "reason": "原因",
    "reasons": "原因",
    "evidence": "依据",
    "detail": "明细",
    "details": "明细",
    "risk": "风险",
    "risks": "风险",
    "recommendation": "建议",
    "recommendations": "建议",
    "metric": "指标",
    "value": "数值",
    "change": "变化",
    "impact": "影响",
    "data": "数据",
}


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


# --------------------------------------------------------------------------- #
# 一、提取与宽松载入
# --------------------------------------------------------------------------- #


def _matching_brace_end(text: str, start: int) -> int | None:
    """返回与 ``text[start]``（``{``）配对的 ``}`` 下标；跳过字符串内的花括号。"""
    depth = 0
    quote: str | None = None
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _iter_json_objects(text: str) -> list[str]:
    """扫描文本中的 JSON 对象候选；未闭合的尾片也保留，交给截断补全。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n and len(out) < _MAX_CANDIDATES:
        if text[i] != "{":
            i += 1
            continue
        end = _matching_brace_end(text, i)
        if end is None:
            # 这个 { 之后不可能再有完整对象（后续的 { 都在它内部）
            out.append(text[i:])
            break
        out.append(text[i : end + 1])
        i = end + 1
    return out


def _report_key_score(candidate: str) -> int:
    """候选里命中的报告字段越多，越可能是我们要的那份报告。"""
    return sum(
        1
        for key in _REPORT_FIELD_KEYS
        if re.search(rf"""["']{key}["']\s*:""", candidate)
    )


def _extract_json(text: str) -> str | None:
    """从文本中选出最像报告 JSON 对象的片段。

    与旧实现的区别：用配对花括号扫描（跳过字符串内的 ``{}``），因此能正确处理
    「正文里带花括号」「前后有说明文字」「输出里出现多个对象」等形态；未闭合的
    截断尾片也会作为候选返回，由 ``_loads_lenient`` 补全。
    """
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > _MAX_SCAN_CHARS:
        text = text[:_MAX_SCAN_CHARS]

    sources = [
        m.group(1).strip()
        for m in re.finditer(r"```(?:json|markdown|md)?\s*(.*?)\s*```", text, re.S | re.I)
    ]
    sources.append(text)

    candidates: list[str] = []
    for src in sources:
        candidates.extend(_iter_json_objects(src))
    if not candidates:
        return None
    return max(candidates, key=lambda c: (_report_key_score(c), len(c)))


def _escape_control_chars_in_strings(text: str) -> str:
    """把字符串内的裸换行 / 制表符转义为 ``\\n`` / ``\\t``（JSON 不允许裸控制符）。"""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == quote:
                quote = None
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
        out.append(ch)
    return "".join(out)


def _close_unbalanced_json(text: str) -> str | None:
    """补全被截断的字符串 / 括号；括号明显错配时返回 None。"""
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            else:
                return None
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
            else:
                return None

    out = text
    if quote:
        if out.endswith("\\"):
            out = out[:-1]
        out += quote
    out = re.sub(r"[,:]\s*$", "", out)
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    out = re.sub(r",\s*([}\]])", r"\1", out)
    # 末尾悬空 key（"analysis":）补空串，避免补括号后仍然非法
    out = re.sub(r":\s*([}\]])", r'""\1', out)
    return out


def _cut_candidates(text: str, limit: int = 8) -> list[str]:
    """取字符串外的逗号作为截断点，生成「丢掉半截最后一项」的候选。"""
    cuts: list[int] = []
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == ",":
            cuts.append(i)
    return [text[:i].rstrip().rstrip(",").rstrip(":") for i in reversed(cuts[-limit:])]


def _repair_candidates(raw: str) -> list[str]:
    """生成「可能能解析」的修复候选，按可信度从高到低排列。"""
    raw = raw.replace("﻿", "").replace("​", "")
    # 中文引号只在全文没有 ASCII 双引号时才视为 JSON 分隔符：
    # 否则会破坏正常正文里的 “……”
    if '"' not in raw and "“" in raw:
        raw = raw.replace("“", '"').replace("”", '"')
    raw = _escape_control_chars_in_strings(raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

    variants = [raw]
    closed = _close_unbalanced_json(raw)
    if closed:
        variants.append(closed)
    for prefix in _cut_candidates(raw):
        closed = _close_unbalanced_json(prefix)
        if closed:
            variants.append(closed)
    # dict.fromkeys 去重且保序
    return list(dict.fromkeys(v for v in variants if v.strip()))


def _loads_lenient(raw: str) -> Any:
    """宽松载入：JSON → Python 字面量 → 常见修复后重试。

    ``ast.literal_eval`` 兼容模型偶发输出的 Python 字面量（单引号、``None`` / ``True``）。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    for candidate in [raw, *_repair_candidates(raw)]:
        for loader in (json.loads, ast.literal_eval):
            try:
                return loader(candidate)
            except Exception:
                continue
    return None


def _coerce_report_dict(value: Any) -> dict | None:
    """把解析结果收敛成「报告对象」：剥掉数组与 ``report`` / ``data`` / ``result`` 包装。"""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and any(k in item for k in _REPORT_FIELD_KEYS):
                return item
        return None
    if not isinstance(value, dict):
        return None
    if any(k in value for k in _REPORT_FIELD_KEYS):
        return value
    for wrapper in ("report", "data", "result"):
        inner = value.get(wrapper)
        if isinstance(inner, dict):
            return _coerce_report_dict(inner)
        if isinstance(inner, str):
            raw = _extract_json(inner)
            if raw:
                got = _coerce_report_dict(_loads_lenient(raw))
                if got:
                    return got
    return None


# --------------------------------------------------------------------------- #
# 二、正则抢救（JSON 无法修复时的最后手段）
# --------------------------------------------------------------------------- #

_STRING_FIELD_RE = {
    key: re.compile(
        rf"""["']{key}["']\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""",
        re.S,
    )
    for key in ("title", "summary", "analysis")
}


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s.replace("\\n", "\n").replace('\\"', '"').replace("\\'", "'")


def _salvage_string(text: str, key: str) -> str | None:
    m = _STRING_FIELD_RE[key].search(text)
    if m:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        return _unescape(raw)
    # 截断在字符串中间：取到下一个引号 / 换行为止
    m = re.search(rf"""["']{key}["']\s*:\s*["']([^"'\n]{{1,800}})""", text)
    return _unescape(m.group(1)).strip() if m else None


def _salvage_array(text: str, key: str) -> list | None:
    m = re.search(rf"""["']{key}["']\s*:\s*\[(.*?)(?:\]|$)""", text, re.S)
    if not m:
        return None
    body = m.group(1)
    for loader in (json.loads, ast.literal_eval):
        try:
            value = loader("[" + body + "]")
            if isinstance(value, list):
                return value
        except Exception:
            continue
    # 半截数组：抓引号字符串，跳过后面紧跟冒号的键名
    items = [
        _unescape(m.group(1) or m.group(2))
        for m in re.finditer(r"""["']((?:[^"'\\]|\\.)*)["'](?!\s*:)""", body)
    ]
    return items or None


def _salvage_report(text: str) -> dict | None:
    """正则抢救：只抓报告字段，绝不把原文整段当摘要。"""
    text = (text or "")[: _MAX_SCAN_CHARS * 2]
    data: dict = {}
    for key in ("title", "summary", "analysis"):
        value = _salvage_string(text, key)
        if value:
            data[key] = value
    for key in ("key_findings", "recommendations"):
        value = _salvage_array(text, key)
        if value:
            data[key] = value
    m = re.search(r"""["']confidence["']\s*:\s*["'](high|medium|low)["']""", text, re.I)
    if m:
        data["confidence"] = m.group(1).lower()
    return data or None


# --------------------------------------------------------------------------- #
# 三、字段归一化（dict / 嵌套 JSON 一律转成可读文本）
# --------------------------------------------------------------------------- #


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"```(?:json|markdown|md)?\s*(.*?)\s*```", text, re.S | re.I)
    return m.group(1).strip() if m else text


def _looks_like_json(text: str) -> bool:
    s = (text or "").lstrip()
    return bool(s) and s[0] in "{[" and bool(re.search(r"""["']\s*:\s*""", s))


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _label(key: Any) -> str:
    return _FIELD_LABELS.get(str(key).lower(), str(key))


def _clean_text(text: str) -> str:
    """清掉围栏、包裹引号与残留的 JSON 字段名；明显不可读时返回空串。"""
    s = _strip_fence(text).strip().strip('"').strip("'").strip()
    s = re.sub(
        r"""["'](?:title|summary|key_findings|analysis|recommendations|confidence)["']\s*:\s*""",
        "",
        s,
        flags=re.I,
    )
    if s[:1] in "{[":
        s = s.strip("{[]},").strip()
    s = s.strip(":").strip()
    # 仍有 ≥2 个 "键": 形态的残片：判定不可读，交给上层降级
    if len(re.findall(r"""["'][^"'\n]{1,40}["']\s*:""", s)) >= 2:
        return ""
    return s


def _dict_line(value: dict) -> str:
    """把一个 dict 元素压成一行可读文本；绝不 ``str(dict)`` 泄漏花括号。"""
    primary_keys = (
        "text",
        "finding",
        "content",
        "description",
        "summary",
        "title",
        "point",
        "label",
        "name",
    )
    primary = next((k for k in primary_keys if _scalar_text(value.get(k))), None)
    if primary:
        main = _scalar_text(value[primary]).replace("\n", " ").strip()
        extras = [
            f"{_label(k)}：{_scalar_text(v)}"
            for k, v in value.items()
            if k != primary and _scalar_text(v)
        ]
        return main + (f"（{'；'.join(extras)}）" if extras else "")
    return "；".join(
        f"{_label(k)}：{_scalar_text(v)}" for k, v in value.items() if _scalar_text(v)
    )


def _maybe_report_dict(value: Any) -> dict | None:
    """把 summary 里嵌套的「整份报告」还原成 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = _extract_json(value)
        if raw:
            return _coerce_report_dict(_loads_lenient(raw))
    return None


def _as_summary(value: Any, depth: int = 0) -> str:
    """把 summary 归一为一段可读纯文本；dict / 嵌套 JSON 字符串自动拆包。"""
    if value is None or depth > 3:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("null", "none", "{}", "[]"):
            return ""
        parsed = _loads_lenient(_strip_fence(text))
        if isinstance(parsed, (dict, list)):
            return _as_summary(parsed, depth + 1)
        if _looks_like_json(text):
            nested = _maybe_report_dict(text)
            if nested:
                got = _as_summary(nested.get("summary"), depth + 1)
                if got:
                    return got
            m = re.search(
                r"""["'](?:summary|conclusion|core|content|text)["']\s*:\s*["']((?:[^"'\\]|\\.)*)""",
                text,
                re.S | re.I,
            )
            return _as_summary(_unescape(m.group(1)), depth + 1) if m else ""
        return _clean_text(text)
    if isinstance(value, dict):
        for key in _SUMMARY_KEYS:
            if value.get(key):
                got = _as_summary(value[key], depth + 1)
                if got:
                    return got
        parts = [_as_summary(v, depth + 1) for v in value.values()]
        return "；".join(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        parts = [_as_summary(v, depth + 1) for v in value]
        return "；".join(p for p in parts if p)
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _as_text(value: Any, depth: int = 0) -> str:
    """把 analysis 归一为可读 Markdown；dict / 数组绝不 ``str()``。"""
    if value is None or depth > 3:
        return ""
    if isinstance(value, str):
        text = _strip_fence(value)
        if not text or text.lower() in ("null", "none", "{}", "[]"):
            return ""
        if _looks_like_json(text):
            parsed = _loads_lenient(text)
            if isinstance(parsed, (dict, list)):
                return _as_text(parsed, depth + 1)
            return _clean_text(text)
        return text
    if isinstance(value, dict):
        for key in ("analysis", "detail", "details", "text", "content", "summary"):
            if value.get(key):
                got = _as_text(value[key], depth + 1)
                if got:
                    return got
        lines: list[str] = []
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                sub = _as_text(val, depth + 1)
                if sub:
                    lines.append(
                        f"- **{_label(key)}**：\n{sub}"
                        if "\n" in sub
                        else f"- **{_label(key)}**：{sub}"
                    )
            else:
                scalar = _scalar_text(val)
                if scalar:
                    lines.append(f"- **{_label(key)}**：{scalar}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            if isinstance(item, dict):
                line = _dict_line(item)
                if line:
                    lines.append(f"- {line}")
            else:
                sub = _as_text(item, depth + 1)
                if sub:
                    lines.append(sub if "\n" in sub else f"- {sub}")
        return "\n".join(lines)
    return _scalar_text(value)


def _as_str_list(value: Any, limit: int = _MAX_FINDINGS) -> list[str]:
    """把模型可能返回的 null / 单字符串 / 列表 / dict 元素统一成可读字符串列表。

    DeepSeek 偶发输出 ``"key_findings": null``：早前直接对 ``None`` 做列表推导会抛
    ``TypeError``，被外层 ``except`` 吞掉后整份报告降级为「原文前 500 字作摘要」——
    前端看到的是一段被截断的 JSON 字符串、``key_findings`` 全空，看起来像报告坏了。
    列表元素是 dict 时同样不能 ``str(dict)``，否则字段名会再次泄漏到界面上。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = _strip_fence(value)
        if not text or text.lower() in ("null", "none", "{}", "[]"):
            return []
        parsed = _loads_lenient(text)
        if isinstance(parsed, list):
            return _as_str_list(parsed, limit)
        if isinstance(parsed, dict):
            line = _dict_line(parsed)
            return [line] if line else []
        if _looks_like_json(text):
            nested = _maybe_report_dict(text)
            if nested:
                line = _dict_line(nested)
                return [line] if line else []
            return []
        text = _clean_text(text)
        return [text] if text else []
    if isinstance(value, dict):
        line = _dict_line(value)
        return [line] if line else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_as_str_list(item, limit))
            if len(out) >= limit:
                break
        result: list[str] = []
        seen: set[str] = set()
        for item in out:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                result.append(item)
            if len(result) >= limit:
                break
        return result
    text = _scalar_text(value)
    return [text] if text else []


def _as_confidence(value: Any) -> Literal["high", "medium", "low"] | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.lower() in ("high", "medium", "low"):
        return v.lower()  # type: ignore[return-value]
    return {
        "高": "high",
        "中": "medium",
        "低": "low",
        "高置信度": "high",
        "中置信度": "medium",
        "低置信度": "low",
    }.get(v)  # type: ignore[return-value]


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _as_title(value: Any, question: str) -> str:
    title = _as_summary(value)
    title = re.sub(r"\s+", " ", title).strip()
    # 旧的降级分支会把用户问题当标题；这种「标题」没有信息量，交给兜底标题
    if question and title == (question or "").strip():
        return ""
    return _truncate(title, 40)


def _merge_nested_report(data: dict) -> dict:
    """summary 本身是一份完整报告时，拆包并把缺失字段回填到外层。"""
    nested = _maybe_report_dict(data.get("summary"))
    if not isinstance(nested, dict) or not any(k in nested for k in _REPORT_FIELD_KEYS):
        return data

    merged = dict(nested)
    for key, value in data.items():
        if key == "summary":
            continue
        if value not in (None, "", [], {}):
            merged[key] = value
    merged["summary"] = next(
        (nested[k] for k in _SUMMARY_KEYS if nested.get(k)), nested.get("summary", "")
    )
    return merged


def _normalize_report_dict(data: dict | None, question: str) -> ReportResult | None:
    """把任意形态的报告 dict 收敛成合法 ``ReportResult``；没有有效内容返回 None。"""
    if not isinstance(data, dict):
        return None
    data = _merge_nested_report(data)

    title = _as_title(data.get("title"), question)
    summary = re.sub(r"\s+", " ", _as_summary(data.get("summary"))).strip()
    summary = _truncate(summary, _SUMMARY_MAX_CHARS)
    findings = _as_str_list(data.get("key_findings"))
    analysis = _truncate(_as_text(data.get("analysis")), _ANALYSIS_MAX_CHARS) or None
    recommendations = _as_str_list(data.get("recommendations")) or None
    confidence = _as_confidence(data.get("confidence"))

    # summary 为空时，用第一条关键发现兜底，避免「大片空白 + 只有列表」
    if not summary:
        summary = findings[0] if findings else ""
    if not summary and analysis:
        summary = "本次分析已完成，详见下方详细分析。"
    if not summary and recommendations:
        summary = "本次分析已完成，建议如下。"

    # 只有 title 不算有效报告（模型输出了一个空壳对象）
    if not (summary or findings or analysis or recommendations):
        return None

    return ReportResult(
        title=title or _REPORT_FALLBACK_TITLE,
        summary=summary,
        key_findings=findings,
        analysis=analysis,
        recommendations=recommendations,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# 四、对外入口
# --------------------------------------------------------------------------- #


def parse_report(text: str, question: str) -> ReportResult:
    """解析 agent 输出的 JSON 报告。

    顺序：配对扫描 → 宽松载入 → 字段归一化；失败再走正则抢救；仍失败则返回固定
    友好文案。**任何情况下都不把模型原文（尤其是 JSON 残片）当 summary 展示**。
    """
    original = (text or "").strip()

    raw = _extract_json(original)
    data = _coerce_report_dict(_loads_lenient(raw)) if raw else None
    report = _normalize_report_dict(data, question)
    if report is not None:
        return report

    salvaged = _salvage_report(original)
    report = _normalize_report_dict(salvaged, question)
    if report is not None:
        logger.debug("决策报告 JSON 解析失败，已通过正则抢救字段。question=%s", question)
        return report

    # 原文只进日志，绝不回显给用户：它通常是被截断的 JSON 残片
    logger.warning(
        "决策报告解析失败，已返回固定降级文案；question=%s，原文前 500 字：%s",
        question,
        original[:500],
    )
    return ReportResult(
        title=_REPORT_FALLBACK_TITLE,
        summary=_REPORT_FALLBACK_SUMMARY,
        key_findings=[],
    )
