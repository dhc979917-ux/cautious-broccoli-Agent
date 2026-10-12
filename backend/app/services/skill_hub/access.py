"""Object-level access policy shared by UI, file APIs and runtime."""
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import or_, inspect
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.models.skill_resource import PackagePublication, PackageVersion
from app.models.skill_hub import SkillHubResource, SkillHubShare, SkillHubBlock, SkillHubVersion


def utcnow():
    return datetime.now(timezone.utc)


def active_share(share):
    return bool(share and share.revoked_at is None and
                (share.expires_at is None or share.expires_at.replace(tzinfo=timezone.utc) > utcnow()))


def resource(db, identifier, actor=None, own=False, lock=False):
    query = db.query(SkillHubResource).filter_by(id=identifier)
    row = query.populate_existing().with_for_update().one_or_none() if lock else query.one_or_none()
    if row is None or (own and (actor is None or row.owner_id != actor.id)):
        raise HTTPException(404, '技能资源不存在或无权访问')
    return row


def owner(db, identifier, actor):
    # Consistent lock order: owner quota row, then resource/version rows.
    from app.models.user import User
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    row = resource(db, identifier, actor, own=True, lock=True)
    if row.blocked:
        raise HTTPException(409, '技能已被安全停用')
    return row


def shares_for(db, actor):
    return [s for s in db.query(SkillHubShare).filter_by(shared_to=actor.id) if active_share(s)]


def native(db, row, version_id):
    model = PackageVersion if row.kind == 'package' else SkillVersion
    version = db.get(model, version_id)
    key = 'package_id' if row.kind == 'package' else 'skill_id'
    if version is None or getattr(version, key) != getattr(row, key):
        raise HTTPException(404, '技能版本不存在')
    return version


def current_id(db, row):
    if row.owner_id is not None or row.origin == 'retained_private':
        return row.published_version_id
    publication = db.get(PackagePublication if row.kind == 'package' else SkillPublication,
                         row.package_id if row.kind == 'package' else row.skill_id)
    return publication.version_id if publication else None


def is_blocked(db, row, version_id=None):
    return row.blocked or db.query(SkillHubBlock.id).filter(
        SkillHubBlock.resource_id == row.id,
        or_(SkillHubBlock.version_id.is_(None), SkillHubBlock.version_id == version_id)).first() is not None


def visible_version(db, row, actor, version_id=None):
    if row.origin == 'retained_private':
        raise HTTPException(404, '已注销作者的私有资源不对外开放')
    if row.owner_id == actor.id:
        version_id = version_id or row.draft_version_id or current_id(db, row)
    elif row.owner_id is None:
        from app.services.skill_service import SkillService
        allowed = {s.id for s in SkillService(db).get_usable_skills_for_user(actor)}
        ids = member_ids(db, row)
        if not ids or not ids.issubset(allowed):
            raise HTTPException(404, '技能资源不存在或无权访问')
        version_id = version_id or current_id(db, row)
        native_version = native(db, row, version_id)
        if row.kind == 'package' and any(db.get(SkillVersion, m['version_id']).status != 'published' for m in native_version.members.values()):
            raise HTTPException(404, '技能版本不存在')
        if row.kind == 'skill' and native_version.status != 'published':
            raise HTTPException(404, '技能版本不存在')
    else:
        share = next((s for s in shares_for(db, actor) if s.resource_id == row.id), None)
        if not share or row.archived or (version_id is not None and version_id != share.version_id):
            raise HTTPException(404, '技能资源不存在或无权访问')
        version_id = share.version_id
    if not version_id:
        raise HTTPException(409, '技能尚未建立可用版本')
    if is_blocked(db, row, version_id):
        raise HTTPException(409, '该技能版本已被安全停用')
    return native(db, row, version_id)


def member_ids(db, row):
    return {s.id for s in db.query(Skill).filter_by(package_id=row.package_id)} if row.kind == 'package' else {row.skill_id}


