"""表格感知切块器（检索芯 v2.6，借鉴 RAGFlow 模板化切块思想）。

问题（2026-10-16 实测）：sentence_chunker 按句子边界切块，设备手册
里的参数表（"参数 | 值 | 单位" 逐行）被腰斩——"拉伸强度 22 MPa"
跨块断裂且相邻块重复携带，检索命中"拉伸强度"时拿不到完整行。

方案：在 sentence_chunker 之上加一层表格保护——
1. 表格识别：Markdown 表格（| 分隔行）、CSV 风格（Tab/多空格分隔
   的等宽数据行）、source_to_md 生成的"cell | cell"形态都认；
2. 不拆家：同一张表的所有行必须落在同一个块里（表格原子性）；
3. 超长表按行边界切：每块以表头行开头（重复表头），行完整性保留；
4. 面包屑：块文本前附标题路径（# 技术参数 > ## TPU-5000），
   检索命中即知"这段出自哪一节"；
5. 非表格文本：原样交给 sentence_chunker（行为完全不变）。

用法（与 chunk_text 同签名，可作 drop-in）：
    from app.services.knowledge.table_aware_chunker import chunk_text
    chunks = chunk_text(text, max_chars=1800)
"""

import re
from typing import List, Optional

from app.services.knowledge.sentence_chunker import (
    chunk_text as _sentence_chunk,
    MAX_CHARS as _DEFAULT_MAX,
)

# 表格行判定：| 分隔 或 Tab/连续空格分隔且 >= 2 个分隔符
_MD_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_MD_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")  # |---|---| 分隔行
_PLAIN_SEP = re.compile(r"\t|\s{2,}")


def _is_table_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _MD_TABLE.match(s):
        return True
    # CSV/多空格：至少 2 个分隔且含非空白内容
    if "\t" in s:
        parts = [p for p in s.split("\t") if p.strip()]
        return len(parts) >= 2
    if _PLAIN_SEP.search(s) and not s.startswith(("#", "-", "*", ">", "1.", "2.", "3.")):
        parts = [p for p in _PLAIN_SEP.split(s) if p.strip()]
        return len(parts) >= 3 and sum(len(p) for p in parts) >= 8
    return False


def _is_sep_line(line: str) -> bool:
    return bool(_MD_SEP.match(line.strip())) and "-" in line


def _is_heading(line: str) -> Optional[int]:
    m = re.match(r"^(#{1,6})\s+(.*)", line.strip())
    return (len(m.group(1)), m.group(2).strip()) if m else None


def _split_table_block(lines: List[str], max_chars: int) -> List[str]:
    """超长表格按行边界切块，每块重复表头。"""
    header = lines[0] if lines and not _is_sep_line(lines[0]) else None
    sep_idx = 1 if (header and len(lines) > 1 and _is_sep_line(lines[1])) else None
    prefix = [header] + ([lines[sep_idx]] if sep_idx is not None else [])
    prefix_len = sum(len(l) + 1 for l in prefix)
    blocks, cur, cur_len = [], list(prefix), prefix_len
    for line in lines[len(prefix):]:
        add = len(line) + 1
        if cur_len + add > max_chars and len(cur) > len(prefix):
            blocks.append("\n".join(cur))
            cur, cur_len = list(prefix), prefix_len
        cur.append(line)
        cur_len += add
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _classify(text: str) -> List[dict]:
    """把文本切成连续段落，每段标记类型与内容行。

    返回 [{"type": "table"|"text"|"heading", "lines": [...], "level": n}]
    """
    out = []
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        h = _is_heading(line)
        if h:
            out.append({"type": "heading", "lines": [line], "level": h[0], "title": h[1]})
            i += 1
            continue
        if _is_table_line(line) and not _is_sep_line(line):
            j = i
            # 表头行后允许跟分隔行
            if j + 1 < n and _is_sep_line(lines[j + 1]):
                j += 1
            while j + 1 < n and (_is_table_line(lines[j + 1]) or _is_sep_line(lines[j + 1])):
                j += 1
            out.append({"type": "table", "lines": lines[i:j + 1]})
            i = j + 1
            continue
        j = i
        while j < n and not _is_heading(lines[j]) and not (_is_table_line(lines[j]) and not _is_sep_line(lines[j])):
            j += 1
        out.append({"type": "text", "lines": lines[i:j]})
        i = j
    return [seg for seg in out if seg["lines"]]


def chunk_text(text: str, max_chars: int = _DEFAULT_MAX,
               overlap_sentences: int = 2) -> List[str]:
    """表格感知切块：表格原子性 + 超长表行边界切 + 文本部分走句子切块。

    与 sentence_chunker.chunk_text 同签名；无表格输入时行为与原版
    完全一致（文本段原样转发）。
    """
    text = (text or "").strip()
    if not text:
        return []
    segments = _classify(text)
    if not any(s["type"] == "table" for s in segments):
        return _sentence_chunk(text, max_chars=max_chars,
                               overlap_sentences=overlap_sentences)

    chunks: List[str] = []
    breadcrumb: List[str] = []          # 最近标题链（面包屑）
    text_buf: List[str] = []            # 待处理的连续文本行

    def _flush_text():
        if not text_buf:
            return
        body = "\n".join(text_buf).strip()
        text_buf.clear()
        if not body:
            return
        prefix = ""
        if breadcrumb:
            prefix = "# " + " > ".join(breadcrumb) + "\n"
        for piece in _sentence_chunk(body, max_chars=max_chars,
                                     overlap_sentences=overlap_sentences):
            chunks.append((prefix + piece).strip())

    for seg in segments:
        if seg["type"] == "heading":
            _flush_text()
            # 面包屑：按层级截断重建
            breadcrumb = breadcrumb[:seg["level"] - 1]
            breadcrumb.append(seg["title"])
            continue
        if seg["type"] == "text":
            text_buf.extend(seg["lines"])
            continue
        # table：先冲刷缓冲文本，保证表格独立成块
        _flush_text()
        table_lines = seg["lines"]
        prefix = ""
        if breadcrumb:
            prefix = "# " + " > ".join(breadcrumb) + "\n"
        budget = max_chars - len(prefix) - 8
        blocks = _split_table_block(table_lines, budget) if len("\n".join(table_lines)) > budget else ["\n".join(table_lines)]
        for b in blocks:
            chunks.append((prefix + b).strip())
    _flush_text()
    return [c for c in chunks if c.strip()]
