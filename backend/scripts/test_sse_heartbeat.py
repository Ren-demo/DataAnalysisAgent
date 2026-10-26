"""SSE 保活 / 事件搬运自检（替身驱动，不连图、不连 LLM、不连 Mongo）。

背景：``_chat_stream_response`` 早前是一句 ``async for event in graph.astream(...)``，
图内节点（分析 / 决策报告 agent）连续跑十几轮 LLM 往返且**中途一个事件都不下发**，
前端空闲超时（600s）把这类健康的长任务单方面掐断。现在图跑在独立任务里、事件经队列搬运、
生成器按 ``sse_heartbeat_seconds`` 超时取，静默期间下发 ``: keepalive`` 注释行保活。

本脚本用桩图替换 ``chat.orchestrator_graph``、用记录器替换 ``record_turn`` / ``attach_report``，
断言：
1. 静默期间按心跳间隔下发 ``: keepalive``（注释行**必须带空行**，否则前端切块不 flush）；
2. 真实事件按原顺序透传，且 ``result`` 先于 ``report``；
3. 副作用的**次序**仍是「result 时 record_turn → report 时 attach_report」（不能搬进图任务里乱序）；
4. 桩图抛出的 AppError / ValueError / SQLAlchemyError / 其它异常映射出的 error_code
   与改造前一致（这是前端「修复连接」入口的契约）；
5. ``result`` 带 error 时**不**持久化（既有行为）；
6. 生成器被提前关闭（客户端断开 / 点「停止」）时，桩图任务确实被 cancel —— 否则
   agent 会在后台继续烧 LLM 直到耗尽步数上限。

运行：``cd backend && python scripts/test_sse_heartbeat.py``
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy.exc import OperationalError

from app.errors import AppError
from app.routers import chat
from app.schemas import ChatRequest

KEEPALIVE = ": keepalive\n\n"
# 心跳间隔压到毫秒级：测试等 2 个心跳即可，不必真等 20 秒
chat._HEARTBEAT_SECONDS = 0.05


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


def _events(chunks: list[str]) -> list[dict]:
    """把 SSE 文本块解析回事件 dict（只取 data: 行，忽略注释行）。"""
    out = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


class _StubGraph:
    """替身编排图：按 ``plan`` 顺序产出事件。

    plan 元素为 dict 时产出事件、为 Exception 时抛出、为 asyncio.Event 时等待它被 set
    （用它做「先让消费侧收够心跳再放行」的同步点，避免用 sleep 计时，消除抖动）。
    """

    def __init__(self, plan: list) -> None:
        self.plan = plan
        self.cancelled = False
        self.closed = False

    async def astream(self, initial, stream_mode="custom"):
        try:
            for step in self.plan:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                elif isinstance(step, Exception):
                    raise step
                else:
                    yield step
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed = True


class _Recorder:
    """记录 record_turn / attach_report 的调用次序与参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def record_turn(self, session_id, question, result, data_source_id):
        self.calls.append(("record_turn", session_id, question, bool(result)))
        return "msg-1"

    async def attach_report(self, session_id, message_id, report):
        self.calls.append(("attach_report", session_id, message_id, report))
        return True


def _install(stub: _StubGraph, rec: _Recorder) -> None:
    chat.orchestrator_graph = stub
    chat.record_turn = rec.record_turn
    chat.attach_report = rec.attach_report


def _request(session_id: str | None = "s1") -> ChatRequest:
    return ChatRequest(question="哪些地区满足条件", data_source_id="doc:x", session_id=session_id)


# ---------------------------------------------------------------- 1) 心跳 + 次序
async def _test_heartbeat_and_order() -> None:
    gate = asyncio.Event()
    stub = _StubGraph([
        gate,
        {"type": "result", "data": {"error": None, "answer": "ok"}},
        {"type": "report", "data": {"title": "报告"}},
    ])
    rec = _Recorder()
    _install(stub, rec)

    agen = chat._chat_stream_response(_request())
    chunks: list[str] = []
    # 先收够 2 条心跳再放行图；等到就说明「静默期间确实在保活」
    while sum(1 for c in chunks if c == KEEPALIVE) < 2:
        chunks.append(await agen.__anext__())
    _check("图静默期间按下发心跳（: keepalive）", chunks.count(KEEPALIVE) >= 2, str(len(chunks)))
    _check("心跳行**带空行**（否则前端切块不会 flush）", all(c.endswith("\n\n") for c in chunks))

    gate.set()
    async for chunk in agen:
        chunks.append(chunk)

    evts = _events(chunks)
    _check(
        "真实事件按原顺序透传（result → report）",
        [e["type"] for e in evts] == ["result", "report"],
        str([e["type"] for e in evts]),
    )
    _check(
        "副作用次序仍是 record_turn → attach_report",
        [c[0] for c in rec.calls] == ["record_turn", "attach_report"],
        str(rec.calls),
    )
    _check(
        "attach_report 拿到的是 record_turn 返回的 message_id",
        rec.calls[1][2] == "msg-1",
        str(rec.calls),
    )
    _check("图任务跑完后桩图正常关闭", stub.closed is True and stub.cancelled is False)


