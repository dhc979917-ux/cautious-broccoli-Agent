"""Bounded, owner-scoped operational metadata without prompts or tool payloads."""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from app.core.tracing import list_trace_files, trace_dir_path
from app.models.agent import Agent
from app.models.agent_governance import AgentVersion
from app.models.audit_event import AuditEvent
from app.models.knowledge import KnowledgeDocument, KnowledgeBase
from app.models.memory_governance import MemoryFusionTask, MemoryIndexJob
from app.models.message import Message
from app.models.model_governance import GatewayModelEntry, ModelTestRun
from app.models.operations import GovernanceCleanup, GovernancePolicy
from app.models.session import Session as Conversation
from app.models.skill import Skill
from app.models.skill_governance import SkillVersion
from app.models.mcp_server import McpServer
from app.models.tool_governance import ToolApproval
from app.models.user import User
from app.services.organization_access import has_permission, visible_users


LOCAL_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
IDENTIFIER = re.compile(r'^[A-Za-z0-9_.:/-]{1,200}$')
TRACE_ID = re.compile(r'^[a-f0-9]{32}$')
SAFE_ATTRIBUTES = {'user.id', 'session.id', 'agent.id', 'agent.version_id', 'skill.version_ids',
    'gen_ai.request.model', 'gen_ai.tool.name', 'gen_ai.usage.input_tokens',
    'gen_ai.usage.output_tokens', 'request.id'}
RESOURCES = {'trace', 'audit', 'candidate_memory', 'published_memory', 'tool_approval',
    'gateway', 'mcp', 'index', 'failure_rate'}
MAX_SPANS = 50000
DEFAULT_THRESHOLDS = {'gateway': 1, 'mcp': 1, 'index': 1, 'audit': 1,
                      'tool_approval': 20, 'failure_rate': 10}


def window(since=None, until=None):
    end = until or datetime.now(LOCAL_TZ)
    start = since or end.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.replace(tzinfo=LOCAL_TZ) if start.tzinfo is None else start.astimezone(LOCAL_TZ)
    end = end.replace(tzinfo=LOCAL_TZ) if end.tzinfo is None else end.astimezone(LOCAL_TZ)
    if start >= end or end - start > timedelta(days=90):
        raise HTTPException(422, '统计时间窗口须为1秒至90天')
    return start, end


def visible_owner_ids(db, actor, permission):
    return {row.id for row in visible_users(db, actor, permission).all()}


def require_view(db, actor, permission):
    if not has_permission(actor, permission, db=db) and not visible_owner_ids(db, actor, permission):
        raise HTTPException(403, '没有运营数据访问权限')


def safe_attributes(attributes):
    result = {}
    if not isinstance(attributes, dict):
        return result
    for key, value in attributes.items():
        if key not in SAFE_ATTRIBUTES:
            continue
        if key == 'skill.version_ids':
            try:
                versions = json.loads(value) if isinstance(value, str) else value
                if isinstance(versions, dict) and len(versions) <= 100:
                    result[key] = {str(skill): str(version) for skill, version in versions.items()
                        if str(skill).isdigit() and re.fullmatch(r'[a-f0-9-]{36}', str(version))}
            except (ValueError, TypeError):
                pass
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            result[key] = value
        elif isinstance(value, str) and IDENTIFIER.fullmatch(value):
            result[key] = value
    return result


