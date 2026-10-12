"""Explicitly isolated PostgreSQL fixture; no startup schedulers or production writes."""
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from app.config import settings
from app.database import SessionLocal, init_db


def create_app():
    if os.environ.get('SKILL_HUB_ISOLATED') != '1' or 'skillhub52-qa-db' not in settings.DATABASE_URL:
        raise RuntimeError('Only isolated52 PostgreSQL fixture is supported')
    from app.main import app
    from app.models.user import User
    from app.models.agent import Agent
    from app.models.user_agent_access import UserAgentAccess
    from app.core.security import get_password_hash, create_access_token
    init_db()
    result = {}
    with SessionLocal() as db:
        agent = db.query(Agent).filter_by(is_active=True).order_by(Agent.id).first()
        for key, role in [('author','viewer'),('reader','viewer'),('other','operator'),('admin','admin'),('reviewer','admin')]:
            account = 'qa_skillhub52_'+key
            user = db.query(User).filter_by(account=account).first()
            if user is None:
                user = User(username='技能验收-'+key, account=account, role=role, is_active=True,
                    password_hash=get_password_hash(secrets.token_urlsafe(30)), must_change_password=False,
                    permissions='["chat.use", "schedule_manage.create"]')
                db.add(user); db.flush()
                if agent:
                    db.add(UserAgentAccess(user_id=user.id, agent_id=agent.id, access_level='execute'))
            result[key] = {'id': user.id, 'token': create_access_token({'sub': str(user.id), 'username': user.username,
                'role': role, 'tv': user.token_version or 0, 'agent_permissions': [str(agent.id)] if agent else []}),
                'user': {'id': user.id, 'username': user.username, 'account': account, 'role': role,
                    'is_active': True, 'permissions': ['chat.use','schedule_manage.create'], 'agent_permissions': [str(agent.id)] if agent else []}}
        db.commit()
    target = Path('/app/data/skillhub52-identities.json')
    target.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8'); target.chmod(0o600)
    @asynccontextmanager
    async def lifespan(_app):
        yield
    app.router.lifespan_context = lifespan
    return app