# ---------------------------------------------------------------- 2) 错误映射
async def _test_error_mapping() -> None:
    cases = [
        (AppError("没有配置 API Key", "NO_API_KEY"), "NO_API_KEY"),
        (ValueError("未知数据源 upload:不存在"), "DATASOURCE_UNAVAILABLE"),
        (OperationalError("SELECT 1", {}, Exception("boom")), "DATASOURCE_UNAVAILABLE"),
        (RuntimeError("代码缺陷"), "SYSTEM_ERROR"),
    ]
    for exc, expect in cases:
        stub = _StubGraph([exc])
        rec = _Recorder()
        _install(stub, rec)

        chunks = [c async for c in chat._chat_stream_response(_request())]
        evts = _events(chunks)
        _check(
            f"{type(exc).__name__} → error_code={expect}",
            len(evts) == 1
            and evts[0]["type"] == "error"
            and evts[0]["error_code"] == expect,
            str(evts),
        )
        _check(
            f"{type(exc).__name__} 时不持久化会话",
            rec.calls == [],
            str(rec.calls),
        )

    # AppError 的面向用户文案要原样透出（不是通用兜底文案）
    stub = _StubGraph([AppError("数据源已失效，请重新选择", "DATASOURCE_UNAVAILABLE")])
    _install(stub, _Recorder())
    evts = _events([c async for c in chat._chat_stream_response(_request())])
    _check("AppError 的用户文案原样透出", evts[0]["message"] == "数据源已失效，请重新选择", str(evts))


# ---------------------------------------------------------------- 3) error 结果不落库
async def _test_error_result_not_persisted() -> None:
    stub = _StubGraph([{"type": "result", "data": {"error": "查询失败", "error_code": "SQL_ERROR"}}])
    rec = _Recorder()
    _install(stub, rec)
    evts = _events([c async for c in chat._chat_stream_response(_request())])
    _check("result 带 error 时事件照常下发", evts[0]["data"].get("error") == "查询失败", str(evts))
    _check("result 带 error 时不写会话记忆", rec.calls == [], str(rec.calls))

    # 无 session_id 时同样不落库
    stub2 = _StubGraph([{"type": "result", "data": {"error": None}}])
    rec2 = _Recorder()
    _install(stub2, rec2)
    [c async for c in chat._chat_stream_response(_request(session_id=None))]
    _check("无 session_id 时不写会话记忆", rec2.calls == [], str(rec2.calls))


# ---------------------------------------------------------------- 4) 提前关闭 → 取消图任务
async def _test_cancel_on_close() -> None:
    gate = asyncio.Event()  # 永不 set：桩图一直挂着，模拟跑不完的长任务
    stub = _StubGraph([{"type": "result", "data": {"error": None}}, gate, {"type": "report", "data": {}}])
    rec = _Recorder()
    _install(stub, rec)

    agen = chat._chat_stream_response(_request())
    first = await agen.__anext__()  # 拿到 result 即停（等价于前端点「停止」后断开连接）
    _check("提前关闭前已收到 result 事件", '"type": "result"' in first, first)
    await agen.aclose()
    # 让被取消的任务把 CancelledError 传播完
    await asyncio.sleep(0)
    _check(
        "生成器关闭即取消图任务（否则 agent 会在后台继续烧 LLM）",
        stub.cancelled is True,
        f"cancelled={stub.cancelled}",
    )
    _check("图任务被取消后仍完成清理", stub.closed is True)


async def main() -> None:
    await _test_heartbeat_and_order()
    await _test_error_mapping()
    await _test_error_result_not_persisted()
    await _test_cancel_on_close()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
