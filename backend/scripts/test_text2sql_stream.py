"""流式 text2sql 的离线自检：不依赖 LLM，用假模型驱动。

覆盖 ``app/llm/text2sql.py`` 的 ``generate_sql_stream`` / ``_response_to_text`` /
``_retry_prompt``，回归三处：

1. **token 在校验前泄漏**：早前逐 token 直接 yield，校验在流结束后才做 ——
   用户会看到一段最终未通过校验、也从未执行的 SQL（展示与执行不一致）。
   现在必须先缓存、校验通过后回放。
2. **流式链路无重试**：``MAX_RETRY`` 只作用于非流式的 ``generate_sql``，
   文档承诺的「失败自动重试」在主 SSE 链路并不生效。
3. **重试不回喂上一版 SQL**：模型看不到自己写了什么，重试常原样再犯同一个错。

运行：``cd backend && python scripts/test_text2sql_stream.py``
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.errors import AppError
from app.llm.text2sql import (
    MAX_RETRY,
    _response_to_text,
    _retry_prompt,
    generate_sql_stream,
)


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


class _Chunk:
    """模拟 AIMessageChunk：只需 ``content`` 属性。"""

    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """按脚本逐次响应：脚本项为 list[str]（本次流出的 token）或 Exception（本次抛错）。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.prompts: list[str] = []

    async def astream(self, prompt):
        self.prompts.append(prompt)
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        for piece in item:
            yield _Chunk(piece)


async def _drain(llm, question="统计各分类数量", schema="CREATE TABLE t (category TEXT);"):
    """驱动生成器，返回 (yield 出的 token 列表, 抛出的异常或 None)。"""
    tokens: list[str] = []
    try:
        with patch("app.llm.text2sql.get_llm", return_value=llm):
            async for token in generate_sql_stream(question, schema, "sqlite"):
                tokens.append(token)
    except Exception as e:  # noqa: BLE001 — 自检需要捕获任意异常以断言类型
        return tokens, e
    return tokens, None


def main() -> None:
    # 1) F6：失败那次的 token **一个都不能流出**，重试成功后只回放成功那次
    llm = _FakeLLM([["DELETE FROM t"], ["SELECT ", "category FROM t"]])
    tokens, err = asyncio.run(_drain(llm))
    _check("失败尝试无异常抛出", err is None, repr(err))
    _check("只回放通过校验的那次输出", "".join(tokens) == "SELECT category FROM t", repr(tokens))
    _check("被拒的 DELETE 从未流出", "DELETE" not in "".join(tokens), repr(tokens))
    _check("确实重试了（2 次调用）", len(llm.prompts) == 2, str(len(llm.prompts)))

    # 2) F7：重试提示里带上了上一版 SQL 原文
    _check("重试提示含上一版 SQL", "DELETE FROM t" in llm.prompts[1], llm.prompts[1][-200:])
    _check("重试提示含校验失败说明", "校验失败" in llm.prompts[1])

    # 3) F5：语法错误（无法解析）同样重试
    llm2 = _FakeLLM([["这不是 SQL"], ["SELECT 1"]])
    tokens2, err2 = asyncio.run(_drain(llm2))
    _check("语法错误后重试成功", err2 is None and "".join(tokens2) == "SELECT 1", repr((tokens2, err2)))
    _check("乱码内容未流出", "这不是 SQL" not in "".join(tokens2), repr(tokens2))

    # 4) 网络异常重试，而非立即失败
    llm3 = _FakeLLM([ConnectionError("boom"), ["SELECT 1"]])
    tokens3, err3 = asyncio.run(_drain(llm3))
    _check("网络异常后重试成功", err3 is None and "".join(tokens3) == "SELECT 1", repr((tokens3, err3)))

    # 5) 一直失败 → SQL_ERROR，且全程无 token 流出
    llm4 = _FakeLLM([["DELETE FROM t"]] * MAX_RETRY)
    tokens4, err4 = asyncio.run(_drain(llm4))
    _check("多次校验失败抛 AppError", isinstance(err4, AppError), repr(err4))
    _check("错误码为 SQL_ERROR", getattr(err4, "error_code", None) == "SQL_ERROR", repr(err4))
    _check("失败路径无 token 流出", tokens4 == [], repr(tokens4))

    # 6) 一直网络失败 → LLM_UNAVAILABLE
    llm5 = _FakeLLM([ConnectionError("boom")] * MAX_RETRY)
    _, err5 = asyncio.run(_drain(llm5))
    _check("多次网络失败抛 LLM_UNAVAILABLE", getattr(err5, "error_code", None) == "LLM_UNAVAILABLE", repr(err5))

    # 7) _response_to_text：结构化块列表不再被 str() 成 Python repr
    _check("字符串 content 原样返回", _response_to_text(_Chunk("SELECT 1")) == "SELECT 1")
    _check(
        "块列表 content 拼接文本",
        _response_to_text(_Chunk([{"type": "text", "text": "SELECT "}, {"type": "text", "text": "1"}]))
        == "SELECT 1",
    )
    _check(
        "嵌套 text.value 形态",
        _response_to_text(_Chunk([{"type": "text", "text": {"value": "SELECT 1"}}])) == "SELECT 1",
    )
    _check(
        "混合 str 与块",
        _response_to_text(_Chunk(["SELECT ", {"type": "text", "text": "1"}])) == "SELECT 1",
    )

    # 8) _retry_prompt：无上一版 SQL 时给出另一种措辞（不出现空的「你的 SQL 是：」）
    p = _retry_prompt("q", "schema", "sqlite", "", ValueError("SQL 无法解析"))
    _check("无上一版 SQL 时不拼接空 SQL 块", "你上次输出的 SQL 是" not in p, p[-120:])
    _check("无上一版 SQL 时仍提示重写", "请直接输出一条正确的 SELECT 语句" in p)

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
