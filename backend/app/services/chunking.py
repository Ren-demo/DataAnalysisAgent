"""文本分块工具：结构感知分块（段落/句子边界优先），字符滑动窗口兜底，供文档/知识库写入 Milvus 前使用。"""
import re

# 句末标点（中文为主，含英文 !?;）后切句；不含英文句号「.」，避免误伤小数点/缩写
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*")
# 段落按空行切分
_PARA_SPLIT = re.compile(r"\n\s*\n")


def _char_chunks(text: str, size: int, overlap: int) -> list[str]:
    """字符滑动窗口兜底：用于无句末标点的文本或超长句。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text) - overlap, step) if text[i : i + size].strip()]


def _split_sentences(text: str) -> list[str]:
    """按段落 + 句末标点切句，返回非空句子列表（每句以句末标点结尾、不含尾部空白）。"""
    sentences: list[str] = []
    for para in _PARA_SPLIT.split(text):
        para = para.strip()
        if not para:
            continue
        sentences.extend(s.strip() for s in _SENT_SPLIT.split(para) if s.strip())
    return sentences


def _join_len(sentences: list[str]) -> int:
    """句子列表拼接为 chunk 后的长度（句子间以空格分隔）。"""
    return sum(len(s) + 1 for s in sentences) - 1 if sentences else 0


def _take_tail(sentences: list[str], limit: int) -> list[str]:
    """从句子列表末尾往回取，累计长度不超过 limit（用于 overlap 回退，以句子为粒度）。"""
    out: list[str] = []
    total = 0
    for s in reversed(sentences):
        add = len(s) + (1 if total else 0)
        if total + add > limit:
            break
        out.append(s)
        total += add
    return list(reversed(out))


def chunk_text(text: str, size: int = 500, overlap: int = 100) -> list[str]:
    """把文本切分为结构化分块：段落/句子边界优先，字符滑动窗口兜底。

    - 短于 size 的文本整体作为一个块返回。
    - 有句末标点时，以完整句子为最小单位贪心合并到 size，不切断句子；
      封口时按 overlap 回退上一块末尾的完整句子，保持相邻块之间的语义连续性。
    - 无句末标点（或仅一句）的文本，或单句自身超长时，回退字符滑动窗口。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    sentences = _split_sentences(text)
    # 无句末标点（或仅一句）的文本 → 字符窗口兜底
    if len(sentences) <= 1:
        return _char_chunks(text, size, overlap)

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        # 单句自身超长 → 先封口当前块，再对该句做字符切分，不参与句子合并
        if len(s) > size:
            if cur:
                chunks.append(" ".join(cur))
                cur, cur_len = [], 0
            chunks.extend(_char_chunks(s, size, overlap))
            continue
        add = len(s) + (1 if cur else 0)
        if cur_len + add <= size:
            cur.append(s)
            cur_len += add
        else:
            chunks.append(" ".join(cur))
            # overlap 回退：新块以上一块末尾 ≤ overlap 的完整句子开头
            cur = _take_tail(cur, overlap) + [s]
            cur_len = _join_len(cur)
    if cur:
        chunks.append(" ".join(cur))
    return chunks
