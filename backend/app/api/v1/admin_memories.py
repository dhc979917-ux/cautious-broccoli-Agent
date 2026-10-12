"""Explicitly scoped administrative memory review, outside the personal KB API."""

import json
from datetime import datetime
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.core.deps import get_current_user
from app.database import get_db
from app.models.memory_governance import MemoryEntry, MemoryExtractionMethod, MemoryFusionItem, MemoryFusionTask
from app.models.user import User
from app.services import memory_governance as governance
from app.services.organization_access import ScopeTarget, has_permission, visible_users


router = APIRouter(prefix="/admin/memories", tags=["用户记忆"])


class CreateTask(BaseModel):
    user_id: int = Field(gt=0)
    entry_ids: list[str] = Field(min_length=1, max_length=100)
    method_code: str = "summary"
    idempotency_key: str = Field(min_length=8, max_length=64)
    max_candidates: int = Field(10, ge=1, le=20)
    confidence_hint: float = Field(0.70, ge=0, le=1)
    deduplicate: bool = True
    time_hints: bool = True
    replace_sources: bool = False


class Review(BaseModel):
    revision: int = Field(ge=0)
    decision: Literal['accepted', 'rejected', 'keep_both']
    content: str | None = Field(None, max_length=4000)
    reason: str | None = Field(None, max_length=500)


class Publish(BaseModel):
    revision: int = Field(ge=0)


class MethodCreate(BaseModel):
    user_id: int = Field(gt=0)
    code: str = Field(pattern=r'^custom_[a-z0-9_]{3,40}$')
    title: str = Field(min_length=2, max_length=100)
    instructions: str = Field(min_length=10, max_length=2000)


class Lifecycle(BaseModel):
    user_id: int = Field(gt=0)
    action: str
    revision: int = Field(ge=0)


class MemoryPolicy(BaseModel):
    user_id: int = Field(gt=0)
    scope_type: str = Field(pattern='^(user|department)$')
    retention_days: int = Field(365, ge=1, le=3650)
    backlog_threshold: int = Field(100, ge=1, le=1000)
    responsible_user_id: int = Field(gt=0)
    revision: int = Field(ge=0)


