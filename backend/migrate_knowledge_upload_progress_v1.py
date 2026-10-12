"""Add the knowledge upload batch id column.

Run from backend/: python migrate_knowledge_upload_progress_v1.py
"""

from sqlalchemy import inspect, text

from app.database import engine


TABLE = "knowledge_documents"
COLUMN = "upload_batch_id"
COLUMN_TYPE = "VARCHAR(32)"


def migrate() -> None:
    inspector = inspect(engine)
    if not inspector.has_table(TABLE):
        print(f"skip: table {TABLE} does not exist")
        return
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    if COLUMN in columns:
        print(f"skip: {TABLE}.{COLUMN} already exists")
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {COLUMN_TYPE}"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_{COLUMN} ON {TABLE} ({COLUMN})"))
    print(f"added {TABLE}.{COLUMN}")


if __name__ == "__main__":
    migrate()
