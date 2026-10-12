import argparse
import json
import secrets
from pathlib import Path

from sqlalchemy import text

from app.core.security import create_access_token, get_password_hash
from app.database import SessionLocal
from app.models.user import User


ACCOUNTS = {"admin": "qa_modelgateway52_admin", "viewer": "qa_modelgateway52_viewer", "operator": "qa_modelgateway52_operator"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["setup", "cleanup"])
    parser.add_argument("output")
    args = parser.parse_args()
    with SessionLocal() as db:
        existing = db.query(User).filter(User.account.in_(ACCOUNTS.values())).all()
        if args.mode == "cleanup":
            for user in existing:
                db.execute(text("DELETE FROM model_access_policies WHERE scope='user' AND principal=:principal"), {"principal": str(user.id)})
                db.execute(text("DELETE FROM audit_events WHERE actor_id=:id"), {"id": user.id})
                db.execute(text("DELETE FROM sessions WHERE user_id=:id"), {"id": user.id})
                db.delete(user)
            db.commit()
            Path(args.output).unlink(missing_ok=True)
            print("Synthetic gateway users removed")
            return
        if existing:
            raise RuntimeError("Gateway test accounts already exist; inspect before reusing")
        result = {}
        for role, account in ACCOUNTS.items():
            user = User(username={"admin": "模型网关验收管理员", "viewer": "模型网关验收观察者", "operator": "模型网关验收操作员"}[role],
                        account=account, role=role, password_hash=get_password_hash(secrets.token_urlsafe(30)),
                        is_active=True, permissions='["chat.use"]', must_change_password=False)
            db.add(user)
            db.flush()
            token = create_access_token({"sub": str(user.id), "username": user.username,
                                         "role": role, "agent_permissions": [], "tv": 0})
            result[role] = {"id": user.id, "username": user.username, "account": user.account, "token": token}
        db.commit()
        output = Path(args.output)
        output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        output.chmod(0o600)
        print("Synthetic gateway identity created; tokens saved privately")


if __name__ == "__main__":
    main()
