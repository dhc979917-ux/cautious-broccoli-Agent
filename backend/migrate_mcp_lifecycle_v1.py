from sqlalchemy import inspect, text

from app.database import engine


def migrate(apply=False, database_engine=None):
    target = database_engine or engine
    inspector = inspect(target)
    if not inspector.has_table('mcp_servers'):
        return []
    if 'is_deleted' in {column['name'] for column in inspector.get_columns('mcp_servers')}:
        return []
    statements = ['ALTER TABLE mcp_servers ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT false']
    if apply:
        with target.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    return statements


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    migrate(parser.parse_args().apply)
