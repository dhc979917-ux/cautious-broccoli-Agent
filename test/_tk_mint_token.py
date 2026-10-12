# -*- coding: utf-8 -*-
# 容器内铸造测试 JWT（用服务端自身密钥，不改任何用户密码）。
# docker exec -e PYTHONPATH=/app <backend> python /tmp/_tk_mint_token.py <user_id>
import sys

from app.database import SessionLocal
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess
from app.core.security import create_access_token


def main(user_id: int) -> int:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            print(f"FAIL: user {user_id} 不存在")
            return 1
        perms = [
            str(a.agent_id)
            for a in db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user.id).all()
        ]
        token = create_access_token({
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "agent_permissions": perms,
            "tv": int(user.token_version or 0),
        })
        print(f"USER:{user.username} ROLE:{user.role}")
        print(f"TOKEN:{token}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
