"""Add the isolated Skill Hub without rewriting existing system resources."""
import argparse
from uuid import uuid4
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from app.database import engine
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.skill_hub import (SkillHubResource, SkillHubVersion, SkillHubFork, SkillHubShare,
    SkillMarketplaceListing, SkillMarketplaceRequest, SkillHubInstall, SkillHubReview,
    SkillHubReport, SkillHubBlock, ScheduledSkillBinding)

TABLES = tuple(model.__table__ for model in (SkillHubResource, SkillHubVersion, SkillHubFork,
    SkillHubShare, SkillMarketplaceListing, SkillMarketplaceRequest, SkillHubInstall,
    SkillHubReview, SkillHubReport, SkillHubBlock, ScheduledSkillBinding))


def migrate(apply=False, database_engine=None):
    target = database_engine or engine
    existing = set(inspect(target).get_table_names())
    pending = [table.name for table in TABLES if table.name not in existing]
    if apply:
        for table in TABLES:
            table.create(target, checkfirst=True)
        if target.dialect.name == 'postgresql':
            with target.begin() as connection:
                connection.execute(text('ALTER TABLE skill_hub_blocks ALTER COLUMN actor_id DROP NOT NULL'))
                for field, parent in [('skill_id', 'skills'), ('package_id', 'skill_packages')]:
                    constraints = inspect(connection).get_foreign_keys('skill_hub_resources')
                    for constraint in constraints:
                        if constraint['constrained_columns'] == [field] and constraint.get('options', {}).get('ondelete') != 'SET NULL':
                            name = constraint['name']
                            connection.execute(text(f'ALTER TABLE skill_hub_resources DROP CONSTRAINT "{name}"'))
                            connection.execute(text(f'ALTER TABLE skill_hub_resources ADD CONSTRAINT "{name}" FOREIGN KEY ({field}) REFERENCES {parent}(id) ON DELETE SET NULL'))
        with Session(target) as db:
            from app.services.skill_hub.registry import register_system
            register_system(db)
            db.commit()
    print('Skill Hub schema: ' + (', '.join(pending) if pending else 'up to date'))
    return pending


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    migrate(parser.parse_args().apply)
