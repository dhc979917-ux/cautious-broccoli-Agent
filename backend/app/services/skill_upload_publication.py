from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import inspect

from app.config import settings
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.services import skill_versions


def publish_uploaded_skills(db: Session, skill_ids: list[int], actor_id: int | None) -> int:
    if not settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        return 0
    published = 0
    for skill_id in skill_ids:
        skill = db.get(Skill, skill_id)
        if skill is None or not skill.is_active:
            continue
        if db.get(SkillPublication, skill_id) is not None:
            continue
        version = db.query(SkillVersion).filter_by(skill_id=skill_id).order_by(SkillVersion.created_at.desc()).first()
        if version is None:
            version = skill_versions.create_snapshot(db, skill_id, actor_id)
        if version.status != 'draft' or version.dependencies:
            raise HTTPException(409, '已有非上传草稿，不能自动覆盖发布；请检查技能配置。')
        result = skill_versions.sandbox_smoke_test(db, skill_id, version.id)
        if result['status'] != 'smoke_passed':
            raise HTTPException(409, '上传技能未通过隔离校验，暂不展示到技能广场。')
        skill_versions.publish(db, skill_id, version.id, 0, actor_id, notify_package=False)
        published += 1
    from app.models.skill_resource import PackageVersion
    if inspect(db.bind).has_table(PackageVersion.__tablename__):
        from app.services.skill_package_versions import ensure_baseline
        package_ids = {skill.package_id for skill in db.query(Skill).filter(Skill.id.in_(skill_ids)) if skill.package_id}
        for package_id in package_ids:
            ensure_baseline(db, package_id, actor_id)
    return published
