"""Atomically migrate stored user credentials from legacy obfuscation to AES-GCM.

Run without flags for a read-only dry run, then rerun with --apply after a DB
backup and persistent SECRET_ENCRYPTION_KEY provisioning.
"""

import argparse
import json

from app.config import settings
from app.core.secrets import obfuscate_secret, reveal_secret
from app.database import SessionLocal
from app.models.user import User
from app.models.message import Message
from app.models.session import Session as ChatSession
from app.models.session_context import SessionContext


def _redact(value, secrets: list[str]):
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if key.lower() == "mail_password" else _redact(item, secrets))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def migrate(apply: bool = False) -> int:
    if len(settings.SECRET_ENCRYPTION_KEY) < 32:
        raise RuntimeError("A persistent SECRET_ENCRYPTION_KEY of 32+ characters is required")
    count = 0
    redacted_contexts = 0
    redacted_steps = 0
    with SessionLocal() as db:
        try:
            users = db.query(User).order_by(User.id).with_for_update().all() if apply else db.query(User).all()
            for user in users:
                user_secrets = []
                for field in ("mail_password",):
                    stored = getattr(user, field)
                    if not stored:
                        continue
                    plain = reveal_secret(stored)
                    if not plain:
                        raise RuntimeError(f"User {user.id} {field} cannot be decrypted; transaction rolled back")
                    user_secrets.append(plain)
                    if stored.startswith("enc$v2$"):
                        continue
                    encrypted = obfuscate_secret(plain)
                    if len(encrypted) > 255 or not encrypted.startswith("enc$v2$") or reveal_secret(encrypted) != plain:
                        raise RuntimeError(f"User {user.id} {field} failed encryption validation")
                    if apply:
                        setattr(user, field, encrypted)
                    count += 1
                if not user_secrets:
                    continue
                session_ids = [row.id for row in db.query(ChatSession.id).filter(ChatSession.user_id == user.id)]
                if not session_ids:
                    continue
                for context in db.query(SessionContext).filter(SessionContext.session_id.in_(session_ids)):
                    original = json.loads(context.state_json)
                    sanitized = _redact(original, user_secrets)
                    if sanitized != original:
                        redacted_contexts += 1
                        if apply:
                            context.state_json = json.dumps(sanitized, ensure_ascii=False)
                for message in db.query(Message).filter(Message.session_id.in_(session_ids), Message.metadata_json.isnot(None)):
                    original = json.loads(message.metadata_json)
                    sanitized = _redact(original, user_secrets)
                    if sanitized != original:
                        redacted_steps += 1
                        if apply:
                            message.metadata_json = json.dumps(sanitized, ensure_ascii=False)
            if apply:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
    print(
        f"Credentials {'migrated' if apply else 'eligible for migration'}: {count}; "
        f"contexts redacted: {redacted_contexts}; message metadata redacted: {redacted_steps}"
    )
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    migrate(args.apply)
