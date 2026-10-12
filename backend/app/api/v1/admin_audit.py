from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.services.operations import require_view, visible_owner_ids, window
from app.services.organization_access import has_permission
from app.database import get_db
from app.models.audit_event import AuditEvent
from app.models.user import User


router = APIRouter(prefix="/admin/audit", tags=["Administrative audit"])


@router.get("/events")
def list_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    resource_type: str | None = None,
    actor_id: int | None = None,
    action: str | None = Query(None, max_length=80),
    result: str | None = Query(None, pattern='^(attempt|denied|failed|succeeded)$'),
    request_id: str | None = Query(None, max_length=80),
    trace_id: str | None = Query(None, max_length=80),
    since: datetime | None = None,
    until: datetime | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_view(db, current_user, 'ops_manage.audit')
    start, end = window(since, until)
    query = db.query(AuditEvent).filter(AuditEvent.created_at >= start, AuditEvent.created_at < end)
    if not has_permission(current_user, 'ops_manage.audit', db=db):
        query = query.filter(AuditEvent.actor_id.in_(visible_owner_ids(db, current_user, 'ops_manage.audit')),
            AuditEvent.resource_type.notin_(('memories', 'credentials')))
    if resource_type:
        query = query.filter(AuditEvent.resource_type == resource_type)
    if actor_id is not None:
        query = query.filter(AuditEvent.actor_id == actor_id)
    for column, value in ((AuditEvent.action, action), (AuditEvent.result, result),
                           (AuditEvent.request_id, request_id), (AuditEvent.trace_id, trace_id)):
        if value:
            query = query.filter(column == value)
    total = query.count()
    rows = query.order_by(AuditEvent.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [
            {
                "id": row.id, "actor_id": row.actor_id, "action": row.action,
                "resource_type": row.resource_type, "resource_id": row.resource_id,
                "result": row.result, "http_status": row.http_status,
                "request_id": row.request_id, "trace_id": row.trace_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "total": total, "page": page, "page_size": page_size,
        "timezone": 'Asia/Shanghai', 'since': start.isoformat(), 'until': end.isoformat(),
        'note': '仅脱敏审计元数据；不提供Prompt、记忆正文、工具参数或批量原文导出',
    }
