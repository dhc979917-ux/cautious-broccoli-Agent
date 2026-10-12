"""Independent, data-minimizing administrative audit writes."""

import json
import threading
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.audit_event import AuditEvent


SAFE_VALUES = {"enabled", "is_active", "role", "type"}
_health_lock = threading.Lock()
_health = {'failures': 0, 'last_failure_at': None, 'started_at': datetime.now(timezone.utc).isoformat()}


def record_audit_failure():
    with _health_lock:
        _health['failures'] += 1
        _health['last_failure_at'] = datetime.now(timezone.utc).isoformat()


def audit_health():
    with _health_lock:
        return dict(_health)


def safe_state(field: str, value):
    if value is None:
        return None
    if field in ("enabled", "is_active"):
        return value if isinstance(value, bool) else "other"
    if field == "role":
        return value if value in ("admin", "operator", "viewer") else "other"
    if field == "type":
        return value if value in ("stdio", "http") else "other"
    return "other"


def safe_changes(changes: dict | None) -> str:
    summary = {}
    for key, change in (changes or {}).items():
        if key in SAFE_VALUES and isinstance(change, dict):
            summary[key] = {
                "before": safe_state(key, change.get("before")),
                "after": safe_state(key, change.get("after")),
            }
        elif key in {
            "env", "permissions", "prompt", "model", "allowed_models",
            "agent_grants", "skill_grants", "args", "url",
            "agent_version", "skill_version", "agent_draft", "skill_test", "agent_test",
            "memory_candidate", "memory_review", "memory_publish",
        }:
            summary[key] = "changed"
    return json.dumps(summary, ensure_ascii=False)


def begin_event(*, action: str, resource_type: str, resource_id: str | None,
                request_id: str, trace_id: str | None, ip: str | None) -> int:
    with SessionLocal() as session:
        event = AuditEvent(
            action=action, resource_type=resource_type, resource_id=resource_id,
            request_id=request_id, trace_id=trace_id, ip=ip,
            result="attempt", changes_json="{}",
        )
        session.add(event)
        session.commit()
        return event.id


def complete_event(event_id: int, *, actor_id: int | None, http_status: int,
                   changes: dict | None = None) -> None:
    if http_status in (401, 403):
        result = "denied"
    elif http_status >= 400:
        result = "failed"
    else:
        result = "succeeded"
    with SessionLocal() as session:
        event = session.get(AuditEvent, event_id)
        if not event:
            raise RuntimeError("Audit event reservation not found")
        event.actor_id = actor_id
        event.http_status = http_status
        event.result = result
        event.changes_json = safe_changes(changes)
        session.commit()
