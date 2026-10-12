"""System catalogue registration is additive, independent of private draft governance."""
from uuid import uuid4
from sqlalchemy import text
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.skill_hub import SkillHubResource


def register_system(db):
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(526201)'))
    rows = db.query(SkillHubResource).all()
    skills = {r.skill_id for r in rows if r.skill_id}
    packages = {r.package_id for r in rows if r.package_id}
    for skill in db.query(Skill).filter(Skill.package_id.is_(None)):
        if skill.id not in skills:
            db.add(SkillHubResource(id=str(uuid4()), kind='skill', skill_id=skill.id,
                title=skill.display_name_zh or skill.display_name,
                summary=skill.description_zh or skill.description or '', origin='system', allow_fork=False))
    for package in db.query(SkillPackage):
        if package.id not in packages:
            db.add(SkillHubResource(id=str(uuid4()), kind='package', package_id=package.id,
                title=package.display_name, summary=package.description or '', origin='system', allow_fork=False))
    db.flush()
