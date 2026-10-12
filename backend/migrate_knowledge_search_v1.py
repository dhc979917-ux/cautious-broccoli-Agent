"""Idempotent one-time backfill of Chinese/Latin PostgreSQL search lexemes."""

from sqlalchemy import text

from app.database import engine
from app.services.knowledge.search_tokens import document_terms


def backfill(batch_size: int = 200) -> int:
    if engine.dialect.name != "postgresql":
        return 0
    migrated = 0
    last_id = 0
    while True:
        with engine.connect() as connection:
            batch = connection.execute(text(
                "SELECT id, content FROM vector_chunks WHERE id > :last_id "
                "ORDER BY id LIMIT :batch_size"
            ), {"last_id": last_id, "batch_size": batch_size}).all()
        if not batch:
            break
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE vector_chunks SET tsv = to_tsvector('simple', :terms) "
                "WHERE id = :id AND content = :content"
            ), [{"id": row.id, "content": row.content,
                 "terms": document_terms(row.content)} for row in batch])
        migrated += len(batch)
        last_id = batch[-1].id
    return migrated


if __name__ == "__main__":
    print(f"Backfilled up to {backfill()} vector chunks")