def trace_records(db, actor, start, end, permission='ops_manage.traces', directory=None):
    owners = visible_owner_ids(db, actor, permission)
    global_access = has_permission(actor, permission, db=db)
    grouped = {}
    scanned = malformed = 0
    truncated = False
    for path in reversed(list_trace_files(directory or trace_dir_path())):
        if path.is_symlink():
            continue
        if path.name.startswith('traces-'):
            day = datetime.strptime(path.stem.split('-')[1], '%Y%m%d').date()
            if day < start.date() - timedelta(days=1) or day > end.date() + timedelta(days=1):
                continue
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    if scanned >= MAX_SPANS:
                        truncated = True
                        break
                    scanned += 1
                    if len(line) > 2_000_000:
                        malformed += 1
                        continue
                    try:
                        raw = json.loads(line)
                        stamp = datetime.fromisoformat(raw['start_time'])
                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)
                        if not start <= stamp < end or not TRACE_ID.fullmatch(str(raw.get('trace_id', ''))):
                            continue
                        attributes = safe_attributes(raw.get('attributes'))
                        duration = float(raw.get('duration_ms') or 0)
                        if not math.isfinite(duration) or duration < 0:
                            duration = 0
                        name = str(raw.get('name') or '')
                        kind = 'model' if name.startswith('chat') else 'tool' if name.startswith('execute_tool') else 'agent' if name.startswith(('invoke_agent', 'agent_run')) else 'span'
                        grouped.setdefault(raw['trace_id'], []).append({'span_id': raw.get('span_id'),
                            'parent_span_id': raw.get('parent_span_id'), 'kind': kind,
                            'start_time': stamp.isoformat(), 'duration_ms': round(duration, 2),
                            'status': 'error' if raw.get('status') == 'ERROR' else 'ok', 'attributes': attributes})
                    except (ValueError, KeyError, TypeError):
                        malformed += 1
        except OSError:
            malformed += 1
        if truncated:
            break
    records = []
    for trace_id, spans in grouped.items():
        metadata = {}
        for span in sorted(spans, key=lambda item: item['kind'] != 'agent'):
            for key, value in span['attributes'].items():
                metadata.setdefault(key, value)
        owner_id = metadata.get('user.id')
        if not isinstance(owner_id, int) or owner_id <= 0:
            session_id = metadata.get('session.id')
            session = db.get(Conversation, session_id) if isinstance(session_id, int) else None
            owner_id = session.user_id if session else None
        if not global_access and owner_id not in owners:
            continue
        records.append({'trace_id': trace_id, 'user_id': owner_id, 'metadata': metadata,
            'started_at': min(span['start_time'] for span in spans),
            'status': 'error' if any(span['status'] == 'error' for span in spans) else 'ok',
            'model_calls': sum(span['kind'] == 'model' for span in spans),
            'tool_calls': sum(span['kind'] == 'tool' for span in spans), 'spans': spans})
    records.sort(key=lambda item: item['started_at'], reverse=True)
    return records, {'scanned_spans': scanned, 'malformed_spans': malformed, 'truncated': truncated,
        'unknown_owner_traces': sum(row['user_id'] is None for row in records),
        'source': 'retained OTel JSONL spans; no prompt or tool body returned'}


