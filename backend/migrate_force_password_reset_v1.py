"""One-off production cutover: reset every user to the shared initial password.

Run only after a verified database backup:
  python migrate_force_password_reset_v1.py --apply --confirm RESET_ALL_USERS
"""

import argparse
from datetime import datetime, timezone

from app.core.password_policy import DEFAULT_INITIAL_PASSWORD
from app.core.security import get_password_hash
from app.database import SessionLocal, init_db
from app.models.user import User


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not args.apply or args.confirm != "RESET_ALL_USERS":
        raise SystemExit("Refusing to run: pass --apply --confirm RESET_ALL_USERS after backup verification")

    init_db()
    db = SessionLocal()
    try:
        users = db.query(User).with_for_update().all()
        now = datetime.now(timezone.utc)
        for user in users:
            user.password_hash = get_password_hash(DEFAULT_INITIAL_PASSWORD)
            user.must_change_password = True
            user.password_initialized_at = now
            user.password_changed_at = None
            user.token_version = int(user.token_version or 0) + 1
        db.commit()
        print(f"Reset {len(users)} users; all existing access tokens were revoked.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
