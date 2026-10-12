"""Owner-only review surfaces; automatic decisions stay behind the server worker."""
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_db
from app.core.deps import get_current_user
from app.api.v1.skill_hub import enabled
from app.models.skill_evolution import SkillEvolutionPreference, SkillEvolutionJob, SkillEvolutionBinding
from app.models.skill_hub import SkillHubResource
from app.schemas.skill_evolution import Preference, Pause, Confirmation, Target
from app.services.skill_evolution import policy, lifecycle, runtime
from app.services.skill_hub import access

router = APIRouter(prefix='/skill-evolution', tags=['个人技能进化'], dependencies=[Depends(enabled)])


@router.get('/preferences')
def preferences(actor=Depends(get_current_user), db=Depends(get_db)):
    return policy.preference(db, actor.id)


@router.put('/preferences')
def update_preferences(body: Preference, actor=Depends(get_current_user), db=Depends(get_db)):
    from app.models.user import User
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    row = db.get(SkillEvolutionPreference, actor.id)
    if not row:
        row = SkillEvolutionPreference(user_id=actor.id); db.add(row)
    row.enabled = body.enabled
    if not body.enabled:
        for job in db.query(SkillEvolutionJob).filter_by(user_id=actor.id, status='queued'):
            job.status = 'cancelled'
    db.commit()
    return policy.preference(db, actor.id)


@router.put('/resources/{identifier}/pause')
def pause(identifier: str, body: Pause, actor=Depends(get_current_user), db=Depends(get_db)):
    access.owner(db, identifier, actor)
    row = db.get(SkillEvolutionPreference, actor.id)
    if not row:
        row = SkillEvolutionPreference(user_id=actor.id, enabled=False); db.add(row)
    paused = set(row.paused_resources or [])
    paused.add(identifier) if body.paused else paused.discard(identifier)
    row.paused_resources = sorted(paused)
    db.commit()
    return policy.preference(db, actor.id)


@router.get('/jobs')
def jobs(resource_id: str = '', session_id: int | None = None, actor=Depends(get_current_user), db=Depends(get_db)):
    query = db.query(SkillEvolutionJob).filter_by(user_id=actor.id)
    if resource_id:
        access.owner(db, resource_id, actor)
        query = query.filter_by(target_resource_id=resource_id)
    if session_id:
        policy.actor_session(db, actor.id, session_id)
        query = query.filter_by(session_id=session_id)
    return [lifecycle.describe(job) for job in query.order_by(SkillEvolutionJob.created_at.desc()).limit(100)]


@router.get('/sessions/{session_id}/bindings')
def bindings(session_id: int, actor=Depends(get_current_user), db=Depends(get_db)):
    return runtime.describe(db, actor, session_id)


@router.get('/jobs/{identifier}')
def job(identifier: str, actor=Depends(get_current_user), db=Depends(get_db)):
    return lifecycle.describe(lifecycle.owned_job(db, actor, identifier))


@router.get('/jobs/{identifier}/diff')
def diff(identifier: str, actor=Depends(get_current_user), db=Depends(get_db)):
    job = lifecycle.owned_job(db, actor, identifier)
    if not job.candidate_version_id:
        return []
    target = access.owner(db, job.target_resource_id, actor)
    from app.services.skill_hub import content
    after = lifecycle.texts(content.version_directory(db, target, access.native(db, target, job.candidate_version_id)))
    if job.base_version_id:
        before = lifecycle.texts(content.version_directory(db, target, access.native(db, target, job.base_version_id)))
    elif job.source_resource_id:
        source = access.resource(db, job.source_resource_id)
        policy.permit_fork(db, source, actor, job.source_version_id)
        before = lifecycle.texts(content.version_directory(db, source, access.native(db, source, job.source_version_id)))
    else:
        before = {}
    import difflib
    return [{'path': path, 'diff': ''.join(difflib.unified_diff(before.get(path, '').splitlines(True),
        after.get(path, '').splitlines(True), fromfile='before/'+path, tofile='after/'+path))}
        for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]


@router.post('/jobs/{identifier}/confirm')
def confirm(identifier: str, body: Confirmation, actor=Depends(get_current_user), db=Depends(get_db)):
    job = lifecycle.owned_job(db, actor, identifier, lock=True)
    return lifecycle.describe(lifecycle.publish(db, actor, job, body.acknowledge_risk))


@router.post('/jobs/{identifier}/reject')
@router.post('/jobs/{identifier}/cancel')
def reject(identifier: str, actor=Depends(get_current_user), db=Depends(get_db)):
    job = lifecycle.owned_job(db, actor, identifier, lock=True)
    return lifecycle.describe(lifecycle.discard(db, actor, job))


@router.post('/jobs/{identifier}/retry')
def retry(identifier: str, actor=Depends(get_current_user), db=Depends(get_db)):
    job = lifecycle.owned_job(db, actor, identifier, lock=True)
    if job.status not in {'failed', 'conflict'} or not policy.is_enabled(db, actor.id):
        raise HTTPException(409, '作业状态不允许此操作，请刷新并检查草稿或自动进化设置')
    if job.target_resource_id:
        row = access.owner(db, job.target_resource_id, actor)
        if row.draft_version_id not in {None, job.candidate_version_id}:
            raise HTTPException(409, '作业状态不允许此操作，请刷新并检查草稿或自动进化设置')
    job.status = 'validating' if job.candidate_version_id else 'queued'
    db.commit()
    return lifecycle.describe(job)


@router.post('/jobs/{identifier}/target')
def choose_target(identifier: str, body: Target, actor=Depends(get_current_user), db=Depends(get_db)):
    job = lifecycle.owned_job(db, actor, identifier, lock=True)
    if job.status != 'awaiting_target' or not job.source_resource_id:
        raise HTTPException(409, '作业状态不允许此操作，请刷新并检查草稿或自动进化设置')
    row = access.owner(db, body.resource_id, actor)
    if row.source_resource_id != job.source_resource_id or row.archived:
        raise HTTPException(403, '目标必须是同来源的本人副本')
    binding = db.query(SkillEvolutionBinding).filter_by(user_id=actor.id, source_resource_id=job.source_resource_id).first()
    if binding:
        binding.target_resource_id = row.id
    else:
        db.add(SkillEvolutionBinding(id=str(uuid4()), user_id=actor.id,
            source_resource_id=job.source_resource_id, target_resource_id=row.id))
    job.status = 'queued'
    db.commit()
    return lifecycle.describe(job)
