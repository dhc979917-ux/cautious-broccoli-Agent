"""Pinned-version sharing, notifications and audited transitions."""
import json
from uuid import uuid4
from fastapi import HTTPException
from app.models.audit_event import AuditEvent
from app.models.notification import Notification
from app.models.user import User
from app.models.skill_hub import SkillHubShare, SkillHubVersion
from app.services.skill_hub import access


def audit(db, actor, action, identifier, changes=None):
    db.add(AuditEvent(actor_id=actor.id, action='skill_hub.'+action, resource_type='skill_hub',
        resource_id=str(identifier), result='success', request_id=str(uuid4()),
        changes_json=json.dumps(changes or {}, ensure_ascii=False)))


def notify(db, user_id, title, content=''):
    db.add(Notification(user_id=user_id, type='skill_hub', title=title[:200], content=content, link='/skills'))


def share(db, actor, identifier, body):
    row = access.owner(db, identifier, actor)
    if row.archived or not row.published_version_id or access.is_blocked(db, row, row.published_version_id):
        raise HTTPException(409, '请先发布自用版本，并恢复归档技能')
    recipient = db.get(User, body['shared_to'])
    if recipient is None or not recipient.is_active or recipient.id == actor.id:
        raise HTTPException(422, '请选择其他有效用户')
    expires = body.get('expires_at')
    if expires and (expires.tzinfo is None or expires <= access.utcnow()):
        raise HTTPException(422, '到期时间必须含时区且晚于当前时间')
    record = db.query(SkillHubShare).filter_by(resource_id=row.id, shared_to=recipient.id).with_for_update().first()
    if record is None:
        record = SkillHubShare(id=str(uuid4()), resource_id=row.id, shared_by=actor.id,
            shared_to=recipient.id, version_id=row.published_version_id)
        db.add(record)
    elif access.active_share(record):
        if record.version_id != row.published_version_id:
            record.proposed_version_id = row.published_version_id
            notify(db, recipient.id, '分享技能有新版待确认', row.title)
    else:
        record.version_id = row.published_version_id
        record.proposed_version_id = None
    record.allow_fork = body.get('allow_fork', False)
    record.expires_at = expires
    record.revoked_at = None
    notify(db, recipient.id, '收到技能分享', row.title+'；分享为固定版本，允许派生时副本不能通过撤销分享收回。')
    audit(db, actor, 'share', row.id, {'recipient_id': recipient.id})
    return record


def accept(db, actor, identifier):
    row = db.query(SkillHubShare).filter_by(id=identifier, shared_to=actor.id).with_for_update().one_or_none()
    if not access.active_share(row) or not row.proposed_version_id:
        raise HTTPException(404, '分享更新不存在或已失效')
    row.version_id = row.proposed_version_id
    row.proposed_version_id = None
    audit(db, actor, 'share_accept', row.resource_id)
    return row


def revoke(db, actor, identifier):
    row = db.query(SkillHubShare).filter_by(id=identifier, shared_by=actor.id).with_for_update().one_or_none()
    if row is None:
        raise HTTPException(404, '分享不存在')
    row.revoked_at = access.utcnow()
    notify(db, row.shared_to, '技能分享已撤销', '不能继续直接访问或使用；合法派生的独立副本保留。')
    audit(db, actor, 'share_revoke', row.resource_id)
    return row