def managed_map(db):
    if not inspect(db.connection()).has_table(SkillHubResource.__tablename__):
        return {}
    rows = db.query(SkillHubResource).filter(or_(SkillHubResource.owner_id.isnot(None), SkillHubResource.origin == 'retained_private')).all()
    by_package = {r.package_id: r for r in rows if r.kind == 'package'}
    by_skill = {r.skill_id: r for r in rows if r.kind == 'skill'}
    for skill in db.query(Skill).filter(Skill.package_id.in_(by_package)) if by_package else []:
        by_skill[skill.id] = by_package[skill.package_id]
    return by_skill


def system_only(query, db, model):
    if not inspect(db.connection()).has_table(SkillHubResource.__tablename__):
        return query
    if model.__tablename__ == 'skills':
        ids = list(managed_map(db))
    else:
        ids = [r.package_id for r in db.query(SkillHubResource).filter(or_(SkillHubResource.owner_id.isnot(None), SkillHubResource.origin == 'retained_private')) if r.package_id]
    return query.filter(~model.id.in_(ids)) if ids else query


def published_member(db, row, skill_id, version_id):
    value = native(db, row, version_id)
    if row.kind == 'skill':
        return value.id
    return next((x['version_id'] for x in value.members.values() if x['skill_id'] == skill_id), None)


def runtime_version(db, row, skill_id, actor):
    if actor is None or row.archived or is_blocked(db, row):
        return None
    if row.owner_id == actor.id:
        version_id = row.published_version_id
    else:
        share = next((s for s in shares_for(db, actor) if s.resource_id == row.id), None)
        version_id = share.version_id if share else None
    if not version_id or is_blocked(db, row, version_id):
        return None
    return published_member(db, row, skill_id, version_id)


def check_dependencies(db, version, actor, seen=None, allowed_drafts=None):
    allowed_drafts = set(allowed_drafts or [])
    from app.services.skill_versions import validate_dependencies
    from app.services.mcp_access import can_use
    from app.models.mcp_server import McpServer
    from app.services.model_access import allowed_models
    validate_dependencies(db, version, allowed_drafts)
    seen = set() if seen is None else seen
    if version.id in seen:
        raise HTTPException(409, '技能依赖存在环路')
    seen = seen | {version.id}
    mapping = managed_map(db)
    for dependency in version.dependencies or []:
        if dependency['kind'] == 'skill':
            target = db.get(SkillVersion, dependency['version_id'])
            target_resource = mapping.get(target.skill_id)
            if target_resource:
                if target.id not in allowed_drafts and not permits_native(db, target_resource, target.skill_id, target.id, actor):
                    raise HTTPException(403, '无权使用依赖技能的指定版本')
            else:
                from app.services.skill_service import SkillService
                allowed = {s.id for s in SkillService(db).get_usable_skills_for_user(actor)} if actor else set()
                if target.skill_id not in allowed:
                    raise HTTPException(403, '无权使用依赖技能')
            check_dependencies(db, target, actor, seen, allowed_drafts)
        elif dependency['kind'] == 'tool':
            if not can_use(db.get(McpServer, dependency['server_id']), actor):
                raise HTTPException(403, '无权使用依赖工具')
        elif dependency['kind'] == 'model':
            if actor is None or dependency['model_id'] not in allowed_models(db, actor):
                raise HTTPException(403, '无权使用依赖模型')


def permits_native(db, row, skill_id, version_id, actor):
    if actor is None or row.archived or is_blocked(db, row, version_id):
        return False
    if row.owner_id != actor.id:
        return runtime_version(db, row, skill_id, actor) == version_id
    if row.kind == 'skill':
        record = db.get(SkillHubVersion, version_id)
        return bool(record and record.resource_id == row.id and record.state == 'published')
    for record in db.query(SkillHubVersion).filter_by(resource_id=row.id, state='published'):
        if is_blocked(db, row, record.id):
            continue
        if published_member(db, row, skill_id, record.id) == version_id:
            return True
    return False


def preview_allowed(db, row, actor):
    if row.owner_id == actor.id:
        return True
    if row.owner_id is None:
        return bool(row.allow_fork)
    return any(s.resource_id == row.id and s.allow_fork for s in shares_for(db, actor))


def require_preview(db, row, actor, community=False):
    allowed = bool(row.allow_fork) if community else preview_allowed(db, row, actor)
    if not allowed:
        raise HTTPException(403, '此资源未开放派生许可，仅可查看目录结构，不能预览或下载文件内容')
