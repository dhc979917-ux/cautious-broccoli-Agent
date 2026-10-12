"""Server-side ownership, scope, evidence and content rules for learning."""
import json
import re
from pathlib import Path
from fastapi import HTTPException
import frontmatter
from app.config import settings
from app.models.user import User
from app.models.session import Session
from app.models.message import Message
from app.models.skill import Skill
from app.models.skill_hub import SkillHubResource
from app.models.skill_evolution import SkillEvolutionPreference
from app.services.skill_hub import access, content

SECRET = re.compile(r'(?i)(?:bearer\s+|(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*)[^\s,;]+')
TOKEN = re.compile(r'(?<![a-zA-Z0-9])[a-zA-Z0-9_+/=-]{36,}(?![a-zA-Z0-9])')
PRIVATE = re.compile(r'(?:[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?:/app|/mnt|/root|[A-Z]:)[/\\][^\s]+)')
DANGEROUS = re.compile(r'(?i)(?:curl|wget|ssh|scp|sudo|chmod|docker|kubectl|requests\.|http[s]?://|\brm\s+-|\bdelete\s+from|\bdrop\s+table|\bos\.system|\bsubprocess|\beval\(|\bexec\(|\btoken\b|\bpassword\b|\bsecret\b|权限|凭证|部署|生产环境|上传到|发送邮件)')
INJECTION = re.compile(r'(?i)(ignore (?:all |previous |system )*instructions|reveal.*(?:secret|prompt)|忽略.{0,12}(?:系统|安全|权限)|绕过.{0,12}(?:权限|审核|安全)|泄露.{0,12}(?:密钥|凭证))')


def redact(text, limit=12000):
    value = str(text or '')
    value = SECRET.sub('[REDACTED]', value)
    value = TOKEN.sub('[REDACTED_TOKEN]', value)
    return PRIVATE.sub('[PRIVATE_PATH_OR_EMAIL]', value)[:limit]


def actor_session(db, user_id, session_id):
    user = db.get(User, user_id, populate_existing=True)
    session = db.get(Session, session_id, populate_existing=True)
    if not user or not user.is_active or user.must_change_password or not session or session.user_id != user_id or session.status == 'closed':
        raise HTTPException(403, '进化作业的用户或会话已不可用')
    from app.services.agent_access import can_execute_agent
    if not can_execute_agent(db, user.id, session.agent_id, user.role):
        raise HTTPException(403, '会话 Agent 权限已失效，不能继续进化')
    return user, session


def preference(db, user_id):
    row = db.get(SkillEvolutionPreference, user_id, populate_existing=True)
    return {'enabled': bool(settings.SKILL_EVOLUTION_ENABLED and settings.SKILL_HUB_ENABLED and row and row.enabled),
            'paused_resources': list(row.paused_resources or []) if row else []}


def is_enabled(db, user_id):
    return preference(db, user_id)['enabled']


def permit_fork(db, row, actor, version_id):
    access.visible_version(db, row, actor, version_id)
    if row.owner_id == actor.id:
        return
    allowed = row.allow_fork if row.owner_id is None else any(
        s.resource_id == row.id and s.allow_fork and s.version_id == version_id
        for s in access.shares_for(db, actor))
    if not allowed:
        raise HTTPException(403, '来源没有派生许可，不能自动复制或改写')


def candidates(db, job, actor):
    mapping = access.managed_map(db)
    rows = {}
    for skill_id, version_id in (job.runtime_versions or {}).items():
        skill = db.get(Skill, int(skill_id))
        if not skill or skill.name not in (job.selected_skills or []):
            continue
        row = mapping.get(skill.id)
        if not row:
            row = db.query(SkillHubResource).filter_by(skill_id=skill.id).first()
        if not row:
            continue
        if row.archived or access.is_blocked(db, row):
            continue
        try:
            visible = access.visible_version(db, row, actor, access.current_id(db, row) if row.owner_id == actor.id else None)
            member = access.published_member(db, row, skill.id, visible.id)
            if member != version_id:
                continue
            permit_fork(db, row, actor, visible.id)
        except HTTPException:
            continue
        rows[row.id] = (row, visible)
    return rows


