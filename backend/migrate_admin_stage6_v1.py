"""Add memory candidate governance without replacing the existing personal vault."""

import argparse

from sqlalchemy import inspect, text

from app.database import engine
from app.models.memory_governance import (MemoryEntry, MemoryExtractionMethod, MemorySourceDisposition,
    MemoryFusionItem, MemoryFusionTask, MemoryIndexJob)


TABLES = (MemoryEntry.__table__, MemoryFusionTask.__table__, MemoryFusionItem.__table__,
          MemoryExtractionMethod.__table__, MemoryIndexJob.__table__, MemorySourceDisposition.__table__)


def migrate(apply: bool = False, database_engine=None) -> list[str]:
    target = database_engine if database_engine is not None else engine
    existing = set(inspect(target).get_table_names())
    if not {"users", "knowledge_bases", "knowledge_documents", "audit_events"}.issubset(existing):
        raise RuntimeError("User, knowledge and audit schemas must precede memory migration")
    missing = [table.name for table in TABLES if table.name not in existing]
    additions = {
        'memory_fusion_task': {'options': "JSON NOT NULL DEFAULT '{}'", 'lease_token': 'VARCHAR(36)', 'lease_until': 'TIMESTAMP'},
        'memory_fusion_item': {'confidence': 'VARCHAR(20)'},
        'memory_extraction_method': {'user_id': 'INTEGER REFERENCES users(id)'},
        'memory_index_jobs': {'lease_token': 'VARCHAR(36)', 'lease_until': 'TIMESTAMP'},
    }
    for table, columns in additions.items():
        if table not in existing:
            continue
        present = {column['name'] for column in inspect(target).get_columns(table)}
        for name, definition in columns.items():
            if name not in present:
                missing.append(table + '.' + name)
                if apply:
                    with target.begin() as connection:
                        connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
    if apply:
        for table in TABLES:
            table.create(bind=target, checkfirst=True)
    print("Stage-6 schema: " + (", ".join(missing) if missing else "up to date"))
    return missing


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    migrate(arguments.apply)
