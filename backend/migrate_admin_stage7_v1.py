import argparse

from sqlalchemy import inspect

from app.database import engine
from app.models.operations import GovernanceCleanup, GovernancePolicy


def migrate(apply=False, database_engine=None):
    target = database_engine if database_engine is not None else engine
    existing = set(inspect(target).get_table_names())
    if not {'users', 'audit_events', 'memory_fusion_task'}.issubset(existing):
        raise RuntimeError('Stages 0, 1 and 6 must precede operations migration')
    tables = (GovernancePolicy.__table__, GovernanceCleanup.__table__)
    missing = [table.name for table in tables if table.name not in existing]
    if apply:
        for table in tables:
            table.create(bind=target, checkfirst=True)
    print('Stage-7 schema: ' + (', '.join(missing) if missing else 'up to date'))
    return missing


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    migrate(parser.parse_args().apply)
