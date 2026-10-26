"""输出格式（answer_format）纯函数离线自检：不依赖 LLM。

覆盖 ``app/llm/intent.py`` 的 ``detect_answer_format`` / ``render_list_answer`` 与
``app/agents/analysis.py`` 的 ``parse_analysis``（names 提取），验证：
列表/JSON 格式识别、列表渲染、从 items[].name 或 answer 列表字面量提取名称。

运行：``cd backend && python scripts/test_answer_format.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.agents.analysis import parse_analysis
from app.llm.intent import detect_answer_format, render_list_answer


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def main() -> None:
    # 1) detect_answer_format：显式列表 / JSON / 隐含「哪些」/ 无格式
    _check("哪些… 隐含列表", detect_answer_format("哪些咖啡品类同时满足平均交易金额与销量正相关、销售占比高于平均且近三个月销售额环比持续增长？") == "list")
    _check("显式「返回列表」", detect_answer_format("请返回列表") == "list")
    _check("显式「只返回名称」", detect_answer_format("只返回名称") == "list")
    _check("显式 JSON", detect_answer_format("请以 JSON 格式返回满足条件的品类") == "json")
    _check("无格式指令", detect_answer_format("统计各分类的数量") is None)
    _check("单答案「最高」", detect_answer_format("销量最高的产品") == "single")
    _check("单答案「哪些+最高」", detect_answer_format("哪些产品的销量最高") == "single")
    _check("单答案「哪个+最好」", detect_answer_format("哪个产品卖得最好") == "single")
    _check("复数「哪些」列表", detect_answer_format("哪些月份出现负增长") == "list")
    _check("明确多个「前三个」列表", detect_answer_format("销量最高的前三个产品") == "list")

    # 2) render_list_answer：Python 列表 / JSON 数组 / 空列表
    _check("render list", render_list_answer(["Latte", "Americano with Milk"], "list") == "['Latte', 'Americano with Milk']")
    _check("render json", render_list_answer(["Latte", "Americano with Milk"], "json") == '["Latte", "Americano with Milk"]')
    _check("render empty list", render_list_answer([], "list") == "[]")

    # 3) parse_analysis：从 items[].name 提取 / 从 answer 列表字面量兜底 / 降级
    r1 = parse_analysis('{"answer":"满足条件的有：Latte","items":[{"name":"Latte","share":0.2}]}', "q")
    _check("parse 从 items 提取 names", r1.names == ["Latte"], str(r1.names))
    r2 = parse_analysis('{"answer":"[\'Latte\', \'Americano with Milk\']", "items": null}', "q")
    _check("parse 从 answer 列表字面量提取", r2.names == ["Latte", "Americano with Milk"], str(r2.names))
    r3 = parse_analysis("无匹配", "q")
    _check("parse 降级无 names", r3.names is None, str(r3.names))
    # 降级文案必须是固定友好提示，**不得**把模型原文（此处为「无匹配」，
    # 真实场景多是被截断的 JSON 残片）当作答案回给用户
    _check("parse 降级不泄漏模型原文", "无匹配" not in (r3.answer or ""), repr(r3.answer))
    r3b = parse_analysis('{"answer": "线性趋势预测', "q")
    _check("parse 半截 JSON 不泄漏残片", "线性趋势预测" not in (r3b.answer or ""), repr(r3b.answer))

    # 4) parse_analysis：answer 为 JSON 数组（空列表 = 无匹配 / 非空列表 = 有匹配）
    r4 = parse_analysis('{"answer": [], "explanation": "三者无交集，故无满足条件的品类。", "items": []}', "q")
    _check("parse 空列表 answer 不降级、归一化为 []", r4.answer == "[]", repr(r4.answer))
    _check("parse 空列表 names 为 []", r4.names == [], str(r4.names))
    _check("parse 空列表 items 为 None", r4.items is None, str(r4.items))
    r5 = parse_analysis('{"answer": ["Latte"], "explanation": "…", "items": []}', "q")
    _check("parse 非空列表 answer 归一化为纯列表", r5.answer == "['Latte']", repr(r5.answer))
    _check("parse 非空列表 names", r5.names == ["Latte"], str(r5.names))

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
