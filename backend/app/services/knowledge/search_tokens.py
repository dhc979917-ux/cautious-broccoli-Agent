"""Shared lexemes for Chinese and Latin full-text lookup.

v2（检索芯强化）：优先使用 jieba 分词（真词级检索），未安装 jieba 时自动回退到
旧版二元组（bigram）方案，接口签名不变——两套向量库（SQLite FTS5 / PG tsvector）
都从这里取词，升级只发生在一处。

注意：分词版本变化会影响索引侧的存储格式。SQLite 侧由 SqliteVectorStore 用
fts_meta 表的 tokenizer_version 标记自动重建；PG 侧新写入立即生效，存量数据
需手动 reindex 才能享受新分词。
"""

import re

try:  # jieba 可选依赖：缺席时回退 bigram，功能不缺失只是中文命中率降低
    import jieba

    jieba.setLogLevel(60)  # 关闭初始化日志
    _HAS_JIEBA = True
except Exception:  # pragma: no cover - 环境未装 jieba
    _HAS_JIEBA = False

TOKENIZER_VERSION = "2-jieba" if _HAS_JIEBA else "1-bigram"

_HAN = re.compile(r"[\u3400-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_]+")
_TOKEN_OK = re.compile(r"[\w\u3400-\u9fff]")


def _segments(text: str) -> list[str]:
    """jieba 切词（search 模式，兼顾长词与子词）。"""
    jieba.initialize()
    return [t for t in jieba.cut_for_search(text or "") if t.strip()]


def _bigrams(text: str) -> list[str]:
    """旧方案：中文滑窗二元组 + 单字兜底。"""
    out = []
    for match in _HAN.finditer(text or ""):
        value = match.group()
        out.extend(value[i:i + 2] for i in range(len(value) - 1))
        if len(value) == 1:
            out.append(value)
    return out


def lexemes(text: str) -> list[str]:
    """词条化（去重保序）。jieba 词级；回退 bigram。拉丁词统一小写。"""
    result: list[str] = []
    if _HAS_JIEBA:
        for token in _segments(text):
            token = token.strip()
            if token and _TOKEN_OK.search(token):
                result.append(token.lower())
    else:
        result.extend(_bigrams(text))
    result.extend(match.group().lower() for match in _LATIN.finditer(text or ""))
    return list(dict.fromkeys(result))


def document_terms(text: str) -> str:
    """索引侧：空格连接的词条串（喂给 FTS5 content / PG to_tsvector('simple')）。"""
    return " ".join(lexemes(text)[:4096])


def query_terms(text: str) -> str:
    """查询侧：OR 连接的词条串（PG to_tsquery / SQLite MATCH）。"""
    return " | ".join(lexemes(text)[:32])
