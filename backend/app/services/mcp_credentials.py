"""MCP environment-variable storage and redacted API metadata."""

import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

from app.config import settings
from app.core.secrets import obfuscate_secret, reveal_secret


ENCRYPTED_PREFIX = "enc$v2$"
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
INLINE_SECRET = re.compile(r"(?i)(?:^|[^a-z0-9])(?:api[_-]?key|token|secret|password|authorization)(?:$|[^a-z0-9])")


class McpCredentialError(ValueError):
    pass


class McpEncryptionUnavailable(McpCredentialError):
    pass


def has_inline_credentials(command: Optional[str], args: list[str], url: Optional[str]) -> bool:
    if INLINE_SECRET.search(command or ""):
        return True
    if any(INLINE_SECRET.search(argument) for argument in args):
        return True
    parsed = urlsplit(url or "")
    return bool(
        parsed.username or parsed.password or
        any(INLINE_SECRET.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
    )


def stored_env(raw: Optional[str]) -> dict[str, str]:
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise McpCredentialError("MCP credential data is invalid") from exc
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise McpCredentialError("MCP credential data is invalid")
    return values


def runtime_env(raw: Optional[str]) -> dict[str, str]:
    result = {}
    for key, stored in stored_env(raw).items():
        if stored.startswith(ENCRYPTED_PREFIX):
            plain = reveal_secret(stored)
            if not plain:
                raise McpEncryptionUnavailable("MCP credential cannot be decrypted")
            result[key] = plain
        elif settings.MCP_LEGACY_ENV_READ:
            result[key] = stored
        else:
            raise McpCredentialError("Legacy MCP credentials require migration")
    return result


def update_env(raw: Optional[str], updates: Optional[dict[str, Optional[str]]]) -> tuple[str, bool]:
    values = stored_env(raw)
    if updates is None:
        return json.dumps(values, ensure_ascii=False), False
    changed = False
    for key, value in updates.items():
        if not ENV_KEY.fullmatch(key):
            raise McpCredentialError("Invalid MCP environment variable name")
        if value is None or value == "":
            changed |= key in values
            values.pop(key, None)
            continue
        if not isinstance(value, str) or len(value) > 8192:
            raise McpCredentialError("Invalid MCP credential length")
        if len(settings.SECRET_ENCRYPTION_KEY) < 32:
            raise McpEncryptionUnavailable("MCP credential encryption key is not configured")
        encrypted = obfuscate_secret(value)
        if not encrypted.startswith(ENCRYPTED_PREFIX) or reveal_secret(encrypted) != value:
            raise McpEncryptionUnavailable("MCP credential encryption failed")
        values[key] = encrypted
        changed = True
    return json.dumps(values, ensure_ascii=False), changed


def public_env(raw: Optional[str], rotated_at: Optional[datetime] = None) -> dict:
    try:
        names = stored_env(raw)
    except McpCredentialError:
        return {}
    return {
        name: {
            "configured": True,
            "masked": "********",
            "last_rotated_at": rotated_at.isoformat() if rotated_at else None,
        }
        for name in names
    }
