"""Temporary production-read-only identities; no changes to existing users or MCP policies."""

import argparse
import json
from pathlib import Path
import secrets

from sqlalchemy import text

from app.database import SessionLocal
from app.core.security import create_access_token, get_password_hash
from app.models.agent import Agent
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess


ACCOUNTS = {'admin': 'qa_mcp52_admin', 'viewer': 'qa_mcp52_viewer', 'operator': 'qa_mcp52_operator'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'cleanup'])
    parser.add_argument('output')
    args = parser.parse_args()
    with SessionLocal() as db:
        existing = db.query(User).filter(User.account.in_(ACCOUNTS.values())).all()
        if args.mode == 'cleanup':
            for user in existing:
                db.execute(text('DELETE FROM audit_events WHERE actor_id=:id'), {'id': user.id})
                db.execute(text('DELETE FROM user_agent_access WHERE user_id=:id'), {'id': user.id})
                db.delete(user)
            db.commit()
            Path(args.output).unlink(missing_ok=True)
            print('Temporary MCP browser identities removed')
            return
        if existing:
            raise RuntimeError('Temporary identities already exist; inspect before reusing')
        agent = db.query(Agent).filter_by(is_active=True).order_by(Agent.id).first()
        result = {}
        for role, account in ACCOUNTS.items():
            user = User(username={'admin': 'MCP验收管理员', 'viewer': 'MCP验收观察者', 'operator': 'MCP验收操作员'}[role],
                        account=account, role=role, is_active=True, permissions='["chat.use"]',
                        password_hash=get_password_hash(secrets.token_urlsafe(30)), must_change_password=False)
            db.add(user); db.flush()
            ids = [str(agent.id)] if agent else []
            if agent:
                db.add(UserAgentAccess(user_id=user.id, agent_id=agent.id, access_level='execute'))
            result[role] = {'id': user.id, 'token': create_access_token({'sub': str(user.id), 'username': user.username,
                'role': role, 'tv': 0, 'agent_permissions': ids}), 'user': {'id': user.id, 'username': user.username,
                'account': account, 'role': role, 'is_active': True, 'permissions': ['chat.use'], 'agent_permissions': ids}}
        db.commit()
        output = Path(args.output)
        output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        output.chmod(0o600)
        print('Temporary MCP identities saved privately')


if __name__ == '__main__':
    main()
