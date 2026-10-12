"""Bridge legacy system administration to the independent hub policy, no private access."""
from fastapi import HTTPException
from app.config import settings
from app.models.skill_hub import SkillHubResource
from app.services.skill_hub import sharing
from app.services.skill_hub.registry import register_system


def policies(db):
    if not settings.SKILL_HUB_ENABLED:
        return {}
    return {(r.kind, r.package_id if r.kind == 'package' else r.skill_id): r
            for r in db.query(SkillHubResource).filter_by(owner_id=None, origin='system')}


def describe(row, schema, mapping, kind='skill'):
    result = schema.model_validate(row).model_dump()
    inherited = kind == 'skill' and row.package_id is not None
    key = ('package', row.package_id) if inherited else (kind, row.id)
    resource = mapping.get(key)
    result.update(allow_fork=bool(resource and resource.allow_fork), fork_policy_inherited=inherited)
    return result


def prepare(db, actor, native, kind, value, values):
    """Validate before metadata writes; the existing service commits both in one transaction."""
    if value is not None and actor.role != 'admin':
        raise HTTPException(403, '仅管理员可修改派生许可')
    if not settings.SKILL_HUB_ENABLED:
        if value is not None:
            raise HTTPException(409, '技能中心尚未开放')
        return
    if kind == 'skill' and native.package_id is not None:
        if value is not None:
            raise HTTPException(422, '包内技能继承整包派生许可，请编辑技能包')
        return
    register_system(db)
    row = policies(db).get((kind, native.id))
    if row is None:
        raise HTTPException(404, '平台技能资源不存在')
    if value is not None:
        sharing.audit(db, actor, 'fork_policy', row.id, {'allow_fork': value})
        row.allow_fork = value
    if values.get('display_name') is not None:
        row.title = values['display_name']
    if values.get('description') is not None:
        row.summary = values['description']
