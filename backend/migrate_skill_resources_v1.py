"""Add durable package snapshots and recoverable resource-change jobs."""

import argparse

from sqlalchemy import inspect

from app.database import engine
from app.models.skill_resource import PackagePublication, PackageVersion, ResourceChangeJob


TABLES = (PackageVersion.__table__, PackagePublication.__table__, ResourceChangeJob.__table__)


def migrate(apply=False, database_engine=None):
    target = database_engine if database_engine is not None else engine
    existing = set(inspect(target).get_table_names())
    if not {'users', 'skills', 'skill_packages', 'skill_versions'}.issubset(existing):
        raise RuntimeError('Skill governance migrations must precede resource migration')
    missing = [table.name for table in TABLES if table.name not in existing]
    if apply:
        for table in TABLES:
            table.create(bind=target, checkfirst=True)
    print('Skill resource schema: ' + (', '.join(missing) if missing else 'up to date'))
    return missing


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    arguments = parser.parse_args()
    migrate(arguments.apply)
    if arguments.baseline:
        if not arguments.apply:
            parser.error('--baseline requires --apply and verified backups')
        from app.database import SessionLocal
        from app.models.skill_package import SkillPackage
        from app.services.skill_package_versions import ensure_baseline
        with SessionLocal() as db:
            packages = db.query(SkillPackage).filter_by(is_active=True, dir_missing=False).all()
            for package in packages:
                ensure_baseline(db, package.id)
            print('Verified package baselines: ' + str(len(packages)))