@router.get('/policy')
def memory_policy(user_id: int = Query(gt=0), actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owner = authorized(db, actor, user_id, 'memory_manage.view')
    from app.models.operations import GovernancePolicy
    for scope_type, scope_id in (('user', user_id), ('department', owner.department_id)):
        if scope_id is None:
            continue
        row = db.query(GovernancePolicy).filter_by(resource='published_memory', scope_type=scope_type, scope_id=scope_id).first()
        if row:
            return {'scope_type': row.scope_type, 'retention_days': row.retention_days,
                    'backlog_threshold': row.backlog_threshold, 'responsible_user_id': row.responsible_user_id, 'revision': row.revision}
    return {'scope_type': 'user', 'retention_days': 365, 'backlog_threshold': 100,
            'responsible_user_id': None, 'revision': 0, 'automatic_delete': False}


@router.put('/policy')
def save_memory_policy(data: MemoryPolicy, request: Request, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owner = authorized(db, actor, data.user_id, 'memory_manage.policy')
    if data.scope_type == 'department' and actor.role != 'admin':
        raise HTTPException(403, '部门留存政策由超级管理员维护')
    if db.get(User, data.responsible_user_id) is None:
        raise HTTPException(422, '责任人不存在')
    scope_id = data.user_id if data.scope_type == 'user' else owner.department_id
    if scope_id is None:
        raise HTTPException(409, '用户尚未关联部门')
    from app.models.operations import GovernancePolicy
    row = db.query(GovernancePolicy).filter_by(resource='published_memory', scope_type=data.scope_type, scope_id=scope_id).with_for_update().first()
    if row is None:
        if data.revision != 0:
            raise HTTPException(409, '政策修订号不一致')
        row = GovernancePolicy(resource='published_memory', scope_type=data.scope_type, scope_id=scope_id, revision=0)
        db.add(row)
    elif row.revision != data.revision:
        raise HTTPException(409, '政策已变化')
    row.retention_days = data.retention_days
    row.backlog_threshold = data.backlog_threshold
    row.responsible_user_id = data.responsible_user_id
    row.revision += 1
    db.commit()
    request.state.audit_changes = {'memory_candidate': 'retention_policy'}
    return {'revision': row.revision, 'automatic_delete': False}


def authorized(db: Session, actor: User, user_id: int, permission: str) -> User:
    owner = db.get(User, user_id)
    if owner is None:
        raise HTTPException(404, "用户不存在")
    if not has_permission(actor, permission, db=db, target=ScopeTarget(
        user_id=user_id, department_id=owner.department_id,
        resource_type="user", resource_id=user_id,
    )):
        raise HTTPException(403, "无权查看或治理该用户的记忆")
    return owner


def _task(db: Session, task_id: str, actor: User, action: str) -> MemoryFusionTask:
    task = db.get(MemoryFusionTask, task_id, with_for_update=action != "view")
    if task is None:
        raise HTTPException(404, "候选任务不存在")
    authorized(db, actor, task.user_id, "memory_manage." + action)
    return task


@router.get('/users')
def users(search: str = Query('', max_length=100), page: int = Query(1, ge=1),
          actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = visible_users(db, actor, 'memory_manage.view')
    if search:
        query = query.filter(or_(User.account.contains(search, autoescape=True), User.username.contains(search, autoescape=True)))
    return {'total': query.count(), 'items': [{'id': owner.id, 'account': owner.account or owner.username, 'username': owner.username}
        for owner in query.order_by(User.id).offset((page - 1) * 100).limit(100).all()]}


@router.get("/methods")
def methods(user_id: int = Query(gt=0), actor: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    authorized(db, actor, user_id, "memory_manage.view")
    governance.seed_methods(db)
    result = {}
    for row in db.query(MemoryExtractionMethod).filter(or_(MemoryExtractionMethod.user_id.is_(None),
            MemoryExtractionMethod.user_id == user_id)).order_by(
            MemoryExtractionMethod.code, MemoryExtractionMethod.revision.desc()).all():
        if row.code not in result:
            result[row.code] = {'code': row.code, 'revision': row.revision,
                'title': governance.METHOD_TITLES.get(row.code, row.title)}
    return list(result.values())


@router.get('/access')
def memory_access(user_id: int = Query(gt=0), actor: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    owner = authorized(db, actor, user_id, 'memory_manage.view')
    target = ScopeTarget(user_id=user_id, department_id=owner.department_id)
    return {action: has_permission(actor, 'memory_manage.' + action, db=db, target=target)
            for action in ('create', 'review', 'publish')}


@router.post('/methods')
def create_method(data: MethodCreate, request: Request, actor: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    authorized(db, actor, data.user_id, 'memory_manage.create')
    if governance.SENSITIVE.search(data.instructions):
        raise HTTPException(422, '方法不得包含凭据或敏感身份号码')
    from uuid import uuid4
    highest = db.query(MemoryExtractionMethod.revision).filter_by(code=data.code).order_by(
        MemoryExtractionMethod.revision.desc()).first()
    existing = db.query(MemoryExtractionMethod).filter_by(code=data.code).first()
    if existing and existing.user_id != data.user_id:
        raise HTTPException(403, '不能修改其他数据范围的萃取方法')
    row = MemoryExtractionMethod(id=str(uuid4()), code=data.code, revision=highest[0] + 1 if highest else 1,
        title=data.title, instructions_ciphertext=governance.seal(data.instructions, 0), created_by=actor.id, user_id=data.user_id)
    db.add(row)
    db.commit()
    request.state.audit_changes = {'memory_candidate': 'method_version_created'}
    return {'code': row.code, 'revision': row.revision, 'title': row.title}


@router.get("/entries")
def entries(user_id: int = Query(gt=0), page: int = Query(1, ge=1),
            page_size: int = Query(20, ge=1, le=100), keyword: str = Query("", max_length=100),
            source_type: str | None = None, status: str | None = None, agent_id: int | None = None,
            since: datetime | None = None, until: datetime | None = None,
            actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    authorized(db, actor, user_id, "memory_manage.view")
    governance.backfill(db, user_id=user_id)
    query = db.query(MemoryEntry).filter_by(user_id=user_id)
    if keyword:
        query = query.filter(MemoryEntry.vault_path.contains(keyword))
    if source_type:
        query = query.filter(MemoryEntry.source_type == source_type)
    if status:
        query = query.filter(MemoryEntry.status == status)
    if agent_id is not None:
        query = query.filter(MemoryEntry.source_agent_id == agent_id)
    if since:
        query = query.filter(MemoryEntry.created_at >= since)
    if until:
        query = query.filter(MemoryEntry.created_at < until)
    total = query.count()
    rows = query.order_by(MemoryEntry.created_at.desc(), MemoryEntry.id).offset(
        (page - 1) * page_size).limit(page_size).all()
    return {"items": [{"id": row.id, "kb_id": row.kb_id, "path": row.vault_path,
                       "source_type": row.source_type, "status": row.status, "revision": row.revision,
                       "index_status": row.index_status, "created_at": row.created_at,
                       'source_agent_id': row.source_agent_id, 'source_session_id': row.source_session_id}
                      for row in rows], "total": total, "page": page}


@router.get("/entries/{entry_id}")
def entry_detail(entry_id: str, user_id: int = Query(gt=0),
                 actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    authorized(db, actor, user_id, "memory_manage.view")
    row = db.get(MemoryEntry, entry_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(403, "记忆不属于选定用户")
    if row.status in ('disabled', 'deleted', 'superseded'):
        from app.models.knowledge import KnowledgeBase
        kb = db.get(KnowledgeBase, row.kb_id)
        body = governance.safe_path(kb, f'.memory_governance/{row.id}.bak').read_text(encoding='utf-8')
        if governance._hash(body) != row.content_hash:
            raise HTTPException(409, '归档内容哈希不一致')
    else:
        body = governance._content(db, row)
    return {"id": row.id, "path": row.vault_path, "content": body,
            "sha256": row.content_hash, "source_type": row.source_type}


@router.post('/entries/{entry_id}/lifecycle')
def lifecycle(entry_id: UUID, data: Lifecycle, request: Request,
              actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    authorized(db, actor, data.user_id, 'memory_manage.create')
    entry = db.get(MemoryEntry, str(entry_id), with_for_update=True)
    if entry is None or entry.user_id != data.user_id:
        raise HTTPException(403, '记忆不属于选定用户')
    task = governance.lifecycle_task(db, entry, data.action, actor.id, data.revision)
    request.state.audit_changes = {'memory_candidate': 'lifecycle_requested'}
    return {'id': task.id, 'status': task.status, 'revision': task.revision}


@router.post("/tasks")
def create_task(data: CreateTask, background: BackgroundTasks, request: Request,
                actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    authorized(db, actor, data.user_id, "memory_manage.create")
    task = governance.manual_task(db, data.user_id, data.entry_ids,
                                  data.method_code, actor.id, data.idempotency_key, options={
                                      'max_candidates': data.max_candidates, 'confidence_hint': data.confidence_hint,
                                      'deduplicate': data.deduplicate, 'time_hints': data.time_hints,
                                      'replace_sources': data.replace_sources, 'conflict_policy': 'manual'})
    if task.status == "queued":
        background.add_task(governance.generate_task, task.id)
    request.state.audit_changes = {"memory_candidate": "created"}
    return {"id": task.id, "status": task.status, "revision": task.revision}


@router.get("/tasks")
def tasks(user_id: int = Query(gt=0), page: int = Query(1, ge=1),
          page_size: int = Query(20, ge=1, le=100),
          status: str | None = None,
          actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    authorized(db, actor, user_id, "memory_manage.view")
    query = db.query(MemoryFusionTask).filter_by(user_id=user_id)
    if status:
        query = query.filter(MemoryFusionTask.status == status)
    return {"total": query.count(), "page": page, "items": [
        {"id": row.id, "status": row.status, "source_type": row.source_type,
         "method_code": row.method_code, "revision": row.revision,
         "error_code": row.error_code, "created_at": row.created_at}
        for row in query.order_by(MemoryFusionTask.created_at.desc()).offset(
            (page - 1) * page_size).limit(page_size).all()]}


@router.get("/tasks/{task_id}")
def task_detail(task_id: UUID, actor: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    task = _task(db, str(task_id), actor, "view")
    if task.status == 'expired':
        return {"id": task.id, "user_id": task.user_id, "status": task.status,
                "revision": task.revision, "source_type": task.source_type,
                "options": task.options, "sources": [], "items": [], "content_removed": True,
                'review_access': {'can_review': False, 'can_publish': False, 'requires_reason': False}}
    items = db.query(MemoryFusionItem).filter_by(task_id=task.id).all()
    sources = json.loads(governance.unseal(task.source_ciphertext, task.user_id))
    owner = db.get(User, task.user_id)
    target = ScopeTarget(user_id=task.user_id, department_id=owner.department_id)
    entries = {entry.id: entry for entry in db.query(MemoryEntry).filter(
        MemoryEntry.id.in_([ref['id'] for ref in task.source_refs])).all()}
    return {"id": task.id, "user_id": task.user_id, "status": task.status,
            "revision": task.revision, "source_type": task.source_type,
            "options": task.options, 'method_code': task.method_code, 'created_at': task.created_at,
            'error_code': task.error_code,
            'review_access': {
                'can_review': has_permission(actor, 'memory_manage.review', db=db, target=target) and (task.requested_by != actor.id or actor.role == 'admin'),
                'can_publish': has_permission(actor, 'memory_manage.publish', db=db, target=target),
                'requires_reason': task.requested_by == actor.id,
                'reason_recorded': bool(task.override_reason),
            },
            "sources": [{"id": ref["id"], "sha256": ref["sha256"],
                         "content": sources[ref["id"]],
                         'path': entries[ref['id']].vault_path if ref['id'] in entries else None,
                         'source_type': entries[ref['id']].source_type if ref['id'] in entries else task.source_type,
                         'session_id': ref.get('session_id'), 'agent_id': ref.get('agent_id')}
                        for ref in task.source_refs],
            "items": [{"id": item.id, "content": governance.unseal(item.content_ciphertext, task.user_id),
                       "source_ids": item.source_ids, "evidence": json.loads(governance.unseal(item.evidence, task.user_id)), "conflict_group": item.conflict_group,
                       "status": item.status, "revision": item.revision,
                       "confidence": float(item.confidence) if item.confidence is not None else None}
                      for item in items]}


@router.post('/tasks/{task_id}/retry')
def retry_task(task_id: UUID, data: Publish, background: BackgroundTasks,
               actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _task(db, str(task_id), actor, 'create')
    if task.status != 'failed' or task.revision != data.revision:
        raise HTTPException(409, '仅失败的未发布任务可以重新生成')
    governance.check_sources(db, task)
    task.status = 'queued'
    task.revision += 1
    db.commit()
    background.add_task(governance.generate_task, task.id)
    return {'id': task.id, 'status': task.status, 'revision': task.revision}


@router.put("/tasks/{task_id}/items/{item_id}")
def review(task_id: UUID, item_id: UUID, data: Review, request: Request,
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _task(db, str(task_id), actor, "review")
    if task.requested_by == actor.id and actor.role != "admin":
        raise HTTPException(403, "候选创建人不可自审")
    item = db.get(MemoryFusionItem, str(item_id), with_for_update=True)
    if item is None or item.task_id != task.id:
        raise HTTPException(404, "候选条目不存在")
    row = governance.review_item(db, task, item, actor.id,
                                  data.revision, data.decision, data.content, data.reason)
    request.state.audit_changes = {"memory_review": data.decision}
    return {"id": row.id, "status": row.status, "revision": row.revision}


@router.post("/tasks/{task_id}/publish")
async def publish(task_id: UUID, data: Publish, request: Request,
                  actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _task(db, str(task_id), actor, "publish")
    if task.revision != data.revision:
        raise HTTPException(409, "任务修订号已变化")
    jobs = governance.prepare_publish(db, task, actor.id)
    succeeded = 0
    for job in jobs:
        if await governance.reconcile_job(db, job):
            succeeded += 1
    request.state.audit_changes = {"memory_publish": task.status}
    return {"id": task.id, "status": task.status,
            "indexed": succeeded, "total": len(jobs)}
