"""PgVectorStore: PostgreSQL + pgvector extension vector store.

Production backend. Reuses DATABASE_URL PG connection, stores vectors in
the same database as business data -- unified backup/ops.

Tables (idempotent auto-create):
- vector_collections(collection TEXT PK, dimensions INT)
- vector_chunks(id BIGSERIAL PK, collection, document_id, content, source,
  chunk_index, total_chunks, metadata JSONB, embedding vector(1024), tsv tsvector)
- HNSW index (cosine) + GIN index (tsvector full-text search)

All blocking SQL via asyncio.to_thread; thread-safe via _lock (same pattern
as SqliteVectorStore, safe under uvicorn single worker).
"""

import asyncio
import json
import threading
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from app.services.knowledge.search_tokens import document_terms, query_terms

from agentscope.rag import (
    VectorStoreBase,
    VectorRecord,
    VectorSearchResult,
    DocumentSummary,
    Chunk,
)
from agentscope.message import TextBlock


def _vec_to_pgstr(vector) -> str:
    """Convert float list to pgvector string literal '[v1,v2,...]'"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class PgVectorStore(VectorStoreBase):
    def __init__(self, dsn: Optional[str] = None):
        if dsn is None:
            from app.config import settings
            from sqlalchemy.engine.url import make_url
            url = make_url(settings.DATABASE_URL)
            dsn = (
                f"host={url.host} port={url.port} "
                f"user={url.username} password={url.password} "
                f"dbname={url.database}"
            )
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = False
        self._bootstrap()

    def _bootstrap(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_collections (
                    collection TEXT PRIMARY KEY,
                    dimensions INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_chunks (
                    id BIGSERIAL PRIMARY KEY,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT,
                    chunk_index INTEGER,
                    total_chunks INTEGER,
                    metadata JSONB,
                    embedding vector(1024) NOT NULL,
                    tsv tsvector
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_vchunks_coll "
                "ON vector_chunks(collection)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_vchunks_coll_doc "
                "ON vector_chunks(collection, document_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_vchunks_hnsw "
                "ON vector_chunks USING hnsw (embedding vector_cosine_ops)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_vchunks_tsv "
                "ON vector_chunks USING gin (tsv)"
            )
            self._conn.commit()

    # ---- internal sync impl (scheduled by to_thread) ----
    def _create_collection(self, name: str, dimensions: int):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO vector_collections (collection, dimensions) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (name, dimensions),
            )
            self._conn.commit()

    def _delete_collection(self, name: str):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM vector_collections WHERE collection = %s", (name,)
            )
            cur.execute(
                "DELETE FROM vector_chunks WHERE collection = %s", (name,)
            )
            self._conn.commit()

    def _has_collection(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT 1 FROM vector_collections WHERE collection = %s",
                (name,),
            )
            row = cur.fetchone()
        return row is not None

    def _insert(self, collection: str, records, replace_document_id=None):
        rows = []
        for rec in records:
            chunk = rec.chunk
            content_text = (
                getattr(chunk.content, "text", None)
                or (chunk.content if isinstance(chunk.content, str) else str(chunk.content))
            )
            rows.append((
                collection,
                rec.document_id,
                content_text,
                chunk.source,
                chunk.chunk_index,
                chunk.total_chunks,
                json.dumps(chunk.metadata or {}, ensure_ascii=False),
                _vec_to_pgstr(rec.vector),
            ))
        with self._lock:
            cur = self._conn.cursor()
            try:
                if replace_document_id is not None:
                    cur.execute(
                        "DELETE FROM vector_chunks WHERE collection=%s AND document_id=%s",
                        (collection, str(replace_document_id)),
                    )
                cur.executemany(
                """
                INSERT INTO vector_chunks
                    (collection, document_id, content, source,
                     chunk_index, total_chunks, metadata, embedding, tsv)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector,
                        to_tsvector('simple', %s))
                """,
                    [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], document_terms(r[2])) for r in rows],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _doc_vectors(self, collection: str):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT DISTINCT ON (document_id) document_id, embedding::text "
                "FROM vector_chunks WHERE collection=%s ORDER BY document_id, id",
                (collection,),
            )
            rows = cur.fetchall()
        import numpy as np
        return {doc_id: np.asarray(json.loads(vector), dtype=np.float32)
                for doc_id, vector in rows}

    def _delete(self, collection: str, document_id: str):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM vector_chunks "
                "WHERE collection = %s AND document_id = %s",
                (collection, document_id),
            )
            self._conn.commit()

    def _list_chunks(self, collection: str, document_id: str, limit: int, offset: int):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT count(*) FROM vector_chunks "
                "WHERE collection = %s AND document_id = %s",
                (collection, document_id),
            )
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT chunk_index, content FROM vector_chunks "
                "WHERE collection = %s AND document_id = %s "
                "ORDER BY chunk_index ASC LIMIT %s OFFSET %s",
                (collection, document_id, limit, offset),
            )
            rows = cur.fetchall()
        return int(total or 0), [
            {"chunk_index": r[0], "content": r[1]} for r in rows
        ]

    @staticmethod
    def _meta_clause(metadata_filter):
        """Build metadata JSONB filter SQL fragment, returns (sql, params)"""
        if not metadata_filter:
            return "", []
        clauses = []
        params = []
        for k, v in metadata_filter.items():
            clauses.append("metadata ->> %s = %s")
            params.extend([k, str(v)])
        return " AND " + " AND ".join(clauses), params

    def _search(self, collection: str, query_vector, top_k: int,
                metadata_filter, query_text=None):
        qvec_str = _vec_to_pgstr(query_vector)
        meta_clause, meta_params = self._meta_clause(metadata_filter)
        candidate_limit = min(100, max(20, int(top_k) * 8))
        vec_sql = (
            "SELECT id, document_id, content, source, "
            "chunk_index, total_chunks, metadata, "
            "(1 - (embedding <=> %s::vector)) AS vec_score "
            "FROM vector_chunks WHERE collection = %s"
            + meta_clause
            + " ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        vec_params = [qvec_str, collection] + meta_params + [qvec_str, candidate_limit]

        with self._lock:
            try:
                cur = self._conn.cursor()
                cur.execute(vec_sql, vec_params)
                vec_rows = cur.fetchall()
                lexical_rows = []
                terms = query_terms(query_text) if query_text else ""
                if terms:
                    fts_sql = (
                        "SELECT id, document_id, content, source, chunk_index, "
                        "total_chunks, metadata, ts_rank(tsv, to_tsquery('simple', %s)) "
                        "FROM vector_chunks WHERE collection = %s"
                        + meta_clause
                        + " AND tsv @@ to_tsquery('simple', %s) "
                        "ORDER BY ts_rank(tsv, to_tsquery('simple', %s)) DESC LIMIT %s"
                    )
                    cur.execute(fts_sql, [terms, collection] + meta_params + [terms, terms, candidate_limit])
                    lexical_rows = cur.fetchall()
            except Exception:
                self._conn.rollback()
                raise

        candidates = {}
        for weight, ranked in ((0.7, vec_rows), (0.3, lexical_rows)):
            for rank, row in enumerate(ranked, 1):
                row_id = row[0]
                if row_id not in candidates:
                    candidates[row_id] = [row, 0.0]
                candidates[row_id][1] += weight * 60.0 / (60 + rank)

        results = []
        for row, combined in sorted(candidates.values(), key=lambda item: item[1], reverse=True)[:top_k]:
            _, doc_id, content, source, cidx, tcnt, meta_raw, _ = row
            if isinstance(meta_raw, dict):
                meta = meta_raw
            else:
                try:
                    meta = json.loads(meta_raw) if meta_raw else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            chunk = Chunk(
                content=TextBlock(text=content), source=source,
                chunk_index=cidx, total_chunks=tcnt, metadata=meta,
            )
            results.append(VectorSearchResult(
                score=combined, document_id=doc_id, chunk=chunk,
            ))
        return results

    def _list_documents(self, collection: str, metadata_filter):
        meta_clause, meta_params = self._meta_clause(metadata_filter)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT document_id, source, metadata, count(*) "
                "FROM vector_chunks WHERE collection = %s"
                + meta_clause
                + " GROUP BY document_id, source, metadata",
                [collection] + meta_params,
            )
            rows = cur.fetchall()
        return [
            DocumentSummary(
                document_id=row[0],
                source=row[1],
                chunk_count=int(row[3]),
                metadata=json.loads(row[2]) if row[2] else {},
            )
            for row in rows
        ]

    # ---- VectorStoreBase async interface ----
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

    async def search(self, collection: str, query_vector, top_k: int = 5,
                     metadata_filter=None, query_text=None):
        return await asyncio.to_thread(
            self._search, collection, query_vector, top_k,
            metadata_filter, query_text,
        )

    async def list_documents(self, collection: str, metadata_filter=None):
        return await asyncio.to_thread(
            self._list_documents, collection, metadata_filter
        )

    async def list_chunks(self, collection: str, document_id: str,
                          limit: int = 50, offset: int = 0):
        """List chunks for document preview, returns (total, [{chunk_index, content}])"""
        return await asyncio.to_thread(
            self._list_chunks, collection, document_id, limit, offset
        )