def context(db, job):
    actor, _ = actor_session(db, job.user_id, job.session_id)
    recent = db.query(Message).filter(Message.session_id == job.session_id,
        Message.id <= job.message_id).order_by(Message.id.desc()).limit(8).all()
    corrections = db.query(Message).filter(Message.session_id == job.session_id,
        Message.id <= job.message_id, Message.role == 'user').order_by(Message.id.desc()).limit(3).all()
    messages = sorted({m.id: m for m in recent + corrections}.values(), key=lambda m: m.id)
    history = []
    for message in messages:
        meta = json.loads(message.metadata_json or '{}')
        tools = [{'name': s.get('name', ''), 'status': s.get('status', ''), 'state': s.get('state', ''),
                  'output': redact(s.get('result') or s.get('output') or '', 1500)}
                 for s in (meta.get('steps') or []) if s.get('type') == 'tool_call'][-8:]
        history.append({'id': message.id, 'role': message.role,
                        'content': redact(message.content, 3000), 'tools': tools})
    found = candidates(db, job, actor)
    skills = []
    budget = 18000
    for row, native in list(found.values())[:8]:
        directory = content.version_directory(db, row, native)
        files = {}
        for entry in native.manifest:
            path = entry['path']
            if budget <= 0:
                break
            if Path(path).suffix.lower() not in {'.md', '.py', '.json', '.yaml', '.yml', '.txt', '.sh', '.js', '.ts'}:
                continue
            text = redact((directory / path).read_text(encoding='utf-8', errors='replace'), min(6000, budget))
            budget -= len(text)
            files[path] = text
        skills.append({'resource_id': row.id, 'title': row.title, 'version_id': native.id,
                       'owner': row.owner_id == actor.id, 'files': files})
    existing = [{'resource_id': row.id, 'title': redact(row.title, 200), 'summary': redact(row.summary, 300)}
                for row in db.query(SkillHubResource).filter_by(owner_id=actor.id, archived=False).limit(100)]
    return {'messages': history, 'skills': skills, 'existing_personal': existing}


def evidence_checked(proposal, snapshot):
    if not proposal.evidence:
        raise HTTPException(422, '缺少用户纠正或实际工具结果证据')
    messages = {m['id']: m for m in snapshot['messages']}
    checked = []
    for item in proposal.evidence:
        message = messages.get(item.message_id)
        if not message or '[REDACTED' in item.quote or '[PRIVATE_' in item.quote:
            raise HTTPException(422, '证据不属于本次脱敏问答')
        if item.kind == 'user_correction':
            valid = message['role'] == 'user' and item.quote in message['content']
        else:
            valid = any(item.quote in tool['output'] and tool['status'] in {'done', 'success', 'completed'} and tool.get('state') not in {'error', 'failed'}
                        for tool in message['tools'])
        if not valid:
            raise HTTPException(422, '证据必须引用真实用户纠正或成功工具结果，不能使用模型自述')
        checked.append({'message_id': item.message_id, 'kind': item.kind})
    return checked


def validate_root(root):
    content.scan(root)
    for path in root.rglob('SKILL.md'):
        parsed = frontmatter.loads(path.read_text(encoding='utf-8-sig'))
        if not str(parsed.get('name') or '').strip() or not str(parsed.get('description') or '').strip() or not parsed.content.strip():
            raise HTTPException(422, 'SKILL.md 必须有 name、description 和非空正文')


def check_text(text):
    if SECRET.search(text) or TOKEN.search(text) or PRIVATE.search(text) or content.has_secret(text) or '[REDACTED' in text or '[PRIVATE_' in text:
        raise HTTPException(422, '候选包含凭证、敏感标记或未脱敏内容')
    if INJECTION.search(text):
        raise HTTPException(422, '候选含绕过权限或系统指令的内容')
    if len(text.encode('utf-8')) > 100000:
        raise HTTPException(413, '单次进化文本过大')


def risk_of(operations, before):
    risks = []
    for operation in operations:
        old = before.get(operation.path, '')
        changed = operation.content if operation.action == 'write' else operation.new_string
        if operation.action == 'delete':
            risks.append('删除文件')
        if operation.action == 'write' and old and Path(operation.path).name == 'SKILL.md':
            risks.append('完整重写技能入口，需确认原契约未变化')
        if re.search(r'(?i)```(?:bash|sh|python|javascript|sql)|open\(.{0,80}[\"\'](?:w|a)|unlink\(|remove\(|shell=True', changed):
            risks.append('新增执行步骤或文件副作用')
        if Path(operation.path).name == 'SKILL.md' and old:
            previous = frontmatter.loads(old)
            next_text = old.replace(operation.old_string, operation.new_string, 1) if operation.action == 'patch' else changed
            current = frontmatter.loads(next_text)
            if previous.metadata != current.metadata:
                risks.append('技能路由或元数据契约变更')
        if Path(operation.path).suffix.lower() not in {'.md', '.txt'}:
            risks.append('可执行或配置文件变更')
        if DANGEROUS.search(changed) and changed != old:
            risks.append('命令、外部访问、权限或副作用变更')
        if '输入' in old and '输入' not in changed and operation.action == 'write':
            risks.append('可能改变输入契约')
    return sorted(set(risks))
