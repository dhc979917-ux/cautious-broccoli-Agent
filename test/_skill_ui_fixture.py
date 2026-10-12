"""UI 冒烟夹具：创建临时管理员（供 _diag_skill_access_ui.mjs 使用，测完删除）。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from app.database import SessionLocal  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_skill_access import UserSkillAccess  # noqa: E402

ADMIN = "_SKILLUI_admin"
PW = "SkillUI#2026"
OUT = os.path.join(HERE, "_skill_ui_user.json")


def _purge(db):
    ids = [u.id for u in db.query(User).filter(User.account == ADMIN).all()]
    if ids:
        db.query(UserSkillAccess).filter(UserSkillAccess.user_id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
    db.commit()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "setup"
    db = SessionLocal()
    try:
        _purge(db)
        if mode == "teardown":
            print("[OK] UI fixture admin removed")
            return
        admin = User(account=ADMIN, username="技能UI管理员", role="admin",
                     password_hash=get_password_hash(PW), is_active=True)
        db.add(admin)
        db.commit()
        db.refresh(admin)
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump({"account": ADMIN, "password": PW}, fh)
        print(f"[OK] {OUT} (admin_id={admin.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
