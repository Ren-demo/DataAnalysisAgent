"""闲聊上下文治理离线自检：不依赖 LLM / MongoDB / Milvus，纯逻辑级验证。

覆盖三处并发与时序缺陷（P2 · C7 / C16）：

1. ``summary.generate_summary`` 失败必须返回 ``None``。
   早前失败时返回旧摘要，调用方无从分辨，照样推进 ``summary_upto`` ——
   那批旧对话既没被写进摘要、又永远不会再被折叠，等于**静默丢一段对话历史**。
2. ``context_service.build_chat_context`` 的折叠边界必须以**服务端持久化的消息数**为准。
   ``history`` 完全来自客户端请求体，长度可以随便编。
3. ``knowledge_service._file_manifest`` 必须察觉**原地编辑**（文件名与大小都可能不变）。
   早前用目录 mtime 作失效键，改一个已有文件是改不到目录 mtime 的 —— 编辑永远不生效。

运行：``cd backend && python scripts/test_context_governance.py``
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.llm import summary as summary_mod
from app.schemas import ChatMessage
from app.services import context_service
from app.services.knowledge_service import _file_manifest


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    print(f"[ ok ] {name}")


class _FakeLLM:
    """可切换成功/失败的假 LLM。"""

    def __init__(self, content: str | None = "摘要内容", boom: bool = False):
        self._content = content
        self._boom = boom

    async def ainvoke(self, _prompt):
        if self._boom:
            raise RuntimeError("模拟 LLM 不可用")

        class _Resp:
            pass

        r = _Resp()
        r.content = self._content
        return r


def _history(n: int) -> list[ChatMessage]:
    return [ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"m{i}") for i in range(n)]


# --------------------------------------------------------------------------
# 1) generate_summary：失败返回 None，成功返回摘要
# --------------------------------------------------------------------------
async def _test_generate_summary() -> None:
    orig = summary_mod.get_llm
    try:
        summary_mod.get_llm = lambda **_kw: _FakeLLM(boom=True)
        got = await summary_mod.generate_summary("旧摘要", _history(4))
        _check("generate_summary LLM 失败返回 None", got is None, repr(got))

        summary_mod.get_llm = lambda **_kw: _FakeLLM(content="")
        got = await summary_mod.generate_summary("旧摘要", _history(4))
        _check("generate_summary 空输出返回 None（不假装成功）", got is None, repr(got))

        summary_mod.get_llm = lambda **_kw: _FakeLLM(content="新摘要")
        got = await summary_mod.generate_summary("旧摘要", _history(4))
        _check("generate_summary 成功返回新摘要", got == "新摘要", repr(got))

        # 无新增消息：摘要不变（返回值可空，调用方本就不会推进水位）
        got = await summary_mod.generate_summary("旧摘要", [])
        _check("generate_summary 无新增消息时摘要不变", got == "旧摘要", repr(got))
    finally:
        summary_mod.get_llm = orig


# --------------------------------------------------------------------------
# 2) build_chat_context：失败不推进水位；边界受服务端消息数约束
# --------------------------------------------------------------------------
async def _test_build_chat_context() -> None:
    max_msg = context_service.settings.chat_history_max_messages
    saved: list[tuple] = []

    orig_state = context_service.get_summary_state
    orig_save = context_service.save_summary_state
    orig_gen = context_service.generate_summary

    async def _state(_sid):
        return ("旧摘要", 0, len(history))

    async def _save(sid, summary, upto, *, expected_upto=None):
        saved.append((sid, summary, upto, expected_upto))
        return True

    history = _history(max_msg + 4)  # 必然超出窗口，触发折叠

    try:
        context_service.get_summary_state = _state
        context_service.save_summary_state = _save

        # 2a) 生成失败：不写回、不推进
        saved.clear()
        context_service.generate_summary = lambda *_a: _await(None)
        ctx = await context_service.build_chat_context("s1", history)
        _check("摘要失败时保持旧摘要", ctx["summary"] == "旧摘要", repr(ctx["summary"]))
        _check("摘要失败时不写回水位", saved == [], repr(saved))

        # 2b) 生成成功：写回，且带上 CAS 的 expected_upto
        saved.clear()
        context_service.generate_summary = lambda *_a: _await("新摘要")
        ctx = await context_service.build_chat_context("s1", history)
        _check("摘要成功时返回新摘要", ctx["summary"] == "新摘要", repr(ctx["summary"]))
        _check("摘要成功时写回一次", len(saved) == 1, repr(saved))
        _check("写回带 CAS 的 expected_upto=0", saved and saved[0][3] == 0, repr(saved))
        _check(
            "写回水位 = 窗口之前的旧消息条数",
            saved and saved[0][2] == len(history) - max_msg,
            repr(saved),
        )

        # 2c) 客户端伪造超长 history：折叠边界受服务端持久化条数约束
        forged = _history(max_msg * 10)  # 客户端声称有 10 倍历史

        async def _state_short(_sid):
            return ("旧摘要", 0, max_msg + 2)  # 服务端实际只有这么点

        context_service.get_summary_state = _state_short
        saved.clear()
        ctx = await context_service.build_chat_context("s1", forged)
        _check("伪造 history 时折叠边界被服务端条数截断", saved and saved[0][2] == 2, repr(saved))
        _check("伪造 history 时近期窗口仍取窗口内条数", len(ctx["history"]) == max_msg, str(len(ctx["history"])))

        # 2d) 未超窗口：完全不碰摘要
        context_service.get_summary_state = _state
        saved.clear()
        ctx = await context_service.build_chat_context("s1", _history(3))
        _check("未超窗口时摘要为 None", ctx["summary"] is None, repr(ctx["summary"]))
        _check("未超窗口时不写回", saved == [], repr(saved))
    finally:
        context_service.get_summary_state = orig_state
        context_service.save_summary_state = orig_save
        context_service.generate_summary = orig_gen


async def _await(value):
    """把常量包成 awaitable，方便替换 async 函数。"""
    return value


# --------------------------------------------------------------------------
# 3) 知识库文件清单：原地编辑可被察觉（C7）
# --------------------------------------------------------------------------
def _test_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = root / "a.md"
        a.write_text("第一版内容", encoding="utf-8")
        m1 = _file_manifest(root)
        _check("清单含一个文件", len(m1) == 1 and m1[0][0] == "a.md", repr(m1))

        # 原地编辑：**保持字节长度不变**，只有内容与 mtime 变
        time.sleep(0.01)
        a.write_text("第二版内容", encoding="utf-8")
        os.utime(a, (time.time() + 5, time.time() + 5))  # 确保 mtime 前进（文件系统粒度）
        m2 = _file_manifest(root)
        _check("原地编辑（同长度）后清单变化", m1 != m2, f"{m1} vs {m2}")

        # 删文件也要能察觉
        a.unlink()
        m3 = _file_manifest(root)
        _check("删文件后清单变化", m3 != m2 and m3 == (), repr(m3))

        # 非知识库后缀不参与
        (root / "b.pdf").write_text("x", encoding="utf-8")
        (root / "c.txt").write_text("y", encoding="utf-8")
        m4 = _file_manifest(root)
        _check("只统计 .md/.txt", [i[0] for i in m4] == ["c.txt"], repr(m4))


def main() -> None:
    asyncio.run(_test_generate_summary())
    asyncio.run(_test_build_chat_context())
    _test_manifest()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
