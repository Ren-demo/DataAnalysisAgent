"""分析 / 报告 agent 的流式进度自检（替身驱动，不连 LLM、不连库）。

``_run_agent_streamed`` 把原先的 ``await agent.ainvoke(...)``（整段 ReAct 循环被缓冲、
全程零事件）换成 ``agent.astream(..., stream_mode="updates")``，边跑边下发
``{"type": "progress", ...}``，并把最终 AI 文本还原出来。本脚本用桩 agent 断言：

1. 入口先发一条「正在规划分析步骤…」/「正在准备决策报告…」（intent 之后到第一次工具
   调用之间还有一整轮 LLM，不报就仍是静默）；
2. 带 tool_calls 的 AI 消息 → 「正在调用 X…」，多个调用 → 「正在并行调用 X、Y…」；
3. ToolMessage → 「X 已完成」；未登记的工具名原样展示，不编造中文名；
4. **最终文本 = 最后一条不带 tool_calls 的 AI 消息**（等价于原先的
   ``out["messages"][-1].content``），结构化 content 块也能正确拼接；
5. 分片形状不符（字符串 / None / messages 不是列表 / 整块不是 dict）时**不抛异常、
   不发事件**，并且不影响最终文本 —— 进度提示是锦上添花，不该弄坏结果；
6. 同一条消息（同 id）重复出现只播报一次；
7. 事件下发本身失败（``w`` 抛异常）不影响执行；
8. 拿不到任何 AI 文本时抛 RuntimeError（调用方据此回退，而不是静默给空答案）；
9. 条数上限 ``_MAX_PROGRESS`` 生效；
10. ``stream_mode="updates"`` 与 ``recursion_limit=settings.agent_recursion_limit``
    确实传给了 agent（步数兜底，防跑飞的 agent 无限期烧 token）。

运行：``cd backend && python scripts/test_agent_progress.py``
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.graph import (
    _MAX_PROGRESS,
    _ai_text,
    _progress_text,
    _run_agent_streamed,
    _tool_label,
)
from app.config import settings


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


class _StubAgent:
    """替身 agent：按 ``chunks`` 顺序产出 astream 分片，并记录收到的入参。"""

    def __init__(self, chunks: list) -> None:
        self.chunks = chunks
        self.calls: list[dict] = []

    async def astream(self, payload, **kwargs):
        self.calls.append({"payload": payload, **kwargs})
        for chunk in self.chunks:
            yield chunk


def _call_msg(name: str, mid: str, **extra) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": extra.get("args", {}), "id": f"{mid}-call"}],
        id=mid,
    )


async def _run(chunks: list, scope: str = "analysis", sink=None):
    """跑一次 _run_agent_streamed，返回 (最终文本, 事件列表, 桩 agent)。"""
    events: list[dict] = []
    agent = _StubAgent(chunks)

    def w(evt: dict) -> None:
        events.append(evt)
        if sink is not None:
            sink(evt)

    text = await _run_agent_streamed(agent, "问题", w, scope)
    return text, events, agent


# ---------------------------------------------------------------- 1~4) 文案与最终文本
async def _test_progress_texts() -> None:
    chunks = [
        {"model": {"messages": [_call_msg("load_table", "m1")]}},
        {"tools": {"messages": [ToolMessage(content="{}", name="load_table", tool_call_id="m1-call", id="t1")]}},
        {"model": {"messages": [AIMessage(content="", tool_calls=[
            {"name": "group_cagr", "args": {}, "id": "c2"},
            {"name": "group_aggregate", "args": {}, "id": "c3"},
        ], id="m2")]}},
        {"tools": {"messages": [
            ToolMessage(content="{}", name="group_cagr", tool_call_id="c2", id="t2"),
            ToolMessage(content="{}", name="group_aggregate", tool_call_id="c3", id="t3"),
        ]}},
        {"model": {"messages": [AIMessage(content='{"answer": ["R1"]}', id="m4")]}},
    ]
    text, events, agent = await _run(chunks)

    _check(
        "首条是入口文案（intent 之后到首次工具调用之间也是静默）",
        events[0]["text"] == "正在规划分析步骤…" and events[0]["scope"] == "analysis",
        str(events[:1]),
    )
    texts = [e["text"] for e in events]
    _check("带 tool_calls 的 AI 消息 → 正在调用 X…", texts[1] == "正在调用 加载数据…", str(texts))
    _check("ToolMessage → X 已完成", texts[2] == "加载数据 已完成", str(texts))
    _check(
        "同一轮多个调用 → 正在并行调用 X、Y…",
        texts[3] == "正在并行调用 年均增长率、分组聚合…",
        str(texts),
    )
    _check("两个工具各播报一次完成", texts[4:6] == ["年均增长率 已完成", "分组聚合 已完成"], str(texts))
    _check(
        "最终文本 = 最后一条不带 tool_calls 的 AI 消息",
        text == '{"answer": ["R1"]}',
        repr(text),
    )
    _check("入参 payload 是 HumanMessage", isinstance(agent.calls[0]["payload"]["messages"][0], HumanMessage))
    _check("stream_mode 用 updates", agent.calls[0].get("stream_mode") == "updates", str(agent.calls[0].keys()))
    _check(
        "recursion_limit 传给 agent（步数兜底，langgraph 默认 10000 等于不设限）",
        agent.calls[0].get("config", {}).get("recursion_limit") == settings.agent_recursion_limit,
        str(agent.calls[0].get("config")),
    )

    # report 分支的入口文案不同（前端在 result 之后单独开位显示）
    _, evts_r, _ = await _run([{"model": {"messages": [AIMessage(content="{}", id="r1")]}}], scope="report")
    _check("report 分支入口文案", evts_r[0]["text"] == "正在准备决策报告…" and evts_r[0]["scope"] == "report", str(evts_r))


# ---------------------------------------------------------------- 5) 结构化 content
async def _test_structured_content() -> None:
    chunks = [
        {"model": {"messages": [AIMessage(
            content=[{"type": "text", "text": '{"answer": '}, {"type": "text", "text": '"ok"}'}],
            id="m1",
        )]}},
    ]
    text, _, _ = await _run(chunks)
    _check("结构化 content 块被拼接为文本（不是 Python repr）", text == '{"answer": "ok"}', repr(text))


# ---------------------------------------------------------------- 6) 形状不符不打断
async def _test_malformed_chunks() -> None:
    good = AIMessage(content='{"answer": "ok"}', id="m-tail")
    chunks = [
        "裸字符串分片",
        None,
        [1, 2, 3],
        {"model": None},
        {"model": {"messages": "不是列表"}},
        {"model": {"messages": [None, "字符串消息", {"no": "id"}]}},
        {"model": {"messages": [good]}},
    ]
    text, events, _ = await _run(chunks)
    _check("形状不符的分片不抛异常", True)
    _check("形状不符的分片不发进度事件", len(events) == 1, str(events))
    _check("形状不符的分片不影响最终文本", text == '{"answer": "ok"}', repr(text))

    # w 抛异常（连接已断等）不影响执行与结果
    def boom(_evt):
        raise RuntimeError("连接已断")

    text2, _, _ = await _run([{"model": {"messages": [good]}}], sink=boom)
    _check("事件下发失败不影响执行与结果", text2 == '{"answer": "ok"}', repr(text2))

    # 去重：同一条消息出现在多个 update 里只播报一次
    dup = _call_msg("load_table", "dup")
    _, evts_dup, _ = await _run([
        {"model": {"messages": [dup]}},
        {"tools": {"messages": [ToolMessage(content="{}", name="load_table", tool_call_id="x")]}},
        {"model": {"messages": [dup]}},
        {"model": {"messages": [AIMessage(content="{}", id="dup-tail")]}},
    ])
    _check(
        "同 id 的消息只播报一次",
        [e["text"] for e in evts_dup] == ["正在规划分析步骤…", "正在调用 加载数据…", "加载数据 已完成"],
        str([e["text"] for e in evts_dup]),
    )


# ---------------------------------------------------------------- 7~9) 无文本 / 上限
async def _test_no_text_and_cap() -> None:
    try:
        await _run([{"model": {"messages": [_call_msg("load_table", "m1")]}}])
    except RuntimeError as e:
        _check("拿不到 AI 文本 → 抛 RuntimeError（调用方据此回退，不给空答案）", "文本" in str(e), str(e))
    else:
        raise AssertionError("[FAIL] 无 AI 文本时应当抛 RuntimeError")

    many = [{"model": {"messages": [_call_msg("group_cagr", f"m{i}")]}} for i in range(_MAX_PROGRESS + 20)]
    many.append({"model": {"messages": [AIMessage(content='{"answer": []}', id="m-tail")]}})
    text, events, _ = await _run(many)
    _check(
        f"进度条数上限 {_MAX_PROGRESS} 生效",
        len(events) == _MAX_PROGRESS,
        str(len(events)),
    )
    _check("截断进度不影响最终文本", text == '{"answer": []}', repr(text))


# ---------------------------------------------------------------- 10) 纯函数
def _test_pure_helpers() -> None:
    _check("未登记的工具名原样展示（不编造中文名）", _tool_label("brand_new_tool") == "brand_new_tool")
    _check("空工具名兜底", _tool_label("") == "工具")
    _check("group_cagr 有中文标签", _tool_label("group_cagr") == "年均增长率")
    _check(
        "带 tool_calls 的 AI 消息不参与最终文本",
        _ai_text(_call_msg("load_table", "x")) == "",
    )
    _check("ToolMessage 不参与最终文本", _ai_text(ToolMessage(content="x", name="load_table", tool_call_id="y")) == "")
    _check("普通 AI 消息取文本", _ai_text(AIMessage(content="  hi  ")) == "hi")
    _check(
        "tool_calls 为空列表时按普通 AI 消息处理",
        _progress_text(AIMessage(content="hi", tool_calls=[])) == "",
    )
    class _Bare:
        """形状残缺的消息替身：``_progress_text`` 只按属性判分支，不该假设它是 AIMessage。"""

        tool_calls = [{"args": {}, "id": "c1"}]

    _check(
        "工具名缺失的 tool_calls 不播报（不产出「正在调用 工具…」这种无信息量的行）",
        _progress_text(_Bare()) == "",
    )


async def main() -> None:
    await _test_progress_texts()
    await _test_structured_content()
    await _test_malformed_chunks()
    await _test_no_text_and_cap()
    _test_pure_helpers()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
