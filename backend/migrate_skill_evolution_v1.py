"""Additive, idempotent personal skill-evolution migration."""
import argparse
from app.database import engine
from app.models.skill_evolution import (SkillEvolutionPreference, SkillEvolutionBinding,
    SkillEvolutionJob, SessionSkillEvolution)


def migrate(apply=False):
    tables = [SkillEvolutionPreference.__table__, SkillEvolutionBinding.__table__,
              SkillEvolutionJob.__table__, SessionSkillEvolution.__table__]
    if apply:
        for table in tables:
            table.create(engine, checkfirst=True)
    print('Skill evolution schema: ' + ('applied' if apply else 'dry run'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    migrate(parser.parse_args().apply)
