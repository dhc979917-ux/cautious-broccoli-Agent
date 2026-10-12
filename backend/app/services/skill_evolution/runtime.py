"""Conversation-only selection adapter; never changes global or scheduled choices."""
from fastapi import HTTPException
from app.models.skill import Skill
from app.models.skill_evolution import SessionSkillEvolution
from app.services.skill_evolution import policy
from app.services.skill_runtime import validate_selection


def selection(db, actor, session_id, requested):
    if not policy.settings.SKILL_EVOLUTION_ENABLED or not policy.settings.SKILL_HUB_ENABLED:
        return validate_selection(db, actor, requested)
    from app.models.session import Session
    session = db.get(Session, session_id)
    if not session or session.user_id != actor.id:
        raise HTTPException(404, '会话不存在')
    if requested is None:
        return validate_selection(db, actor, requested)
    names = list(requested)
    for row in db.query(SessionSkillEvolution).filter_by(session_id=session_id):
        source, target = db.get(Skill, row.source_skill_id), db.get(Skill, row.target_skill_id)
        if source and source.name in names and target:
            try:
                validate_selection(db, actor, [target.name])
            except HTTPException:
                continue  # Old source remains selected when personal variant is unavailable.
            names = [target.name if name == source.name else name for name in names]
    return validate_selection(db, actor, names)


def describe(db, actor, session_id):
    policy.actor_session(db, actor.id, session_id)
    return [{'source': db.get(Skill, row.source_skill_id).name,
             'target': db.get(Skill, row.target_skill_id).name}
            for row in db.query(SessionSkillEvolution).filter_by(session_id=session_id)
            if db.get(Skill, row.source_skill_id) and db.get(Skill, row.target_skill_id)]


def bind(db, job, source, target):
    if not source or job.source_resource_id == job.target_resource_id:
        return False
    source_members = {entry['name']: (path, entry) for path, entry in source.members.items()} if hasattr(source, 'members') else {}
    from app.services.skill_hub import access
    target_row = access.resource(db, job.target_resource_id)
    target_native = access.native(db, target_row, job.candidate_version_id)
    targets = target_native.members if target_row.kind == 'package' else {
        '.': {'skill_id': target_row.skill_id}}
    if not source_members:
        skill = db.get(Skill, getattr(source, 'skill_id', None))
        if skill:
            source_members[skill.name] = ('.', {'skill_id': skill.id})
    changed = False
    for name, (path, member) in source_members.items():
        if name not in (job.selected_skills or []) or path not in targets:
            continue
        key = (job.session_id, member['skill_id'])
        row = db.get(SessionSkillEvolution, key)
        if row:
            row.target_skill_id = targets[path]['skill_id']
        else:
            db.add(SessionSkillEvolution(session_id=job.session_id,
                source_skill_id=member['skill_id'], target_skill_id=targets[path]['skill_id']))
        changed = True
    return changed