def dashboard(db, actor, since=None, until=None):
    require_view(db, actor, 'ops_manage.view')
    start, end = window(since, until)
    owners = visible_owner_ids(db, actor, 'ops_manage.view')
    rows = db.query(Message, Conversation.user_id).join(Conversation, Message.session_id == Conversation.id).filter(
        Conversation.user_id.in_(owners), Message.created_at >= start, Message.created_at < end,
        Message.role.in_(('agent', 'assistant'))).all()
    tokens = {'input': 0, 'output': 0, 'complete_samples': 0, 'missing_samples': 0}
    for message, owner_id in rows:
        try:
            usage = json.loads(message.metadata_json or '{}').get('token_usage') or {}
            if any(type(usage.get(key)) is not int or usage[key] < 0 for key in ('input_tokens', 'output_tokens')):
                tokens['missing_samples'] += 1
                continue
            tokens['input'] += usage['input_tokens']
            tokens['output'] += usage['output_tokens']
            tokens['complete_samples'] += 1
        except (ValueError, TypeError, AttributeError):
            tokens['missing_samples'] += 1
    trace_rows, samples = trace_records(db, actor, start, end, permission='ops_manage.view')
    global_view = has_permission(actor, 'ops_manage.view', db=db)
    resources = {'users': len(owners)}
    if global_view:
        resources.update({'agents': db.query(Agent).count(), 'skills': db.query(Skill).count(),
            'models': db.query(GatewayModelEntry).count(), 'mcp': db.query(McpServer).count()})
    memory_owners = visible_owner_ids(db, actor, 'memory_manage.view')
    pending = {
        'tool_approvals': db.query(ToolApproval).filter(ToolApproval.user_id.in_(owners), ToolApproval.status == 'pending', ToolApproval.expires_at > end).count(),
        'memory_review': db.query(MemoryFusionTask).filter(MemoryFusionTask.user_id.in_(memory_owners), MemoryFusionTask.status == 'review').count(),
        'memory_failed': db.query(MemoryFusionTask).filter(MemoryFusionTask.user_id.in_(memory_owners), MemoryFusionTask.status == 'failed').count(),
        'memory_backlog': db.query(MemoryFusionTask).filter(MemoryFusionTask.user_id.in_(memory_owners), MemoryFusionTask.status.in_(('queued', 'generating', 'review'))).count(),
        'index_pending': db.query(MemoryIndexJob).filter(MemoryIndexJob.user_id.in_(memory_owners), MemoryIndexJob.status != 'done').count(),
    }
    if global_view:
        pending.update({'agent_drafts': db.query(AgentVersion).filter_by(state='draft').count(),
                        'skill_drafts': db.query(SkillVersion).filter_by(status='draft').count()})
    gateway_errors = db.query(ModelTestRun).filter(ModelTestRun.tested_at >= start, ModelTestRun.tested_at < end,
        ModelTestRun.outcome == 'error', ModelTestRun.actor_id.in_(owners)).count()
    policies = db.query(GovernancePolicy).filter_by(scope_type='global').all() if global_view else []
    policy_map = {row.resource: row for row in policies}
    failure_rate = round(100 * sum(row['status'] == 'error' for row in trace_rows) / len(trace_rows), 2) if trace_rows else None
    alerts = []
    for code, value, default, href in (
        ('gateway', gateway_errors, 1, '/admin/operations?tab=gateway'),
        ('tool_approval', pending['tool_approvals'], 20, '/admin/operations?tab=pending'),
        ('candidate_memory', pending['memory_backlog'], 100, '/admin/memories'),
        ('index', pending['index_pending'], 1, '/admin/memories'),
        ('failure_rate', failure_rate, 10, '/admin/traces'),
    ):
        policy = policy_map.get(code)
        threshold = policy.backlog_threshold if policy else default
        alerts.append({'code': code, 'value': value, 'threshold': threshold,
            'active': value is not None and value >= threshold,
            'responsible_user_id': policy.responsible_user_id if policy else None, 'href': href})
    if global_view:
        from app.services.audit_service import audit_health
        audit = audit_health()
        alerts.append({'code': 'audit', 'value': audit['failures'], 'threshold': 1,
            'active': audit['failures'] > 0, 'responsible_user_id': policy_map.get('audit').responsible_user_id if policy_map.get('audit') else None,
            'href': '/admin/audit', 'sample': 'current process since startup'})
        mcp_errors = sum(span['status'] == 'error' for row in trace_rows for span in row['spans']
                         if span['kind'] == 'tool')
        alerts.append({'code': 'mcp', 'value': mcp_errors if trace_rows else None,
            'threshold': policy_map.get('mcp').backlog_threshold if policy_map.get('mcp') else 1,
            'active': mcp_errors >= (policy_map.get('mcp').backlog_threshold if policy_map.get('mcp') else 1),
            'responsible_user_id': policy_map.get('mcp').responsible_user_id if policy_map.get('mcp') else None,
            'href': '/admin/traces?result=error', 'sample': 'Tool失败仅统计保留Trace；连接失败另外从MCP测试审计下钻'})
    recent_query = db.query(AuditEvent).filter(AuditEvent.created_at >= start, AuditEvent.created_at < end)
    if not has_permission(actor, 'ops_manage.audit', db=db):
        recent_query = recent_query.filter(AuditEvent.actor_id == actor.id, AuditEvent.resource_type != 'memories')
    recent = [{'id': row.id, 'action': row.action, 'resource_type': row.resource_type,
        'resource_id': row.resource_id, 'result': row.result, 'request_id': row.request_id,
        'trace_id': row.trace_id, 'created_at': row.created_at} for row in recent_query.order_by(AuditEvent.id.desc()).limit(8)]
    return {'timezone': 'Asia/Shanghai', 'since': start.isoformat(), 'until': end.isoformat(),
        'updated_at': datetime.now(LOCAL_TZ).isoformat(), 'resources': resources, 'pending': pending,
        'usage': {'agent_replies': len(rows), 'active_users': len({owner_id for message, owner_id in rows}),
            'model_calls': sum(row['model_calls'] for row in trace_rows), 'tool_calls': sum(row['tool_calls'] for row in trace_rows),
            'failure_rate': failure_rate, 'tokens': tokens, 'cost': None, 'cost_note': '暂无法核算：未建立可信完整的价格与调用用量映射'},
        'samples': samples, 'alerts': alerts, 'recent': recent,
        'note': 'Agent回复来自会话数据库；模型及Tool调用来自留存Trace，缺样本不等于零故障。'}


