from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.operations import GovernanceCleanup, GovernancePolicy
from app.models.user import User
from app.services import operations
from app.services.organization_access import has_permission


router = APIRouter(prefix='/admin/operations', tags=['Operations governance'])


class PolicyUpdate(BaseModel):
    retention_days: int = Field(365, ge=1, le=3650)
    backlog_threshold: int = Field(100, ge=1, le=10000)
    responsible_user_id: int = Field(gt=0)
    revision: int = Field(ge=0)


class CleanupCreate(BaseModel):
    resource: str = Field(pattern='^(trace|audit|candidate_memory)$')
    cutoff: datetime
    reason: str = Field(min_length=10, max_length=500)


class CleanupReview(BaseModel):
    revision: int = Field(ge=0)
    decision: str = Field(pattern='^(approved|rejected)$')
    reason: str = Field(min_length=10, max_length=500)


def allowed(db, actor, code):
    if not has_permission(actor, code, db=db):
        raise HTTPException(403, '没有全局运营治理权限')


@router.get('/dashboard')
def dashboard(since: datetime | None = None, until: datetime | None = None,
              actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return operations.dashboard(db, actor, since, until)


@router.get('/traces')
def traces(since: datetime | None = None, until: datetime | None = None,
           page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
           trace_id: str | None = Query(None, pattern='^[a-f0-9]{32}$'),
           result: str | None = Query(None, pattern='^(ok|error)$'),
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    operations.require_view(db, actor, 'ops_manage.traces')
    start, end = operations.window(since, until)
    records, samples = operations.trace_records(db, actor, start, end)
    filtered = [row for row in records if (trace_id is None or row['trace_id'] == trace_id) and (result is None or row['status'] == result)]
    items = [{key: value for key, value in row.items() if key != 'spans'} for row in filtered[(page - 1) * page_size:page * page_size]]
    return {'items': items, 'total': len(filtered), 'page': page, 'samples': samples,
            'timezone': 'Asia/Shanghai', 'since': start.isoformat(), 'until': end.isoformat()}


@router.get('/traces/{trace_id}')
def trace_detail(trace_id: str, since: datetime | None = None, until: datetime | None = None,
                 actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    operations.require_view(db, actor, 'ops_manage.traces')
    if not operations.TRACE_ID.fullmatch(trace_id):
        raise HTTPException(422, '调用链ID格式错误')
    start, end = operations.window(since, until)
    records, samples = operations.trace_records(db, actor, start, end)
    row = next((row for row in records if row['trace_id'] == trace_id), None)
    if row is None:
        raise HTTPException(404, '调用链不存在、无权限或已超出查询范围')
    return {**row, 'samples': samples, 'note': '只显示实际留存的安全元数据；历史调用缺版本不补造。'}


@router.get('/pending')
def pending(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    operations.require_view(db, actor, 'ops_manage.view')
    owners = operations.visible_owner_ids(db, actor, 'ops_manage.view')
    from app.models.tool_governance import ToolApproval
    from app.models.agent_governance import AgentVersion
    from app.models.skill_governance import SkillVersion
    from app.models.memory_governance import MemoryFusionTask, MemoryIndexJob
    items = [{'type': 'tool_approval', 'id': row.id, 'owner_id': row.user_id, 'status': row.status,
              'href': f'/chat/{row.session_id}'} for row in db.query(ToolApproval).filter(
        ToolApproval.user_id.in_(owners), ToolApproval.status == 'pending',
        ToolApproval.expires_at > datetime.now(timezone.utc)).limit(50)]
    memory_owners = operations.visible_owner_ids(db, actor, 'memory_manage.view')
    items.extend({'type': 'memory', 'id': row.id, 'owner_id': row.user_id, 'status': row.status,
                  'href': f'/admin/memories?user_id={row.user_id}&task_id={row.id}'}
                 for row in db.query(MemoryFusionTask).filter(MemoryFusionTask.user_id.in_(memory_owners),
                     MemoryFusionTask.status.in_(('review', 'failed', 'index_pending'))).limit(50))
    items.extend({'type': 'index', 'id': row.id, 'owner_id': row.user_id, 'status': row.status,
                  'href': f'/admin/memories?user_id={row.user_id}'}
                 for row in db.query(MemoryIndexJob).filter(MemoryIndexJob.user_id.in_(memory_owners), MemoryIndexJob.status != 'done').limit(50))
    if actor.role == 'admin':
        items.extend({'type': 'agent_draft', 'id': row.id, 'status': row.test_status, 'href': f'/admin/agents/{row.agent_id}'}
                     for row in db.query(AgentVersion).filter_by(state='draft').limit(50))
        items.extend({'type': 'skill_draft', 'id': row.id, 'status': row.test_status, 'href': f'/admin/skills/{row.skill_id}'}
                     for row in db.query(SkillVersion).filter_by(status='draft').limit(50))
    return {'items': items, 'limit_per_category': 50, 'note': '各类别最多50项，完整队列在对应资源工作台；入口仍复核操作权限。'}


@router.get('/gateway-tests')
def gateway_tests(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    operations.require_view(db, actor, 'ops_manage.view')
    from app.models.model_governance import ModelTestRun
    owners = operations.visible_owner_ids(db, actor, 'ops_manage.view')
    return [{'id': row.id, 'model_id': row.model_id, 'actor_id': row.actor_id,
             'trace_id': row.trace_id, 'outcome': row.outcome, 'error_code': row.error_code,
             'duration_ms': row.duration_ms, 'tested_at': row.tested_at}
            for row in db.query(ModelTestRun).filter(ModelTestRun.actor_id.in_(owners)).order_by(ModelTestRun.id.desc()).limit(100)]


@router.get('/policies')
def policies(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed(db, actor, 'ops_manage.policy')
    rows = {row.resource: row for row in db.query(GovernancePolicy).filter_by(scope_type='global', scope_id=0)}
    return [{'resource': resource, 'retention_days': rows[resource].retention_days if resource in rows else 365,
        'backlog_threshold': rows[resource].backlog_threshold if resource in rows else operations.DEFAULT_THRESHOLDS.get(resource, 100),
        'responsible_user_id': rows[resource].responsible_user_id if resource in rows else None,
        'revision': rows[resource].revision if resource in rows else 0, 'automatic_delete': False}
        for resource in sorted(operations.RESOURCES)]


@router.put('/policies/{resource}')
def save_policy(resource: str, data: PolicyUpdate, request: Request,
                actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed(db, actor, 'ops_manage.policy')
    if resource not in operations.RESOURCES or db.get(User, data.responsible_user_id) is None:
        raise HTTPException(422, '资源类别或责任人无效')
    row = db.query(GovernancePolicy).filter_by(resource=resource, scope_type='global', scope_id=0).with_for_update().first()
    if row is None:
        if data.revision != 0:
            raise HTTPException(409, '政策修订号冲突')
        row = GovernancePolicy(resource=resource, scope_type='global', scope_id=0, revision=0)
        db.add(row)
    elif row.revision != data.revision:
        raise HTTPException(409, '政策已被修改')
    for key in ('retention_days', 'backlog_threshold', 'responsible_user_id'):
        setattr(row, key, getattr(data, key))
    row.revision += 1
    db.commit()
    request.state.audit_changes = {'permissions': 'governance_policy'}
    return {'revision': row.revision, 'automatic_delete': False}


@router.get('/cleanup')
def cleanup_list(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed(db, actor, 'ops_manage.cleanup')
    return [{'id': row.id, 'resource': row.resource, 'cutoff': row.cutoff, 'status': row.status,
        'revision': row.revision, 'target_count': len(row.targets), 'requested_by': row.requested_by,
        'reviewed_by': row.reviewed_by} for row in db.query(GovernanceCleanup).order_by(GovernanceCleanup.created_at.desc()).limit(100)]


@router.post('/cleanup')
def cleanup_request(data: CleanupCreate, request: Request, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed(db, actor, 'ops_manage.cleanup')
    targets = operations.cleanup_preview(db, data.resource, data.cutoff)
    if not targets:
        raise HTTPException(409, '没有超过保护期的可清理对象')
    row = GovernanceCleanup(id=str(uuid4()), resource=data.resource, cutoff=data.cutoff,
        targets=targets, requested_by=actor.id, reason=data.reason)
    db.add(row)
    db.commit()
    request.state.audit_changes = {'permissions': 'cleanup_requested'}
    return {'id': row.id, 'status': row.status, 'target_count': len(targets), 'revision': row.revision}


@router.post('/cleanup/{cleanup_id}/review')
def cleanup_review(cleanup_id: UUID, data: CleanupReview, request: Request,
                   actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed(db, actor, 'ops_manage.cleanup')
    row = db.query(GovernanceCleanup).filter_by(id=str(cleanup_id)).with_for_update().first()
    if not row or row.status != 'pending' or row.revision != data.revision:
        raise HTTPException(409, '清理申请不存在或已发生变化')
    if row.requested_by == actor.id:
        raise HTTPException(409, '清理申请人与审核人必须分离')
    row.reviewed_by = actor.id
    row.review_reason = data.reason
    row.reviewed_at = datetime.now(timezone.utc)
    row.status = data.decision
    row.revision += 1
    db.commit()
    request.state.audit_changes = {'permissions': 'cleanup_reviewed'}
    return {'status': row.status, 'revision': row.revision}


@router.post('/cleanup/{cleanup_id}/execute')
def cleanup_execute(cleanup_id: UUID, request: Request, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    allowed(db, actor, 'ops_manage.cleanup')
    row = db.query(GovernanceCleanup).filter_by(id=str(cleanup_id)).with_for_update().first()
    if row is None or row.status not in ('approved', 'completed'):
        raise HTTPException(409, '清理必须先经分离审核')
    if row.status == 'approved':
        operations.execute_cleanup(db, row)
    request.state.audit_changes = {'permissions': 'cleanup_executed'}
    return {'status': row.status, 'revision': row.revision}
