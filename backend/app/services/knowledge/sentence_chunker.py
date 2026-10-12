"""优先级边界文本切分器（用于向量化入库）。

切分时按边界优先级逐级降级，尽量在自然语义边界处断开：
  1. 段落（双换行）
  2. 句子（。！？.!?\n 等句末标点）
 3. 分句（；; 分号）
 4. 子句（，,、 逗号/顿号）
 5. 词（空格）
 6. 硬切（兜底，避免单句超长无法入库）

每个块携带 overlap_sentences 个句子的重叠，保证跨块上下文连续。
"""
import re
from typing import List

MAX_CHARS = 1800
MIN_CHARS = 200
OVERLAP_SENTENCES = 2

# 句子边界：中英文句末标点（其后可能有空白）
_SENT_END = re.compile(r'(?<=[。．.!?！？\n])\s*')
# 分句边界：分号
_CLAUSE_SEMI = re.compile(r'(?<=[；;])\s*')
# 子句边界：逗号/顿号
_CLAUSE_COMMA = re.compile(r'(?<=[，,、])\s*')
# 词边界：空格
_WORD = re.compile(r'(?<=\s)')


def _split_by(text: str, pattern: re.Pattern) -> List[str]:
    return [p.strip() for p in pattern.split(text) if p and p.strip()]


def _split_to_units(text: str, max_chars: int) -> List[str]:
    """把一段文本拆成 ≤ max_chars 的单元，按边界优先级逐级降级。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 依次尝试更细粒度的边界
    for pattern in (_SENT_END, _CLAUSE_SEMI, _CLAUSE_COMMA, _WORD):
        parts = _split_by(text, pattern)
        if len(parts) > 1:
            units: List[str] = []
            for part in parts:
                if len(part) <= max_chars:
                    units.append(part)
                else:
                    # 单个 part 仍超长，递归用更细边界拆
                    units.extend(_split_to_units(part, max_chars))
            return units

    # 没有任何边界可拆（超长无标点/无空格），硬切兜底
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _merge_units(units: List[str], max_chars: int, min_chars: int) -> List[List[str]]:
    """把细粒度单元贪心合并成块：每块尽量接近 max_chars 但不超过。"""
    groups: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0
    for u in units:
        add_len = len(u) + (1 if cur else 0)  # +1 for join space
        if cur and cur_len + add_len > max_chars:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += add_len
    if cur:
        groups.append(cur)
    return groups


def _sentences_of(units: List[str]) -> List[str]:
    """把块内单元按句子边界归并，用于计算 overlap（以句为单位）。"""
    return [u for u in units if u]


def chunk_text(text: str, max_chars: int = MAX_CHARS,
               overlap_sentences: int = OVERLAP_SENTENCES) -> List[str]:
    """按优先级边界切分文本为重叠的块。"""
    text = (text or "").strip()
    if not text:
        return []

    # 1) 先按段落（双换行）拆
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

    # 2) 每段内部按优先级拆成细粒度单元，再合并成块
    all_units: List[str] = []
    for para in paragraphs:
        all_units.extend(_split_to_units(para, max_chars))

    if not all_units:
        return []

    groups = _merge_units(all_units, max_chars, MIN_CHARS)

    # 3) 生成带 overlap 的块文本
    chunks: List[str] = []
    for i, group in enumerate(groups):
        if i > 0 and overlap_sentences > 0:
            prev_units = _sentences_of(groups[i - 1])
            ov_units = prev_units[-overlap_sentences:]
            block = ov_units + group
        else:
            block = group
        chunk = ' '.join(block)
        # overlap 可能把块撑得比 max_chars 大，容忍（重叠是上下文需要）
        chunks.append(chunk)

    # 兜底：空结果但原文非空时硬切
    if not chunks and text:
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    return chunks
