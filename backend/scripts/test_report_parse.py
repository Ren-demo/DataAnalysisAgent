"""决策报告解析自检（纯函数，不依赖 LLM）。

线上症状：报告卡片的 ``summary`` 是一段被截断的 JSON 字符串、``key_findings`` 全空。
根因是 DeepSeek 偶发输出 ``"key_findings": null``，``parse_report`` 直接对 ``None`` 做
列表推导抛 ``TypeError``，被 ``except`` 吞掉后整份报告降级为「原文前 500 字作摘要」。

本脚本验证：可选字段为 null / 单字符串 / 列表时都能正常解析，只有**真的解析不了**
（无 JSON、JSON 损坏）才降级。

运行：``cd backend && python scripts/test_report_parse.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.agents.report import _as_str_list, parse_report


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

    # 5) 真的解析不了才降级：summary 取原文前 500 字，标题回退为用户问题
    r = parse_report("这不是 JSON", "我的问题")
    _check("非 JSON 降级为原文摘要", r.summary == "这不是 JSON" and r.title == "我的问题", str(r))

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
