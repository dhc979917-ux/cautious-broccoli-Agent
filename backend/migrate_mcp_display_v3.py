"""Add presentation-only service descriptions without changing MCP runtime configuration."""

import argparse
import json

from sqlalchemy import inspect, text

from app.database import engine


def migrate(apply=False, database_engine=None):
    target = database_engine if database_engine is not None else engine
    inspector = inspect(target)
    if 'mcp_servers' not in inspector.get_table_names():
        return []
    columns = {column['name'] for column in inspector.get_columns('mcp_servers')}
    definitions = {'display_description': 'TEXT', 'description_digest': 'VARCHAR(64)',
                   'description_status': "VARCHAR(16) NOT NULL DEFAULT 'pending'"}
    pending = [f'ALTER TABLE mcp_servers ADD COLUMN {name} {definition}'
               for name, definition in definitions.items() if name not in columns]
    if apply:
        with target.begin() as connection:
            if target.dialect.name == 'postgresql':
                connection.execute(text('SELECT pg_advisory_xact_lock(52101003)'))
            existing = {column['name'] for column in inspect(connection).get_columns('mcp_servers')}
            for statement in pending:
                if statement.split()[5] not in existing:
                    connection.execute(text(statement))
    return pending


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(migrate(parser.parse_args().apply)))
