"""决策报告解析自检（纯函数，不依赖 LLM）。

线上症状：报告卡片的 ``summary`` 是一段被截断的 JSON 字符串、``key_findings`` 全空。
根因是 ``parse_report()`` 解析失败后走降级分支，把 agent 的原始输出（通常就是整份
报告 JSON）截断 500 字当成了摘要，前端原样显示，于是出现 ``"title":`` /
``"key_findings":[`` 这类字段名与标点。

本脚本验证解析链路的四层防线：

1. 配对花括号扫描（跳过字符串内的 ``{}``、兼容前后说明与多个对象）；
2. 宽松载入（Python 字面量、尾逗号、截断补全）；
3. 字段归一化（summary 嵌套 JSON、analysis 为 dict/数组、key_findings 元素为 dict）；
4. 彻底失败时返回固定友好文案，**原文绝不回显**。

运行：``cd backend && python scripts/test_report_parse.py``
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.agents.report import (
    _REPORT_FALLBACK_SUMMARY,
    _as_str_list,
    parse_report,
)


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def main() -> None:
    # 0) 归一化函数本身
    _check("None → []", _as_str_list(None) == [])
    _check("空串 → []", _as_str_list("") == [])
    _check("单字符串 → 列表", _as_str_list("一条发现") == ["一条发现"])
    _check("列表透传", _as_str_list(["a", ""]) == ["a"])
    _check("非字符串项转字符串", _as_str_list([1, 2]) == ["1", "2"])

    # 1) 可选字段为 null（线上实际形态）：必须解析成正常报告，不能降级
    text = (
        '{"title":"咖啡销售分析","summary":"三个月营业额合计 30592.94 元。",'
        '"key_findings":null,"recommendations":null,"confidence":"high"}'
    )
    r = parse_report(text, "统计各个月营业额")
    _check("null 字段不再触发降级", r.title == "咖啡销售分析", r.title)
    _check("summary 保留原结论", r.summary == "三个月营业额合计 30592.94 元。", r.summary)
    _check("key_findings 归一为空列表", r.key_findings == [], str(r.key_findings))
    _check("recommendations 为空时置 None", r.recommendations is None, str(r.recommendations))
    _check("confidence 正常解析", r.confidence == "high", str(r.confidence))

    # 2) key_findings 是单个字符串（模型偶发）：包成列表
    r = parse_report('{"title":"t","summary":"s","key_findings":"只有一条"}', "q")
    _check("字符串 key_findings 包成列表", r.key_findings == ["只有一条"], str(r.key_findings))
    _check("缺失 recommendations 置 None", r.recommendations is None)

    # 3) Markdown 围栏包裹：剥离后解析
    r = parse_report('```json\n{"title":"t","summary":"s","key_findings":["a"]}\n```', "q")
    _check("代码围栏内的 JSON 可解析", r.title == "t" and r.key_findings == ["a"], str(r))

    # 4) 非法 confidence 归一为 None
    r = parse_report('{"title":"t","summary":"s","key_findings":[],"confidence":"very-high"}', "q")
    _check("非法 confidence 置 None", r.confidence is None, str(r.confidence))

    # 5) 真的解析不了才降级，且**绝不回显原文**
    r = parse_report("这不是 JSON", "我的问题")
    _check("非 JSON 返回固定文案", r.summary == _REPORT_FALLBACK_SUMMARY, r.summary)
    _check("降级不泄漏原文", "这不是 JSON" not in r.summary, r.summary)
    _check("降级标题不再照抄问题", r.title == "数据分析报告", r.title)

    # 6) 字符串内含花括号：配对扫描要跳过，不能被截断
    r = parse_report(
        '{"title":"区间 {0, 10}","summary":"集合 {a, b} 的分布","key_findings":["x}y"]}',
        "q",
    )
    _check("字符串内花括号不被截断", r.title == "区间 {0, 10}", r.title)
    _check("summary 含花括号也完整", r.summary == "集合 {a, b} 的分布", r.summary)
    _check("key_findings 含花括号也完整", r.key_findings == ["x}y"], str(r.key_findings))

    # 7) 前后带说明文字 / 多个对象：选「最像报告」的那个
    r = parse_report(
        '分析如下：\n{"debug":true}\n最终报告：\n'
        '{"title":"真正报告","summary":"结论","key_findings":["a"]}\n以上。',
        "q",
    )
    _check("多对象时选中报告对象", r.title == "真正报告", r.title)
    _check("多对象时 summary 正确", r.summary == "结论", r.summary)

    # 8) 截断 JSON：补全后仍能救出字段
    r = parse_report(
        '{"title":"截断报告","summary":"这是一段结论","key_findings":["要点一","要点二"',
        "q",
    )
    _check("截断 JSON 标题可救", r.title == "截断报告", r.title)
    _check("截断 JSON summary 可救", r.summary == "这是一段结论", r.summary)
    _check("截断 JSON key_findings 可救", r.key_findings == ["要点一", "要点二"], str(r.key_findings))

    # 9) 尾逗号 / Python 单引号字面量
    r = parse_report("{'title':'t','summary':'s','key_findings':['a',],}", "q")
    _check("Python 字面量可解析", r.title == "t" and r.key_findings == ["a"], str(r))

    # 10) summary 是嵌套的整份报告（本次线上形态）：拆包并回填
    inner = json.dumps(
        {"title": "内层标题", "summary": "内层结论 12%", "key_findings": ["内层发现"]},
        ensure_ascii=False,
    )
    r = parse_report(json.dumps({"title": "外层标题", "summary": inner}, ensure_ascii=False), "q")
    _check("嵌套 summary 拆包", r.summary == "内层结论 12%", r.summary)
    _check("内层 key_findings 回填", r.key_findings == ["内层发现"], str(r.key_findings))
    _check("嵌套拆包后无字段名泄漏", '"summary"' not in r.summary, r.summary)

    # 11) analysis 是 dict / 数组：转成可读 Markdown，不触发整体降级
    r = parse_report(
        json.dumps(
            {
                "title": "t",
                "summary": "s",
                "key_findings": ["a"],
                "analysis": {"趋势": "上升", "原因": ["促销", "新品"]},
            },
            ensure_ascii=False,
        ),
        "q",
    )
    _check("analysis 为 dict 不触发降级", r.key_findings == ["a"], str(r.key_findings))
    _check("analysis 转可读文本", "上升" in (r.analysis or "") and "促销" in (r.analysis or ""), str(r.analysis))
    _check("analysis 不含花括号", "{" not in (r.analysis or "") and "'" not in (r.analysis or ""), str(r.analysis))

    # 12) key_findings 元素是 dict：转可读字符串，不泄漏字段名
    r = parse_report(
        json.dumps(
            {
                "title": "t",
                "summary": "s",
                "key_findings": [{"finding": "三月最高", "value": 1200}, {"text": "二月回落"}],
            },
            ensure_ascii=False,
        ),
        "q",
    )
    _check("dict 元素转字符串", r.key_findings == ["三月最高（数值：1200）", "二月回落"], str(r.key_findings))
    _check("dict 元素无花括号", all("{" not in x and "'" not in x for x in r.key_findings), str(r.key_findings))

    # 13) 标题照抄用户问题：视为无效标题，回退兜底文案
    r = parse_report('{"title":"统计各月销售","summary":"结论"}', "统计各月销售")
    _check("标题照抄问题则回退", r.title == "数据分析报告", r.title)

    # 14) 全空 JSON：不抛异常，返回固定文案
    r = parse_report('{"title":null,"summary":null,"key_findings":null}', "q")
    _check("全空 JSON 返回固定文案", r.summary == _REPORT_FALLBACK_SUMMARY, r.summary)
    _check("全空 JSON 不抛异常", r.key_findings == [], str(r.key_findings))

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
