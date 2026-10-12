"""FTS5 hybrid search migration: create FTS5 virtual table, backfill from existing chunks.
Idempotent: skips if fts_chunks already exists.
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings

DB_PATH = settings.VECTOR_DB_PATH


def migrate():
    conn = sqlite3.connect(DB_PATH)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "fts_chunks" in tables:
        print("[INFO] FTS5 table already exists, skipping")
        conn.close()
        return

    print("[INFO] Creating fts_chunks virtual table...")
    conn.execute(
        """
        CREATE VIRTUAL TABLE fts_chunks USING fts5(
            content,
            collection UNINDEXED,
            document_id UNINDEXED,
            source UNINDEXED,
            chunk_index UNINDEXED,
            total_chunks UNINDEXED,
            metadata UNINDEXED
        )
        """
    )

    rows = conn.execute(
        "SELECT id, content, collection, document_id, source, "
        "chunk_index, total_chunks, metadata FROM chunks"
    ).fetchall()
    count = 0
    for row in rows:
        conn.execute(
            "INSERT INTO fts_chunks "
            "(rowid, content, collection, document_id, source, "
            "chunk_index, total_chunks, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        count += 1
    conn.commit()
    conn.close()
    print(f"[INFO] FTS5 migration done, backfilled {count} chunks")


if __name__ == "__main__":
    migrate()
