"""SqliteVectorStore：基于标准库 sqlite3 + numpy 的本地向量存储。

选择它的原因：milvus-lite（MilvusLiteStore 的依赖）不支持 Windows，Qdrant/ES/Mongo
需要外部服务；本知识库为内部规模（<10 万 chunk），向量存 BLOB、检索时 numpy 暴力
余弦相似度即可满足毫秒级响应，且零新增依赖。

表结构：
- collections(collection PK, dimensions)
- chunks(id PK, collection, document_id, content, source, chunk_index, total_chunks, metadata JSON, vector BLOB)

所有阻塞 sqlite 操作通过 asyncio.to_thread 放到线程池执行，避免阻塞事件循环。"""

import asyncio
import json
import sqlite3
import struct
import threading
from typing import Any, Optional

import numpy as np

from agentscope.rag import (
    VectorStoreBase,
    VectorRecord,
    VectorSearchResult,
    DocumentSummary,
    Chunk,
)
from agentscope.message import TextBlock
from app.services.knowledge.search_tokens import (
    TOKENIZER_VERSION,
    document_terms,
    lexemes,
)


def _pack(vector) -> bytes:
    return struct.pack(f"{len(vector)}f", *[float(x) for x in vector])


def _unpack(blob: bytes):
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


class SqliteVectorStore(VectorStoreBase):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._bootstrap()

    def _bootstrap(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    collection TEXT PRIMARY KEY,
                    dimensions INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT,
                    chunk_index INTEGER,
                    total_chunks INTEGER,
                    metadata TEXT,
                    vector BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_coll
                    ON chunks(collection);
                CREATE INDEX IF NOT EXISTS idx_chunks_coll_doc
                    ON chunks(collection, document_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
                    content,
                    collection UNINDEXED,
                    document_id UNINDEXED,
                    source UNINDEXED,
                    chunk_index UNINDEXED,
                    total_chunks UNINDEXED,
                    metadata UNINDEXED
                );
                CREATE TABLE IF NOT EXISTS fts_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()
            # 分词版本标记：版本变化（如 bigram→jieba）才全量重建 FTS，
            # 替代旧版 O(n) 内容比对——启动即 O(1) 判定，重建只发生一次。
            row = self._conn.execute(
                "SELECT value FROM fts_meta WHERE key = 'tokenizer_version'"
            ).fetchone()
            current_version = row[0] if row else None
            counts = self._conn.execute(
                "SELECT (SELECT count(*) FROM chunks), (SELECT count(*) FROM fts_chunks)"
            ).fetchone()
            if counts[0] != counts[1] or current_version != TOKENIZER_VERSION:
                self._conn.execute("DELETE FROM fts_chunks")
                self._conn.execute(
                    "INSERT INTO fts_chunks(rowid, content, collection, document_id, "
                    "source, chunk_index, total_chunks, metadata) "
                    "SELECT id, '', collection, document_id, source, "
                    "chunk_index, total_chunks, metadata FROM chunks"
                )
                # 逐行写入分词结果（空格连接）：FTS5 默认按空白切词，
                # 中文必须预分词才能被 MATCH 命中
                for row_id, content in self._conn.execute(
                    "SELECT id, content FROM chunks"
                ).fetchall():
                    self._conn.execute(
                        "UPDATE fts_chunks SET content = ? WHERE rowid = ?",
                        (document_terms(content or ""), row_id),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO fts_meta (key, value) "
                    "VALUES ('tokenizer_version', ?)",
                    (TOKENIZER_VERSION,),
                )
                self._conn.commit()

    # ---- 内部同步实现（由 to_thread 调度）----
    def _create_collection(self, name: str, dimensions: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO collections (collection, dimensions) VALUES (?, ?)",
                (name, dimensions),
            )
            self._conn.commit()

    def _delete_collection(self, name: str):
        with self._lock:
            self._conn.execute("DELETE FROM collections WHERE collection = ?", (name,))
            self._conn.execute(
                "DELETE FROM fts_chunks WHERE rowid IN "
                "(SELECT id FROM chunks WHERE collection = ?)", (name,)
            )
            self._conn.execute("DELETE FROM chunks WHERE collection = ?", (name,))
            self._conn.commit()

    def _has_collection(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM collections WHERE collection = ?", (name,)
            ).fetchone()
        return row is not None

    def _insert(self, collection: str, records, replace_document_id=None):
        rows = []
        for rec in records:
            chunk = rec.chunk
            rows.append((
                collection,
                rec.document_id,
                (getattr(chunk.content, 'text', None) or (chunk.content if isinstance(chunk.content, str) else str(chunk.content))),
                chunk.source,
                chunk.chunk_index,
                chunk.total_chunks,
                json.dumps(chunk.metadata or {}, ensure_ascii=False),
                _pack(rec.vector),
            ))
        with self._lock:
            try:
                if replace_document_id is not None:
                    params = (collection, str(replace_document_id))
                    self._conn.execute(
                        "DELETE FROM fts_chunks WHERE rowid IN "
                        "(SELECT id FROM chunks WHERE collection=? AND document_id=?)", params,
                    )
                    self._conn.execute("DELETE FROM chunks WHERE collection=? AND document_id=?", params)
                cursor = self._conn.cursor()
                for row in rows:
                    cursor.execute(
                        "INSERT INTO chunks (collection, document_id, content, source, "
                        "chunk_index, total_chunks, metadata, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        row,
                    )
                    # FTS 列存分词结果（空格连接），与 _bootstrap 重建口径一致
                    cursor.execute(
                        "INSERT INTO fts_chunks (rowid, content, collection, document_id, source, "
                        "chunk_index, total_chunks, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (cursor.lastrowid, document_terms(row[2]), row[0], row[1],
                         row[3], row[4], row[5], row[6]),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _delete(self, collection: str, document_id: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM chunks WHERE collection = ? AND document_id = ?",
                (collection, document_id),
            )
            self._conn.execute(
                "DELETE FROM fts_chunks WHERE collection = ? AND document_id = ?",
                (collection, document_id),
            )
            self._conn.commit()

    def _list_chunks(self, collection: str, document_id: str, limit: int, offset: int):
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE collection = ? AND document_id = ?",
                (collection, document_id),
            ).fetchone()[0]
            rows = self._conn.execute(
                "SELECT chunk_index, content FROM chunks "
                "WHERE collection = ? AND document_id = ? "
                "ORDER BY chunk_index ASC LIMIT ? OFFSET ?",
                (collection, document_id, limit, offset),
            ).fetchall()
        return int(total or 0), [
            {"chunk_index": r[0], "content": r[1]} for r in rows
        ]

    @staticmethod
    def _meta_match(meta: dict, flt: Optional[dict]) -> bool:
        if not flt:
            return True
        for k, v in flt.items():
            if meta.get(k) != v:
                return False
        return True

    def _fts_search(self, collection, query_text, top_k, metadata_filter):
        """FTS5 keyword search returning {rowid: bm25_normalized_score}.

        v2：查询侧同样走分词（lexemes 已是 jieba 词级），中文不再被
        isascii 过滤掉；未分词环境自动回退 bigram。
        """
        tokens = lexemes(query_text)[:32]
        if not tokens:
            return {}
        match_q = " OR ".join('"' + token.replace('"', '') + '"' for token in tokens)
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT rowid, bm25(fts_chunks) as bm25_score "
                    "FROM fts_chunks WHERE fts_chunks MATCH ? AND collection = ? "
                    "ORDER BY bm25_score LIMIT ?",
                    (match_q, collection, top_k * 3),
                ).fetchall()
        except Exception:
            return {}
        result = {}
        for rowid, bm25_score in rows:
            result[rowid] = 1.0 / (1.0 + abs(bm25_score))
        return result

    def _search(self, collection: str, query_vector, top_k: int, metadata_filter, query_text=None):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, document_id, content, source, chunk_index, total_chunks, metadata, vector "
                "FROM chunks WHERE collection = ?",
                (collection,),
            ).fetchall()
        if not rows:
            return []
        qv = np.asarray(query_vector, dtype=np.float32)
        qnorm = np.linalg.norm(qv) or 1.0
        # 第一路：向量余弦（全量打分，构成完整排名）
        scored = []  # (row_id, vec_score, bundle)
        for row_id, doc_id, content, source, cidx, tcnt, meta_raw, blob in rows:
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except Exception:
                meta = {}
            if not self._meta_match(meta, metadata_filter):
                continue
            vec = np.asarray(_unpack(blob), dtype=np.float32)
            if vec.shape != qv.shape:
                continue
            norm = np.linalg.norm(vec) or 1.0
            scored.append((
                row_id, float(np.dot(qv, vec) / (qnorm * norm)),
                (doc_id, content, source, cidx, tcnt, meta),
            ))
        if not scored:
            return []
        # 第二路：词面（FTS5 分词命中 + 中文子串兜底），只保留 >0 的候选
        fts_scores = self._fts_search(collection, query_text, top_k, metadata_filter) if query_text else {}
        chinese_terms = [token for token in lexemes(query_text or "")[:32] if not token.isascii()]

        def _lexical(row_id: int, content: str) -> float:
            score = fts_scores.get(row_id, 0.0)
            if chinese_terms:
                score = max(score, sum(term in content for term in chinese_terms) / len(chinese_terms))
            return score

        # RRF 倒数排名融合：score = Σ 60/(60+名次)。两路各自排名，
        # 只看名次不看原始分——对向量/BM25 两种量纲天然免疫。
        rrf: dict[int, float] = {}
        for rank, (row_id, _, _) in enumerate(sorted(scored, key=lambda x: x[1], reverse=True), 1):
            rrf[row_id] = rrf.get(row_id, 0.0) + 60.0 / (60 + rank)
        lexical_ranked = sorted(
            ((row_id, _lexical(row_id, bundle[1])) for row_id, _, bundle in scored
             if _lexical(row_id, bundle[1]) > 0),
            key=lambda x: x[1], reverse=True,
        )
        for rank, (row_id, _) in enumerate(lexical_ranked, 1):
            rrf[row_id] = rrf.get(row_id, 0.0) + 60.0 / (60 + rank)
        bundles = {row_id: bundle for row_id, _, bundle in scored}
        results = [
            VectorSearchResult(
                score=rrf[row_id],
                document_id=bundle[0],
                chunk=Chunk(
                    content=TextBlock(text=bundle[1]),
                    source=bundle[2],
                    chunk_index=bundle[3],
                    total_chunks=bundle[4],
                    metadata=bundle[5],
                ),
            )
            for row_id, bundle in bundles.items()
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def _doc_vectors(self, collection: str):
        """Return {document_id: avg_vector} for all docs in collection."""
        from collections import defaultdict
        with self._lock:
            rows = self._conn.execute(
                "SELECT document_id, vector FROM chunks WHERE collection = ?",
                (collection,),
            ).fetchall()
        if not rows:
            return {}
        vecs = defaultdict(list)
        for doc_id, blob in rows:
            vecs[doc_id].append(np.asarray(_unpack(blob), dtype=np.float32))
        return {doc_id: np.mean(v, axis=0) for doc_id, v in vecs.items()}

    def _list_documents(self, collection: str, metadata_filter):
        with self._lock:
            rows = self._conn.execute(
                "SELECT document_id, source, metadata FROM chunks WHERE collection = ?",
                (collection,),
            ).fetchall()
        agg = {}
        for doc_id, source, meta_raw in rows:
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except Exception:
                meta = {}
            if not self._meta_match(meta, metadata_filter):
                continue
            if doc_id not in agg:
                agg[doc_id] = {"source": source, "count": 0, "metadata": meta}
            agg[doc_id]["count"] += 1
        return [
            DocumentSummary(
                document_id=doc_id,
                source=v["source"],
                chunk_count=v["count"],
                metadata=v["metadata"],
            )
            for doc_id, v in agg.items()
        ]

    # ---- VectorStoreBase 异步接口 ----
    async def create_collection(self, name: str, dimensions: int) -> None:
        await asyncio.to_thread(self._create_collection, name, dimensions)

    async def delete_collection(self, name: str) -> None:
        await asyncio.to_thread(self._delete_collection, name)

    async def has_collection(self, name: str) -> bool:
        return await asyncio.to_thread(self._has_collection, name)

    async def insert(self, collection: str, records) -> None:
        if not records:
            return
        await asyncio.to_thread(self._insert, collection, records)

    async def replace_document(self, collection: str, document_id: str, records) -> None:
        await asyncio.to_thread(self._insert, collection, records, document_id)

    async def delete(self, collection: str, document_id: str) -> None:
        await asyncio.to_thread(self._delete, collection, document_id)

    async def search(self, collection: str, query_vector, top_k: int = 5, metadata_filter=None, query_text=None):
        return await asyncio.to_thread(self._search, collection, query_vector, top_k, metadata_filter, query_text)

    async def list_documents(self, collection: str, metadata_filter=None):
        return await asyncio.to_thread(self._list_documents, collection, metadata_filter)

    async def list_chunks(self, collection: str, document_id: str, limit: int = 50, offset: int = 0):
        """按文档列切片（chunk 预览用），返回 (total, [{chunk_index, content}])"""
        return await asyncio.to_thread(self._list_chunks, collection, document_id, limit, offset)


_store_instance: Optional[SqliteVectorStore] = None


def get_vector_store(db_path: str = None) -> SqliteVectorStore:
    """进程内单例（VectorStoreBase 约定整应用共享一个实例）

    根据 DATABASE_URL 自动选择后端：
    - postgresql -> PgVectorStore（生产，复用 PG + pgvector 扩展）
    - sqlite -> SqliteVectorStore（本地开发，零依赖文件存储）
    """
    global _store_instance
    from app.config import settings

    use_pg = "postgresql" in settings.DATABASE_URL
    if _store_instance is None:
        if use_pg:
            from app.services.pgvector_store import PgVectorStore
            _store_instance = PgVectorStore()
        else:
            if db_path is None:
                db_path = settings.VECTOR_DB_PATH
            _store_instance = SqliteVectorStore(db_path)
    return _store_instance
