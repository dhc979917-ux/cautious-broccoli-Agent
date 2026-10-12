"""vector.db (SQLite 向量库) -> PostgreSQL pgvector (vector_chunks)。

用法（backend 容器内，vector.db 已 docker cp 到 /app/vector.db）：
    python3 /app/_migrate_vec.py /app/vector.db
幂等：按 (collection, document_id, chunk_index) 去重，已存在则跳过；可中断重跑。
"""
import json
import sqlite3
import struct
import sys

from app.database import engine
from sqlalchemy import text

_ENSURE = """
CREATE EXTENSION IF NOT EXISTS vector;
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
);
CREATE INDEX IF NOT EXISTS idx_vchunks_hnsw ON vector_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_vchunks_tsv ON vector_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS idx_vchunks_coll ON vector_chunks(collection);
"""


def main():
    sqlite_path = sys.argv[1] if len(sys.argv) > 1 else "/app/vector.db"
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT collection, document_id, content, source, chunk_index, "
        "total_chunks, metadata, vector FROM chunks"
    ).fetchall()
    print("source chunks:", len(rows))

    with engine.connect() as pg:
        pg.execute(text(_ENSURE))
        pg.commit()
        existing = set()
        for r in pg.execute(text(
            "SELECT collection, document_id, chunk_index FROM vector_chunks"
        )):
            existing.add((r[0], r[1], r[2]))
        inserted = 0
        for r in rows:
            key = (r["collection"], r["document_id"], r["chunk_index"])
            if key in existing:
                continue
            blob = r["vector"]
            n = len(blob) // 4
            vec = struct.unpack(f"{n}f", blob)
            vec_str = "[" + ",".join(format(v, ".7f") for v in vec) + "]"
            content = (r["content"] or "").replace("\x00", "")
            meta = r["metadata"]
            try:
                meta_j = json.dumps(json.loads(meta)) if meta else None
            except Exception:
                meta_j = None
            pg.execute(text(
                "INSERT INTO vector_chunks "
                "(collection, document_id, content, source, chunk_index, "
                " total_chunks, metadata, embedding, tsv) "
                "VALUES (:c, :d, :ct, :s, :ci, :tc, :m, CAST(:v AS vector), to_tsvector(:ct))"
            ), {
                "c": r["collection"], "d": r["document_id"], "ct": content,
                "s": r["source"], "ci": r["chunk_index"], "tc": r["total_chunks"],
                "m": meta_j, "v": vec_str,
            })
            inserted += 1
            if inserted % 50 == 0:
                pg.commit()
                print("  inserted", inserted)
        pg.commit()
        total = pg.execute(text("SELECT count(*) FROM vector_chunks")).scalar()
        print("done: inserted", inserted, "total in pg:", total)
    conn.close()


if __name__ == "__main__":
    main()