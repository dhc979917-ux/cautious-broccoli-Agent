"""Runtime adapter: system compatibility plus owner/shared snapshot selection."""
from fastapi import HTTPException
from app.models.skill import Skill
from app.models.skill_governance import SkillVersion
from app.models.user import User
from app.models.skill_hub import ScheduledSkillBinding, SkillHubVersion
from app.services.skill_hub import access


def usable(db, actor, existing):
    mapping = access.managed_map(db)
    result = [s for s in existing if s.id not in mapping]
    seen = {s.id for s in result}
    for skill in db.query(Skill).filter(Skill.id.in_(mapping), Skill.is_active.is_(True)) if mapping else []:
        if access.runtime_version(db, mapping[skill.id], skill.id, actor) and skill.id not in seen:
            result.append(skill)
    return result


def decorate(db, actor, items):
    mapping = access.managed_map(db)
    items = [dict(item, origin='system', source_group='system') for item in items if item['id'] not in mapping]
    known = {item['id'] for item in items}
    for skill in usable(db, actor, []):
        if skill.id in known:
            continue
        row = mapping[skill.id]
        version_id = access.runtime_version(db, row, skill.id, actor)
        items.append({'id': skill.id, 'name': skill.name, 'display_name': skill.display_name,
            'description': skill.description, 'required_role': 'viewer', 'is_active': True, 'has_access': True,
            'package_id': skill.package_id, 'package_display_name': row.title if row.kind == 'package' else None,
            'created_at': skill.created_at, 'origin': row.origin, 'resource_id': row.id,
            'source_group': 'mine' if row.owner_id == actor.id else 'shared', 'version_id': version_id})
    return items


def bind_job(db, job, actor):
    from app.services.skill_runtime import validate_selection
    from app.services.skill_service import SkillService
    names = validate_selection(db, actor, job.selected_skills)
    mapping = access.managed_map(db)
    versions = {}
    if job.is_public and any(s.id in mapping for s in SkillService(db).get_usable_skills_for_user(actor) if s.name in names):
        raise HTTPException(409, '公共任务不能绑定个人或分享技能，请创建个人任务')
    for skill in SkillService(db).get_usable_skills_for_user(actor):
        if skill.name in names:
            if skill.id in mapping:
                version_id = access.runtime_version(db, mapping[skill.id], skill.id, actor)
            else:
                from app.models.skill_governance import SkillPublication
                publication = db.get(SkillPublication, skill.id)
                version_id = publication.version_id if publication else None
            if version_id:
                versions[str(skill.id)] = version_id
    row = db.get(ScheduledSkillBinding, job.id)
    if row:
        row.versions, row.revision = versions, row.revision + 1
    else:
        db.add(ScheduledSkillBinding(job_id=job.id, versions=versions))
    return versions


def job_versions(db, job, actor):
    row = db.get(ScheduledSkillBinding, job.id)
    if row is None:
        versions = bind_job(db, job, actor)
        db.flush()
    else:
        versions = row.versions
    mapping = access.managed_map(db)
    from app.services.skill_service import SkillService
    allowed = {s.id for s in SkillService(db).get_usable_skills_for_user(actor)}
    for skill_id, version_id in versions.items():
        identifier = int(skill_id)
        if identifier not in allowed:
            raise HTTPException(409, '任务绑定技能权限已失效或已停用，请更新任务')
        if identifier in mapping:
            resource = mapping[identifier]
            if resource.owner_id == actor.id and not access.permits_native(db, resource, identifier, version_id, actor):
                raise HTTPException(409, "任务绑定版本已被安全停用或不属于该资源")
            if resource.owner_id != actor.id:
                share = next((s for s in access.shares_for(db, actor) if s.resource_id == resource.id), None)
                if share is None or access.published_member(db, resource, identifier, share.version_id) != version_id:
                    raise HTTPException(409, '任务分享版本已变化，请明确更新任务绑定')
            record = db.get(SkillHubVersion, version_id)
            if access.is_blocked(db, resource, version_id) or (record and record.state != 'published'):
                raise HTTPException(409, '任务绑定版本已被安全停用')
        version = db.get(SkillVersion, version_id)
        if version is None or version.skill_id != identifier or version.status != 'published':
            raise HTTPException(409, '任务绑定版本不可用')
        access.check_dependencies(db, version, actor)
    return versions


def binding_description(db, job):
    from sqlalchemy import inspect
    if not inspect(db.connection()).has_table(ScheduledSkillBinding.__tablename__):
        return {}
    row = db.get(ScheduledSkillBinding, job.id)
    return row.versions if row else {}
