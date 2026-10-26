"""``chunk_text`` 离线自检：不依赖 embedding / Milvus，直接验证分块行为。

运行：``cd backend && python scripts/test_chunking.py``
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.services.chunking import chunk_text


def _check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"[FAIL] {name}")
    print(f"[PASS] {name}")


def test_blank() -> None:
    _check("空白文本返回空列表", chunk_text("") == [] and chunk_text("   ") == [])


def test_short_text_single_chunk() -> None:
    chunks = chunk_text("一。二。三。", size=100, overlap=10)
    _check("短文本整体作为单块返回", len(chunks) == 1 and chunks[0] == "一。二。三。")


def test_sentences_not_split_midway() -> None:
    text = "第一句话。第二句话。第三句话。第四句话。第五句话。"
    # 每句 5 字，size=11 恰装 2 句
    chunks = chunk_text(text, size=11, overlap=0)
    assert chunks, "应产生至少一个块"
    for c in chunks:
        assert c.endswith("。"), f"chunk 未以完整句子结尾: {c!r}"
    _check("句子不被从中切断（每块以句末标点结尾）", True)


def test_paragraphs_split() -> None:
    text = "第一段第一句。第一段第二句。\n\n第二段第一句。第二段第二句。"
    chunks = chunk_text(text, size=1000, overlap=10)
    _check("带空行段落文本正常处理", len(chunks) == 1 and text.count("。") == 4)


def test_unpunctated_long_text_falls_back() -> None:
    chunks = chunk_text("x" * 300, size=100, overlap=20)
    _check(
        "无标点长文本回退字符窗口",
        len(chunks) > 1 and all(len(c) <= 100 for c in chunks),
    )


def test_oversized_sentence_split() -> None:
    short = "短句。"
    long = "无标点长句" * 30  # 150 字，无句末标点，单句超长
    chunks = chunk_text(short + long + short, size=50, overlap=10)
    _check("单句超长被切分（所有块 ≤ size）", all(len(c) <= 50 for c in chunks))


def test_overlap_shares_tail() -> None:
    text = "句一。句二。句三。句四。句五。句六。"
    # 每句 3 字，size=7 装 2 句，overlap=3 回退 1 句
    chunks = chunk_text(text, size=7, overlap=3)
    overlap_found = any(
        chunks[i + 1].startswith(chunks[i].split()[-1])
        for i in range(len(chunks) - 1)
    )
    _check("overlap 生效（相邻块共享尾部句子）", overlap_found)


def test_returns_list_of_str() -> None:
    chunks = chunk_text("a。b。c。d。", size=3, overlap=0)
    _check(
        "返回 list[str]（签名兼容）",
        isinstance(chunks, list) and all(isinstance(c, str) for c in chunks),
    )


def main() -> None:
    tests = [
        f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项自检通过")


if __name__ == "__main__":
    main()