def cleanup_preview(db, resource, cutoff):
    now = datetime.now(timezone.utc)
    stamp = cutoff.replace(tzinfo=LOCAL_TZ) if cutoff.tzinfo is None else cutoff
    policy = db.query(GovernancePolicy).filter_by(resource=resource, scope_type='global', scope_id=0).first()
    days = policy.retention_days if policy else 365
    if stamp > now - timedelta(days=days):
        raise HTTPException(409, '清理截止时间仍处于留存保护期')
    if resource == 'trace':
        targets = []
        for path in list_trace_files(trace_dir_path()):
            if path.is_symlink() or path.name == 'traces.jsonl':
                continue
            day = datetime.strptime(path.stem.split('-')[1], '%Y%m%d').replace(tzinfo=LOCAL_TZ)
            if day + timedelta(days=1) < stamp and path.stat().st_size <= 100_000_000:
                targets.append({'id': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return targets[:100]
    if resource == 'audit':
        rows = db.query(AuditEvent).filter(AuditEvent.created_at < stamp).order_by(AuditEvent.id).limit(100).all()
        return [{'id': row.id, 'request_id': row.request_id} for row in rows]
    if resource == 'candidate_memory':
        rows = db.query(MemoryFusionTask).filter(MemoryFusionTask.created_at < stamp,
            MemoryFusionTask.status.in_(('failed', 'rejected'))).limit(100).all()
        return [{'id': row.id, 'revision': row.revision} for row in rows]
    raise HTTPException(422, '已发布记忆只能在记忆工作台逐项审批处置')


def execute_cleanup(db, row):
    if row.resource == 'trace':
        root = trace_dir_path().resolve()
        for target in row.targets:
            path = root / target['id']
            if path.parent != root or path.is_symlink() or not re.fullmatch(r'traces-\d{8}\.jsonl', path.name):
                raise HTTPException(409, '清理目标不安全')
            if not path.exists():
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != target['sha256']:
                raise HTTPException(409, 'Trace文件在审批后发生变化')
            archive = root / '.approved_archive' / row.id
            archive.mkdir(parents=True, exist_ok=True)
            path.replace(archive / path.name)
    elif row.resource == 'audit':
        for target in row.targets:
            event = db.get(AuditEvent, target['id'])
            if event and event.request_id != target['request_id']:
                raise HTTPException(409, '审计清理目标变化')
        for target in row.targets:
            event = db.get(AuditEvent, target['id'])
            if event:
                event.changes_json = '{}'
                event.ip = None
    elif row.resource == 'candidate_memory':
        from app.models.memory_governance import MemoryFusionItem
        from app.services.memory_governance import seal
        for target in row.targets:
            task = db.get(MemoryFusionTask, target['id'])
            if not task or task.revision != target['revision'] or task.status not in ('failed', 'rejected'):
                raise HTTPException(409, '候选已变化，请重新申请')
        for target in row.targets:
            task = db.get(MemoryFusionTask, target['id'])
            task.source_ciphertext = seal('{}', task.user_id)
            task.status = 'expired'
            task.revision += 1
            for item in db.query(MemoryFusionItem).filter_by(task_id=task.id):
                item.content_ciphertext = seal('留存期满，已审批清理', task.user_id)
                item.evidence = seal('[]', task.user_id)
    row.status = 'completed'
    row.revision += 1
    db.commit()
