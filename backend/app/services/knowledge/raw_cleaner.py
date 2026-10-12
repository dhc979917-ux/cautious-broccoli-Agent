"""文档原文规则清洗器（不使用 LLM）。

PDF/Office 提取出的原始 markdown 在入向量库前先过这里：去掉重复页眉/页脚、
页码、作者/单位/邮箱、版权 DOI 行、致谢、参考文献、附录、图表题注等检索噪声。
规则保守：拿不准的一律保留（宁可少删，不可误删正文）。
"""
import re
from collections import Counter
from typing import List

_PAGE_NUM_RE = re.compile(
    r'^\s*(?:第?\s*\d+\s*页(?:\s*[/／,，]\s*(?:共\s*)?\d+\s*页)?|pages?\s*\d+(?:\s*[-–]\s*\d+)?|[-–—]\s*\d{1,4}\s*[-–—]?)\s*$',
    re.I,
)
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_DOI_RE = re.compile(r'\b(?:DOI|doi)\s*[:：]?\s*10\.\d{4,9}/\S+')
_COPYRIGHT_RE = re.compile(
    r'(?:©|copyright|\(c\)|版权所有|all rights reserved|\bISSN\b|\bISBN\b|收稿日期|修回日期|基金项目)',
    re.I,
)
# 作者行启发：短行 + 含典型作者/单位特征（邮箱、数字上标、机构关键词）
_AFFIL_RE = re.compile(
    r'(大学|学院|研究所|研究院|实验室|中心医院|医院|公司|集团|'
    r'university|college|institute|laborator|hospital|school of|department of)',
    re.I,
)
_SECTION_DROP_RES = [
    re.compile(r'^#{0,6}\s*(acknowledg(?:e)?ments?|致\s*谢)\s*[：:]?\s*$', re.I),
    re.compile(r'^#{0,6}\s*(references?|bibliography|参\s*考\s*文\s*献)\s*[：:]?\s*$', re.I),
    re.compile(r'^#{0,6}\s*(appendix(?:\s+[a-z])?|附\s*录\s*[a-zA-Z一二三四五六七八九十]?)\s*[：:]?\s*$', re.I),
]
_FIG_CAP_RE = re.compile(r'^\s*(?:fig(?:ure)?\.?\s*\d|table\s*\d|图\s*\d|表\s*\d)[.:：、]?\s*\S', re.I)
_HEADING_RE = re.compile(r'^#{1,6}\s')
_REF_ENTRY_RE = re.compile(r'^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.、])\s')


def _norm(line: str) -> str:
    return re.sub(r'\s+', ' ', line.strip().lower())


def _detect_header_footer(lines: List[str]) -> set:
    """跨页重复的短行视为页眉/页脚（出现 >=3 次且占比明显）。"""
    counts = Counter(_norm(l) for l in lines if l.strip())
    total = max(len(lines), 1)
    out = set()
    for normed, cnt in counts.items():
        if cnt >= 3 and cnt >= total * 0.01 and len(normed) < 50:
            out.add(normed)
    return out


def clean_extracted_text(text: str) -> str:
    """清洗提取后的文档原文，返回可直接切块入库的文本。"""
    if not text or not text.strip():
        return ""
    lines = text.splitlines()
    hf_set = _detect_header_footer(lines)

    out: List[str] = []
    dropping = False      # 处于待丢弃章节（致谢/参考文献/附录）内
    drop_level = 0
    in_front_block = True  # 文档头部区域（标题/作者/单位）
    front_line_count = 0

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        front_line_count += 1
        if front_line_count > 40:
            in_front_block = False

        # 章节级丢弃：命中 致谢/参考文献/附录 标题后，丢到下一个同级标题为止
        if stripped:
            m_lvl = _HEADING_RE.match(stripped)
            lvl = len(m_lvl.group(0).split()[0]) if m_lvl else 0
            if dropping:
                if lvl and lvl <= drop_level:
                    # 上一丢弃章节结束；当前行可能紧接着是下一个待丢章节标题，
                    # 继续走下面的命中判断，不能直接放行。
                    dropping = False
                else:
                    continue
            if not dropping:
                hit = any(p.match(stripped) for p in _SECTION_DROP_RES)
                if hit:
                    dropping = True
                    drop_level = lvl or 1
                    continue

        if not stripped:
            out.append("")
            continue

        normed = _norm(stripped)
        if normed in hf_set and not _HEADING_RE.match(stripped):
            continue  # 重复页眉/页脚
        if _PAGE_NUM_RE.match(stripped):
            continue  # 页码
        if _COPYRIGHT_RE.search(stripped) and len(stripped) < 200:
            continue  # 版权/DOI/基金等元信息行
        if _DOI_RE.search(stripped) and len(stripped) < 120:
            continue
        if _FIG_CAP_RE.match(stripped) and len(stripped) < 200:
            continue  # 图表题注

        # 文档头部：作者/单位/邮箱块（保守：仅前 40 行内的短行）
        if in_front_block and len(stripped) < 100:
            if not _HEADING_RE.match(stripped):
                # 含邮箱/单位特征的短行视为作者块。作者块常出现在首个标题之前，
                # 因此不要求先见到标题；但排除以句末标点结尾的行（那是正文句子）。
                is_meta = _EMAIL_RE.search(stripped) or _AFFIL_RE.search(stripped)
                ends_sentence = bool(re.search(r'[。！？.!?]$', stripped))
                if is_meta and not ends_sentence:
                    continue

        out.append(line)

    # 兜底：参考文献条目常以 [1] 形式出现在文末，即使标题未命中也清落成段的条目行
    tail_ref_streak = 0
    for i in range(len(out) - 1, max(len(out) - 200, -1), -1):
        s = out[i].strip()
        if not s:
            continue
        if _REF_ENTRY_RE.match(s) and len(s) < 400:
            tail_ref_streak += 1
        else:
            break
    if tail_ref_streak >= 3:
        kept = out[: len(out) - 0]
        removed = 0
        for i in range(len(out) - 1, -1, -1):
            s = out[i].strip()
            if not s:
                continue
            if _REF_ENTRY_RE.match(s) and len(s) < 400:
                out[i] = ""
                removed += 1
                if removed >= tail_ref_streak:
                    break
            else:
                break
        del kept

    cleaned = "\n".join(out)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned
