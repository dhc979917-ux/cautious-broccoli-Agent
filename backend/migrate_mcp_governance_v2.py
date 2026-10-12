"""Add MCP visibility and independent tool display metadata without resetting configuration."""

import argparse
import json

from sqlalchemy import inspect, text

from app.database import engine


def migrate(apply=False, database_engine=None):
    target = database_engine if database_engine is not None else engine
    inspector = inspect(target)
    pending = []
    columns = {
        "mcp_servers": {
            "hidden": "BOOLEAN NOT NULL DEFAULT false",
            "visibility_roles": "JSON NOT NULL DEFAULT '[]'",
            "visibility_user_ids": "JSON NOT NULL DEFAULT '[]'",
            "visibility_revision": "INTEGER NOT NULL DEFAULT 0",
            "source_files": "JSON NOT NULL DEFAULT '{}'",
        },
        "mcp_tool_catalog": {
            "display_description": "TEXT",
            "description_digest": "VARCHAR(64)",
            "description_status": "VARCHAR(16) NOT NULL DEFAULT 'pending'",
        },
    }
    for table, definitions in columns.items():
        if table not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        pending.extend(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'
                       for name, definition in definitions.items() if name not in existing)
    if apply:
        with target.begin() as connection:
            if target.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(52061010)"))
            current = inspect(connection)
            for statement in pending:
                parts = statement.split()
                if parts[5] not in {column["name"] for column in current.get_columns(parts[2])}:
                    connection.execute(text(statement))
    return pending


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(migrate(parser.parse_args().apply), ensure_ascii=False))
