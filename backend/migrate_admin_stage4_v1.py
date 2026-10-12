"""Add immutable Skill snapshots and published-version pointers."""

import argparse

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.database import engine
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillReleaseEvent, SkillVersion
from app.services.skill_versions import create_snapshot


TABLES = (SkillVersion.__table__, SkillPublication.__table__, SkillReleaseEvent.__table__)


def migrate(apply: bool = False, database_engine=None) -> list[str]:
    target = database_engine if database_engine is not None else engine
    existing = set(inspect(target).get_table_names())
    if not {"users", "skills"}.issubset(existing):
        raise RuntimeError("Existing Skill and user tables are required")
    pending = [table.name for table in TABLES if table.name not in existing]
    if apply:
        for table in TABLES:
            table.create(bind=target, checkfirst=True)
    print("Stage-4 schema: " + (", ".join(pending) if pending else "up to date"))
    return pending


def backfill(database_engine=None) -> dict:
    target = database_engine if database_engine is not None else engine
    outcome = {"published": [], "missing": [], "inactive": []}
    with Session(target) as db:
        for skill in db.query(Skill).order_by(Skill.id):
            if db.get(SkillPublication, skill.id):
                continue
            if not skill.is_active:
                outcome["inactive"].append(skill.id)
                continue
            try:
                create_snapshot(db, skill.id, baseline=True)
                outcome["published"].append(skill.id)
            except Exception:
                db.rollback()
                outcome["missing"].append(skill.id)
    return outcome


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    arguments = parser.parse_args()
    migrate(arguments.apply)
    if arguments.baseline:
        if not arguments.apply:
            parser.error("--baseline requires --apply and verified persistent mounts")
        outcome = backfill()
        print(outcome)
        if outcome['missing']:
            raise RuntimeError('Active Skill baseline incomplete; versioned runtime must remain disabled')
