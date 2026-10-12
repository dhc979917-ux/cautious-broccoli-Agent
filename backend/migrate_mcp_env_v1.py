"""Check legacy MCP values without printing secrets; encrypt after a verified backup.

Apply only after migrate_admin_stage0_v1.py and after the dual-read backend is live.
"""

import argparse
import json
from datetime import datetime, timezone

from app.config import settings
from app.core.secrets import reveal_secret
from app.database import SessionLocal
from app.models.mcp_server import McpServer
from app.services.mcp_credentials import (
    ENCRYPTED_PREFIX, McpCredentialError, has_inline_credentials, stored_env, update_env,
)


def migrate(apply: bool = False) -> tuple[int, int]:
    rows = 0
    legacy = 0
    inline = 0
    with SessionLocal() as session:
        try:
            query = session.query(McpServer).order_by(McpServer.id)
            if apply:
                query = query.with_for_update()
            for server in query:
                try:
                    args = json.loads(server.args_json) if server.args_json else []
                except ValueError:
                    args = []
                if has_inline_credentials(server.command, args, server.url):
                    inline += 1
                values = stored_env(server.env_json)
                if not values:
                    continue
                rows += 1
                plain = {}
                for name, value in values.items():
                    if value.startswith(ENCRYPTED_PREFIX):
                        if not reveal_secret(value):
                            raise McpCredentialError(f"MCP server {server.id} cannot be decrypted")
                    else:
                        legacy += 1
                        if not value:
                            raise McpCredentialError(f"MCP server {server.id} has an empty credential")
                        plain[name] = value
                if plain and len(settings.SECRET_ENCRYPTION_KEY) < 32:
                    raise McpCredentialError("Persistent MCP encryption key is not configured")
                if apply and plain:
                    encrypted, _ = update_env(server.env_json, plain)
                    server.env_json = encrypted
                    server.env_rotated_at = datetime.now(timezone.utc)
            if apply and inline:
                raise McpCredentialError("Inline MCP credentials need separate remediation before migration")
            if apply:
                session.commit()
            else:
                session.rollback()
        except Exception:
            session.rollback()
            raise
    print(
        f"MCP servers with credentials: {rows}; legacy credential values: {legacy}; "
        f"suspected inline-credential servers: {inline}; apply: {apply}"
    )
    return rows, legacy


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    migrate(parser.parse_args().apply)
